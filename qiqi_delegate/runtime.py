"""Repository-scoped Herdr runner with native Stop capture. No Supervisor process."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
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
                    created_at_ns INTEGER NOT NULL
                );
            """)

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

    def _claim(self, repository: str, claim_id: str) -> None:
        try:
            with self._connect() as db:
                db.execute("INSERT INTO write_claims VALUES (?, ?, ?)",
                           (repository, claim_id, time.time_ns()))
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(f"repository {repository} is busy; recover stale claim explicitly") from exc

    def release_claim(self, repository: str, claim_id: str) -> bool:
        with self._connect() as db:
            result = db.execute("DELETE FROM write_claims WHERE repository=? AND claim_id=?",
                                (repository, claim_id))
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
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status, _, _ = await self._run("status", "server",
                                               check=False, timeout=5)
                if status == 0:
                    return
                if server.returncode is not None:
                    break
                await asyncio.sleep(0.1)
            if server.returncode is None:
                server.terminate()
                try:
                    await asyncio.wait_for(server.wait(), 3)
                except TimeoutError:
                    server.kill()
                    await server.wait()
            raise RuntimeError(
                "Herdr headless server did not become ready. "
                "Check 'herdr status server', Herdr logs, and HERDR_SOCKET_PATH; "
                "do not run 'herdr session attach' from a nested Herdr pane."
            )

    async def _run(self, *args: str, timeout=60, check=True):
        argv = self._herdr_argv(*args)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"Herdr command timeout: {args[:2]}")
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
            if not (isinstance(data, dict) and isinstance(data.get("error"), dict)
                    and data["error"].get("code") == "agent_pane_busy"):
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
                       session_id: str | None = None) -> dict:
        repos = self.repos()
        if repository not in repos:
            raise ValueError(f"unknown repository: {repository}; available: {', '.join(repos)}")
        adapter, args = self.route(route)
        if not shutil.which(self.herdr_bin):
            raise RuntimeError(f"Herdr CLI not found: {self.herdr_bin}")
        if session_id:
            with self._connect() as db:
                row = db.execute("SELECT repository, adapter FROM sessions WHERE session_id=?",
                                 (session_id,)).fetchone()
            if row is None or row["repository"] != repository or row["adapter"] != adapter:
                raise ValueError("unknown session or session owned by another repository/agent")
        turn_id = str(uuid.uuid4())
        claim_id = "turn:" + turn_id
        self._claim(repository, claim_id)
        workspace_id = None
        closed = False
        result = None
        try:
            with tempfile.TemporaryDirectory(prefix="qiqi-result-") as td:
                sink = Path(td)
                os.chmod(sink, 0o700)
                nonce = uuid.uuid4().hex
                await self._ensure_herdr_server()
                top = await self._json("workspace", "create", "--cwd", str(repos[repository]),
                                       "--label", f"qiqi:{repository}:{turn_id[:8]}", "--no-focus")
                workspace_id = top["workspace"]["workspace_id"]
                pane_id = top["root_pane"]["pane_id"]
                agent_args = self._hook_args(adapter, sink, nonce) + args
                if session_id:
                    agent_args += (["resume", session_id] if adapter == "codex"
                                   else ["--resume", session_id])
                name, agent = await self._start_agent(pane_id, adapter, agent_args)
                prompt = render_task_prompt(packet)
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
        finally:
            error = None
            if workspace_id:
                try:
                    await self._run("workspace", "close", workspace_id, timeout=20)
                    closed = True
                except Exception as exc:
                    error = str(exc)
            else:
                closed = True
            if closed:
                self.release_claim(repository, claim_id)
            if error:
                if result is None:
                    raise RuntimeError(f"workspace close unconfirmed; claim={claim_id}; {error}")
                result.update({"cleanup_state": "workspace_close_unconfirmed",
                               "write_claim_id": claim_id, "write_claim_repository": repository,
                               "workspace_id": workspace_id,
                               "recovery_action": "Verify worker has stopped; then release claim"})
        assert result is not None
        return result
