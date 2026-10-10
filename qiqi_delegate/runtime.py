"""Repository-scoped Herdr runner with native Stop capture. No Supervisor process."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
import yaml

from qiqi_delegate.core import (
    TaskPacket, render_task_prompt, codex_stop_hook_hash,
    codex_session_hook_key, load_capture_events, resolve_capture_events,
)

class AgentStartupBlocked(RuntimeError):
    """Herdr started a named Peer, but its startup UI requires attention."""

    def __init__(
        self,
        agent_name: str,
        pane_id: str,
        evidence: str,
        *,
        recovery_command: str | None = None,
        public_context: str | None = None,
    ):
        self.agent_name = agent_name
        self.pane_id = pane_id
        self.recovery_command = recovery_command
        self.public_context = public_context
        super().__init__(
            f"agent_not_ready: {agent_name} blocked during startup; "
            f"pane_id={pane_id}; startup_evidence={evidence}"
        )

    def actionable_detail(self) -> str:
        """Bound verbose evidence, never truncate the exact operator recovery command."""
        if self.recovery_command is None:
            return str(self)[:1200]
        context = self.public_context or (
            f"agent_not_ready; agent_name={self.agent_name}; pane_id={self.pane_id}"
        )
        return f"{context[:1200]}; recovery_command={self.recovery_command}"


def workspace_root() -> Path:
    raw = os.environ.get("QIQI_WORKSPACE_ROOT")
    if not raw:
        raise RuntimeError("QIQI_WORKSPACE_ROOT is required; install from workspace root")
    root = Path(raw).resolve()
    if not root.is_dir():
        raise RuntimeError(f"workspace does not exist: {root}")
    return root

class DelegateRuntime:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state = self.root / ".herdr-task-mcp"
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.state / "qiqi_delegate.sqlite3"
        self.herdr_bin = os.environ.get("QIQI_HERDR_BIN", "herdr")
        # By default use the Herdr server already selected by the caller:
        # HERDR_SOCKET_PATH (inside a Herdr pane) or HERDR_SESSION/default.
        # Forcing --session would bypass that socket and create a dead namespace.
        explicit_session = os.environ.get("QIQI_HERDR_SESSION")
        self.herdr_session = explicit_session.strip() if explicit_session is not None else None
        if explicit_session is not None and not self.herdr_session:
            raise ValueError("QIQI_HERDR_SESSION must be non-empty when set")
        self._herdr_server_lock = asyncio.Lock()
        self._ensure_db()

    def _connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _ensure_db(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    adapter TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    route TEXT NOT NULL,
                    state TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS write_claims (
                    repository TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    repository_root TEXT
                );
            """)
            # Schema upgrade must be serialized with other MCP processes.
            # Legacy claims cannot be safely mapped by current mutable aliases:
            # leave their roots NULL and reject all new claims until they are
            # released by exact ID (or their existing worker finishes).
            db.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in db.execute(
                    "PRAGMA table_info(write_claims)"
                ).fetchall()
            }
            if "repository_root" not in columns:
                db.execute(
                    "ALTER TABLE write_claims ADD COLUMN repository_root TEXT"
                )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS write_claims_canonical_root "
                "ON write_claims(repository_root) "
                "WHERE repository_root IS NOT NULL"
            )

    def _load_yaml(self, filename: str) -> dict:
        path = self.root / filename
        if not path.is_file():
            raise ValueError(f"missing workspace config: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{filename} must be a YAML object")
        return data

    def repos(self) -> dict[str, Path]:
        config = self._load_yaml("repos.yaml")
        entries = config.get("repositories")
        if not isinstance(entries, list):
            raise ValueError("repos.yaml repositories must be a list")
        result = {}
        canonical_roots: dict[Path, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"name", "path"}:
                raise ValueError("repos.yaml entries need exact name and path")
            name, path = entry["name"], entry["path"]
            if not isinstance(name, str) or not name.strip() or name in result:
                raise ValueError("invalid or duplicate repository name")
            if not isinstance(path, str) or not path.strip() or Path(path).is_absolute():
                raise ValueError("repository path must be nonempty and relative")
            target = (self.root / path).resolve()
            # A standalone MCP is installed in the control workspace, while
            # Git repositories can be children OR siblings of that workspace.
            # Explicitly registered sibling paths (../frontend, ../backend)
            # stay inside the common parent boundary. Do not allow arbitrary
            # ancestor traversal or symlink escapes outside that boundary.
            allowed_root = self.root.parent
            if target == allowed_root or not target.is_relative_to(allowed_root):
                raise ValueError(
                    f"repos.yaml repository {name!r} path escapes the workspace "
                    f"parent boundary: {path!r}"
                )
            if not target.is_dir():
                raise ValueError(f"repository is missing: {target}")
            try:
                proc = subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True, check=True, timeout=10,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ValueError(f"not a Git repository: {target}") from exc
            if Path(proc.stdout.strip()).resolve() != target:
                raise ValueError(f"repository path is not exact Git root: {target}")
            if target in canonical_roots:
                raise ValueError(
                    f"repos.yaml repository {name!r} points to the same Git root "
                    f"as {canonical_roots[target]!r}: {target}; "
                    "repository aliases would bypass write-claim exclusivity"
                )
            canonical_roots[target] = name
            result[name] = target
        return result

    def route(self, name: str) -> tuple[str, list[str]]:
        data = self._load_yaml("agent-routing.yaml")
        routes = data.get("routes")
        if not isinstance(routes, dict) or name not in routes:
            raise ValueError(f"unknown route: {name}")
        route = routes[name]
        if not isinstance(route, dict) or set(route) - {"agent", "args"}:
            raise ValueError("route must contain agent and optional args only")
        agent = route.get("agent")
        args = route.get("args", [])
        if agent not in {"codex", "claude"}:
            raise ValueError("route agent must be codex or claude")
        if (not isinstance(args, list) or len(args) > 32 or
            any(not isinstance(a, str) or not a.strip() or "\x00" in a for a in args)):
            raise ValueError("route args must be an array of CLI argument strings")
        forbidden = ("--settings", "hooks.Stop", "hooks.state", "features.hooks")
        if any(a in forbidden or a.startswith(("hooks.", "features.hooks")) for a in args):
            raise ValueError("result capture configuration is runtime-owned")
        return agent, args

    def _claim(
        self,
        repository: str,
        claim_id: str,
        *,
        repository_root: Path | None = None,
    ) -> None:
        # The path actually used for execution is the write identity. A
        # logical repo name can be renamed while an earlier worker is alive.
        root = repository_root if repository_root is not None else self.repos()[repository]
        canonical = str(root.resolve())
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                # An old claim has no trustworthy root when repos.yaml may
                # have been renamed. Fail closed rather than guessing and
                # dispatching a concurrent writer during a rolling upgrade.
                unknown = db.execute(
                    "SELECT repository FROM write_claims "
                    "WHERE repository_root IS NULL LIMIT 1"
                ).fetchone()
                if unknown is not None:
                    raise RuntimeError(
                        "unmapped legacy write claim is active for "
                        f"{unknown['repository']!r}; verify worker termination "
                        "and release its exact claim before delegation"
                    )
                db.execute(
                    "INSERT INTO write_claims("
                    "repository, claim_id, created_at_ns, repository_root"
                    ") VALUES (?, ?, ?, ?)",
                    (repository, claim_id, time.time_ns(), canonical),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(
                f"repository {repository} / Git root {canonical} is busy; "
                "recover stale claim explicitly"
            ) from exc

    def release_claim(self, repository: str, claim_id: str) -> bool:
        with self._connect() as db:
            result = db.execute(
                "DELETE FROM write_claims WHERE repository=? AND claim_id=?",
                (repository, claim_id),
            )
            return result.rowcount == 1

    def _session(self, session_id: str, repository: str, adapter: str):
        with self._connect() as db:
            entry = db.execute("SELECT repository, adapter FROM sessions WHERE session_id=?",
                               (session_id,)).fetchone()
            if entry is None:
                db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                           (session_id, repository, adapter))
            elif entry["repository"] != repository or entry["adapter"] != adapter:
                raise RuntimeError("native session is owned by another repository/agent")

    def _herdr_argv(self, *args: str) -> list[str]:
        argv = [self.herdr_bin]
        if self.herdr_session:
            argv.extend(("--session", self.herdr_session))
        argv.extend(args)
        return argv

    async def _ensure_herdr_server(self) -> None:
        """Reuse the active Herdr server; launch headless only when absent.

        'herdr session attach' is an interactive TUI launch and is forbidden
        in nested Herdr panes. 'herdr server' is the headless server command.
        """
        status, _, _ = await self._run("status", "server", check=False, timeout=10)
        if status == 0:
            return
        async with self._herdr_server_lock:
            status, _, _ = await self._run("status", "server", check=False, timeout=10)
            if status == 0:
                return
            # Detached headless Herdr stays alive after the MCP stdio process
            # exits. Do not launch the interactive Herdr client/attach command.
            server = await asyncio.create_subprocess_exec(
                *self._herdr_argv("server"),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=(os.name == "posix"),
            )
            ready = False
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    status, _, _ = await self._run(
                        "status", "server", check=False, timeout=5
                    )
                    if status == 0:
                        ready = True
                        return
                    if server.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
                raise RuntimeError(
                    "Herdr headless server did not become ready. "
                    "Check 'herdr status server', Herdr logs, and HERDR_SOCKET_PATH; "
                    "do not run 'herdr session attach' from a nested Herdr pane."
                )
            finally:
                # A failing/raising readiness probe must not strand the
                # detached process (start_new_session=True on POSIX).
                # Only confirmed readiness transfers ownership to Herdr.
                if not ready:
                    cancelled = await self._terminate_and_reap_server(server)
                    if cancelled:
                        # Cancellation during cleanup supersedes an earlier
                        # startup error just as cancellation during a timeout
                        # supersedes the earlier command timeout.
                        raise asyncio.CancelledError()

    @staticmethod
    async def _await_reaper(
        reaper: asyncio.Task[Any], *, timeout: float | None = None,
    ) -> tuple[bool, bool]:
        """Wait without cancelling the child reaper; remember parent cancellations.

        asyncio.wait() does not cancel supplied tasks when its own waiter is
        cancelled or times out. Keep waiting after repeated cancellations,
        and propagate that cancellation only once the child is reaped.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        cancelled = False
        while not reaper.done():
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            if remaining == 0:
                return cancelled, False
            try:
                await asyncio.wait((reaper,), timeout=remaining)
            except asyncio.CancelledError:
                cancelled = True
        await reaper
        return cancelled, True

    @classmethod
    async def _terminate_and_reap_server(cls, server: asyncio.subprocess.Process) -> bool:
        """Gracefully stop a failed headless launch, escalating after three seconds."""
        if server.returncode is None:
            try:
                server.terminate()
            except ProcessLookupError:
                pass
        reaper = asyncio.create_task(server.wait())
        cancelled, completed = await cls._await_reaper(reaper, timeout=3)
        if not completed:
            if server.returncode is None:
                try:
                    server.kill()
                except ProcessLookupError:
                    pass
            cancelled_after_kill, _ = await cls._await_reaper(reaper)
            cancelled = cancelled or cancelled_after_kill
        return cancelled

    @classmethod
    async def _kill_and_reap(cls, proc: asyncio.subprocess.Process) -> bool:
        """Kill a command and report cancellation arriving during its cleanup."""
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass  # Child exited between checking returncode and kill.
        reaper = asyncio.create_task(proc.communicate())
        cancelled, _ = await cls._await_reaper(reaper)
        return cancelled

    async def _run(self, *args: str, timeout=60, check=True):
        argv = self._herdr_argv(*args)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            cancelled = await self._kill_and_reap(proc)
            if cancelled:
                # A request cancelled *during* timeout cleanup must not
                # report a timeout instead of its cancellation.
                raise asyncio.CancelledError()
            raise RuntimeError(f"Herdr command timeout: {args[:2]}")
        except asyncio.CancelledError:
            await self._kill_and_reap(proc)
            raise
        stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
        if proc.returncode and check:
            detail = (stderr or stdout)[-1800:]
            raise RuntimeError(f"Herdr failed ({proc.returncode}) {args[:3]}: {detail}")
        return proc.returncode, stdout, stderr

    async def _json(self, *args: str, timeout=60):
        _, stdout, _ = await self._run(*args, timeout=timeout)
        try:
            value = json.loads(stdout)
            return value["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"invalid Herdr JSON response for {args[:2]}") from exc

    @staticmethod
    def _hook_args(adapter: str, sink: Path, nonce: str) -> list[str]:
        import shlex
        command = shlex.join([
            sys.executable, "-m", "qiqi_delegate.result_hook",
            "--adapter", adapter, "--sink", str(sink), "--nonce", nonce,
        ])
        if adapter == "claude":
            settings = {"hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": command}]}],
                "StopFailure": [{"hooks": [{"type": "command", "command": command}]}],
            }}
            return ["--settings", json.dumps(settings, separators=(",", ":"))]
        stop = '[{hooks=[{type="command",command=' + json.dumps(command) + ',timeout=10}]}]'
        key = codex_session_hook_key()
        state = "{" + json.dumps(key) + "={trusted_hash=" + json.dumps(codex_stop_hook_hash(command)) + "}}"
        return ["-c", "features.hooks=true", "-c", f"hooks.Stop={stop}", "-c", f"hooks.state={state}"]

    async def _start_agent(self, pane_id: str, adapter: str, argv: list[str]) -> tuple[str, dict]:
        name = "qiqi-" + uuid.uuid4().hex[:12]
        args = ["agent", "start", name, "--kind", adapter, "--pane", pane_id,
                "--timeout", "60000", "--", *argv]
        deadline = time.monotonic() + 10
        while True:
            rc, out, err = await self._run(*args, timeout=65, check=False)
            data = None
            for raw in (out, err):
                try:
                    data = json.loads(raw)
                    break
                except ValueError:
                    continue
            if rc == 0:
                if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
                    raise RuntimeError("agent start did not return Herdr JSON")
                agent = data["result"].get("agent")
                if not isinstance(agent, dict):
                    raise RuntimeError("agent start returned no agent identity")
                return name, agent
            code = (data.get("error", {}).get("code")
                    if isinstance(data, dict) and isinstance(data.get("error"), dict)
                    else None)
            if code == "agent_not_ready":
                # Herdr intentionally leaves this named agent running while its
                # interactive startup prompt is blocked (trust/auth/approval).
                # Use structured detection evidence, NOT terminal final-output scraping.
                evidence = "agent start returned agent_not_ready"
                try:
                    rc_explain, out_explain, _ = await self._run(
                        "agent", "explain", name, "--json", check=False, timeout=8
                    )
                    if rc_explain == 0 and out_explain.strip():
                        evidence = out_explain[-1600:].strip()
                except (OSError, RuntimeError, TimeoutError):
                    pass
                raise AgentStartupBlocked(name, pane_id, evidence)
            if code != "agent_pane_busy":
                raise RuntimeError(f"agent start failed: {(err or out)[-1800:]}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"pane {pane_id} did not become ready in 10s")
            await asyncio.sleep(.1)

    @staticmethod
    def _native_id(agent: dict, adapter: str) -> str | None:
        identity = agent.get("agent_session")
        if not isinstance(identity, dict):
            return None
        if identity.get("kind") != "id" or identity.get("agent") != adapter:
            raise RuntimeError("Herdr native session identity mismatch")
        value = identity.get("value")
        return value if isinstance(value, str) and value else None

    async def _agent_get(self, name: str) -> dict:
        result = await self._json("agent", "get", name, timeout=10)
        return result["agent"]

    async def _prompt(self, name: str, prompt: str, adapter: str):
        rc, out, err = await self._run("agent", "prompt", name, prompt, "--wait",
                                       timeout=3700, check=False)
        payload = None
        try:
            payload = json.loads(out)
        except ValueError:
            pass
        if rc:
            # Do not infer success from the screen. The hook still provides content.
            raise RuntimeError(f"agent prompt failed: {(err or out)[-1300:]}")
        if not isinstance(payload, dict):
            raise RuntimeError("agent prompt returned invalid JSON")
        agent = payload.get("result", {}).get("agent")
        if not isinstance(agent, dict):
            raise RuntimeError("agent prompt returned no agent state")
        status = agent.get("agent_status")
        if status not in {"idle", "done", "blocked"}:
            raise RuntimeError(f"unexpected Herdr status: {status}")
        return status, agent

    @staticmethod
    async def _capture(sink: Path, nonce: str, adapter: str, session_id: str, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = load_capture_events(sink, nonce)
            for event in events:
                if event.get("state") == "capture_error" and event.get("hook_failure"):
                    raise RuntimeError(f"native capture hook failed: {event.get('error')}")
            try:
                resolved = resolve_capture_events(events, adapter=adapter, session_id=session_id)
                if resolved["state"] in {"settled", "failed", "capture_ambiguous"}:
                    return resolved
            except RuntimeError as exc:
                if "overflow" in str(exc):
                    raise
            await asyncio.sleep(.05)
        raise RuntimeError("native result hook did not capture final response; no screen fallback")

    async def delegate(self, *, repository: str, route: str, packet: TaskPacket,
                       session_id: str | None = None,
                       discovery_repositories: tuple[str, ...] | None = None,
                       discovery_id: str | None = None,
                       evaluation_repositories: tuple[str, ...] | None = None,
                       evaluation_roots: dict[str, Path] | None = None,
                       evaluation_id: str | None = None) -> dict:
        repos = self.repos()
        if repository not in repos:
            raise ValueError(f"unknown repository: {repository}; available: {', '.join(repos)}")
        adapter, args = self.route(route)
        if evaluation_id is not None:
            if (discovery_repositories is not None or discovery_id is not None
                    or evaluation_repositories is None or evaluation_roots is None
                    or session_id is not None):
                raise ValueError("evaluation requires a fresh isolated multi-root session")
            if (not evaluation_repositories or
                    set(evaluation_repositories) != set(evaluation_roots) or
                    len(evaluation_repositories) != len(set(evaluation_repositories)) or
                    repository not in evaluation_repositories or
                    any(name not in repos for name in evaluation_repositories)):
                raise ValueError("evaluation roots must match registered graph repositories")
            if any(not root.is_dir() or root.resolve() != root or
                   root.resolve() in repos.values()
                   for root in evaluation_roots.values()):
                raise ValueError("unsafe evaluation snapshot root")
            # Prompt-only no-write is not protection. Reject unsafe Herdr
            # routes: allow a deliberately configured Codex read-only sandbox
            # and disallow arbitrary config overrides and bypass flags.
            if adapter != "codex" or args not in (
                ["--sandbox", "read-only"],
                ["--sandbox=read-only"],
            ):
                raise ValueError(
                    "Final Evaluator requires a configured Codex route with "
                    "exactly --sandbox read-only and no --yolo/overrides"
                )
            args = list(args) + ["--skip-git-repo-check"]
            for name in evaluation_repositories:
                if name != repository:
                    args += ["--add-dir", str(evaluation_roots[name])]
        if discovery_repositories is not None:
            if session_id is not None:
                raise ValueError("Discovery RESUME is not supported in this workflow")
            if not discovery_repositories or len(set(discovery_repositories)) != len(discovery_repositories):
                raise ValueError("Discovery repositories must be unique and nonempty")
            if repository not in discovery_repositories:
                raise ValueError("Primary repository must be included in Discovery repositories")
            if any(name not in repos for name in discovery_repositories):
                raise ValueError("Discovery references an unregistered repository")
            # All paths come from the checked repos.yaml registry, not agent-supplied args.
            extra_roots = [str(repos[name]) for name in discovery_repositories
                           if name != repository]
            for path in extra_roots:
                args += ["--add-dir", path]
        if not shutil.which(self.herdr_bin):
            raise RuntimeError(f"Herdr CLI not found: {self.herdr_bin}")
        if session_id:
            with self._connect() as db:
                row = db.execute("SELECT repository, adapter FROM sessions WHERE session_id=?",
                                 (session_id,)).fetchone()
            if row is None or row["repository"] != repository or row["adapter"] != adapter:
                raise ValueError("unknown session or session owned by another repository/agent")
        turn_id = str(uuid.uuid4())
        if evaluation_id is not None:
            # Native ID linked BEFORE Peer launch, surviving interruption
            # between capture persistence and coordinator return.
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    "UPDATE final_evaluations SET status='evaluating', turn_id=?, "
                    "updated_at_ns=? WHERE evaluation_id=? "
                    "AND status='requested' AND turn_id IS NULL",
                    (turn_id, time.time_ns(), evaluation_id),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("evaluation dispatch reservation missing")
        if discovery_id is not None:
            if discovery_repositories is None:
                raise ValueError("discovery_id may only be used for Discovery")
            # Establish the association BEFORE launching an agent; native
            # capture and the reservation use one durable SQLite database.
            # A crash after persisting the turn but before the MCP caller
            # attaches its source can now recover the complete result.
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                bound = db.execute(
                    "UPDATE task_discoveries SET turn_id=? "
                    "WHERE discovery_id=? AND state='requested' AND turn_id IS NULL",
                    (turn_id, discovery_id),
                )
                if bound.rowcount != 1:
                    raise RuntimeError("Discovery reservation missing or already bound")
        claim_id = "turn:" + turn_id
        execution_root = (
            evaluation_roots[repository] if evaluation_id is not None
            else repos[repository]
        )
        # Evaluators execute against disposable snapshot directories, but
        # their write claim must retain the stable, *registered* Git-root
        # identity. The operator-only show-claim/release-claim commands resolve
        # repos.yaml, not temporary snapshot paths (which disappear if an
        # AgentStartupBlocked worker must be inspected after cleanup).
        # The registered root is also the mutex identity used by normal Peers:
        # do not allow an evaluator's preserved claim to become unrecoverable
        # and permanently block subsequent delegation.
        claim_root = repos[repository] if evaluation_id is not None else execution_root
        self._claim(repository, claim_id, repository_root=claim_root)
        workspace_id = None
        closed = False
        preserve_startup = False
        result = None
        was_cancelled = False
        try:
            with tempfile.TemporaryDirectory(prefix="qiqi-result-") as td:
                sink = Path(td)
                os.chmod(sink, 0o700)
                nonce = uuid.uuid4().hex
                await self._ensure_herdr_server()
                top = await self._json("workspace", "create", "--cwd", str(execution_root),
                                       "--label", f"qiqi:{repository}:{turn_id[:8]}", "--no-focus")
                workspace_id = top["workspace"]["workspace_id"]
                pane_id = top["root_pane"]["pane_id"]
                agent_args = self._hook_args(adapter, sink, nonce) + args
                if session_id:
                    agent_args += (["resume", session_id] if adapter == "codex"
                                   else ["--resume", session_id])
                name, agent = await self._start_agent(pane_id, adapter, agent_args)
                prompt = render_task_prompt(
                    packet, discovery_repositories=discovery_repositories,
                    evaluation_repositories=evaluation_repositories,
                )
                status, agent = await self._prompt(name, prompt, adapter)
                native = self._native_id(agent, adapter)
                if native is None:
                    for _ in range(150):
                        agent = await self._agent_get(name)
                        native = self._native_id(agent, adapter)
                        if native:
                            break
                        await asyncio.sleep(.1)
                if not native:
                    raise RuntimeError("native session identity not found")
                if session_id and native != session_id:
                    raise RuntimeError("RESUME native session identity mismatch")
                self._session(native, repository, adapter)
                if status == "blocked":
                    result = {"session_id": native, "turn_id": turn_id,
                              "state": "blocked", "agent_response": None,
                              "blocker_type": "agent_blocked"}
                else:
                    capture = await self._capture(sink, nonce, adapter, native)
                    state = capture["state"]
                    if state == "capture_ambiguous":
                        result = {"session_id": native, "turn_id": turn_id,
                                  "state": state, "agent_response": None,
                                  "candidate_count": capture["candidate_count"]}
                    else:
                        response = capture["agent_response"]
                        with self._connect() as db:
                            db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                                       (turn_id, native, repository, route, state, response, time.time_ns()))
                        result = {"session_id": native, "turn_id": turn_id,
                                  "state": state, "agent_response": response}
        except AgentStartupBlocked as exc:
            # Do not destroy the blocked startup pane before the user can inspect
            # it. The write claim stays held until manual verified cleanup.
            preserve_startup = True
            target = (f"{self.herdr_bin} --session {self.herdr_session}"
                      if self.herdr_session else self.herdr_bin)
            # This stdio server is launched with the package's venv Python.
            # Reuse that exact interpreter: operators are not required to
            # activate the venv, and qiqi-delegate-admin is not on global PATH.
            recovery = (
                f"{shlex.quote(sys.executable)} -m qiqi_delegate.maintenance "
                f"release-claim --workspace {shlex.quote(str(self.root))} "
                f"--repository {shlex.quote(repository)} "
                f"--claim-id {shlex.quote(claim_id)} "
                "--worker-termination-confirmed"
            )
            public_context = (
                f"agent_not_ready; agent_name={exc.agent_name}; pane_id={exc.pane_id}; "
                f"workspace_id={workspace_id}; write_claim_id={claim_id}; "
                f"repository={repository}; inspect: {target} agent explain "
                f"{exc.agent_name} --json; startup_evidence={exc}"
            )
            raise AgentStartupBlocked(
                exc.agent_name, exc.pane_id,
                f"workspace_id={workspace_id}; write_claim_id={claim_id}; "
                f"repository={repository}; "
                f"inspect: {target} agent explain {exc.agent_name} --json; "
                f"inspect startup UI: {target} agent read {exc.agent_name} "
                f"--source visible --lines 30; "
                f"recovery: close Herdr workspace {workspace_id} after inspection, "
                f"confirm agent termination, then run: {recovery}. "
                "See README operator recovery instructions; do not send the "
                f"delegated task prompt to the blocked agent manually. {exc}",
                recovery_command=recovery,
                public_context=public_context,
            ) from exc
        except asyncio.CancelledError:
            # Task.cancelling() is only available on newer Python releases;
            # record the original cancellation explicitly for Python 3.10.
            was_cancelled = True
            raise
        finally:
            error = None
            cancelled_during_close = False
            # When delegation itself was cancelled, a failed close must not
            # convert that cancellation into an unrelated RuntimeError.
            if preserve_startup:
                # Keep the workspace and repo claim for safe manual diagnosis.
                pass
            elif workspace_id:
                # The close operation must survive further cancellation of the
                # request, otherwise both the Herdr worker and its write claim
                # can be stranded. Do not mark closed or release the claim until
                # the *actual* Herdr close command has completed successfully.
                async def close_workspace() -> str | None:
                    try:
                        await self._run("workspace", "close", workspace_id, timeout=20)
                    except Exception as exc:
                        return str(exc)
                    return None

                close_task = asyncio.create_task(close_workspace())
                cancelled_during_close, _ = await self._await_reaper(close_task)
                error = close_task.result()
                closed = error is None
            else:
                closed = True
            if closed:
                self.release_claim(repository, claim_id)
            if error:
                if result is None and not (was_cancelled or cancelled_during_close):
                    raise RuntimeError(
                        f"workspace close unconfirmed; workspace={workspace_id}; "
                        f"claim={claim_id}; {error}"
                    )
                if result is not None:
                    result.update({"cleanup_state": "workspace_close_unconfirmed",
                                   "write_claim_id": claim_id, "write_claim_repository": repository,
                                   "workspace_id": workspace_id,
                                   "recovery_action": "Verify worker has stopped; then release claim"})
            if cancelled_during_close:
                # A second cancel must be propagated *after* the close task
                # succeeds or fails, never while it can still leave live agents.
                raise asyncio.CancelledError()
        assert result is not None
        return result
