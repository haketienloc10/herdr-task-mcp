"""Reproducible, bounded Git working-tree copies for final evaluation.

Do not use live registered working trees as evaluator CWD. Copy all
tracked and non-ignored untracked files, not just HEAD: Peer output may be dirty.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


MAX_FILES = 3000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 24_000_000
FORBIDDEN_BASENAMES = {".env", ".env.local", ".env.production", ".npmrc",
                       ".pypirc", "id_rsa", "id_ed25519", "credentials.json"}
FORBIDDEN_PATH_PARTS = {".git", ".herdr-task-mcp", ".qiqi", ".ssh", ".aws",
                        ".venv", "node_modules", "__pycache__"}
# Generated metadata is placed inside isolated repository copies. Collision
# with a real tracked/untracked product file would silently overwrite bytes
# while leaving the original SHA256 recorded in the evaluation manifest.
RESERVED_EVALUATION_PATHS = {
    ".qiqi-evaluation-manifest.json",
    ".qiqi-final-task-sources",
}


def cleanup_temporary_tree(root: Path) -> None:
    """Remove QiQi temporary trees, including intentionally read-only sources."""
    def make_writable_and_retry(func: Any, name: str, _exc: Any) -> None:
        target = Path(name)
        if target != root and root not in target.parents:
            raise RuntimeError("refusing to change permissions outside temporary tree")
        # Source staging removes owner write bits. Never chmod the tree's
        # parent (often /tmp), even if deleting the top-level tree fails.
        parent = target.parent
        if (parent == root or root in parent.parents) and (
            parent.is_dir() and not parent.is_symlink()
        ):
            parent.chmod(0o700)
        if target.exists() and not target.is_symlink():
            target.chmod(0o700 if target.is_dir() else 0o600)
        func(name)

    shutil.rmtree(root, onerror=make_writable_and_retry)


def _remaining_git_timeout(deadline: float | None) -> float:
    if deadline is None:
        return 30
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("live evaluation Git discovery exceeded scan deadline")
    return min(30, remaining)


def _git(
    root: Path, *args: str, deadline: float | None = None,
) -> bytes:
    # Calculate the deadline outside the try/except: TimeoutError inherits
    # OSError and must not be misreported as an ordinary Git failure.
    timeout = _remaining_git_timeout(deadline)
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True, timeout=timeout,
        ).stdout
    except subprocess.TimeoutExpired as exc:
        if deadline is not None:
            raise TimeoutError("live evaluation Git discovery timed out") from exc
        raise RuntimeError("unable to produce verified Git evaluation snapshot") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("unable to produce verified Git evaluation snapshot") from exc


def _git_paths(
    root: Path, *args: str, deadline: float | None = None,
) -> set[str]:
    return {
        part.decode("utf-8")
        for part in _git(root, *args, deadline=deadline).split(b"\0")
        if part
    }


def _reject_gitlinks(
    root: Path, head: str | None, *, deadline: float | None = None,
) -> None:
    """Fail closed for Git submodules instead of interpreting dirs as deletions.

    Git records a submodule as mode 160000 (a gitlink). The submodule's
    populated working tree and its own dirty changes are outside the normal
    ls-files manifest; a digest would otherwise falsely remain unchanged.
    Check BOTH the index and HEAD so staged removal also fails closed.
    """
    for label, payload in [
        ("index", _git(root, "ls-files", "--stage", "-z", deadline=deadline)),
        ("HEAD", _git(root, "ls-tree", "-r", "-z", head, deadline=deadline) if head else b""),
    ]:
        for entry in payload.split(b"\0"):
            if not entry:
                continue
            if b"\t" not in entry:
                raise ValueError("invalid Git tree/index entry")
            metadata, raw_path = entry.split(b"\t", 1)
            if metadata.startswith(b"160000 "):
                path = raw_path.decode("utf-8")
                raise ValueError(
                    "evaluation snapshot cannot safely capture Git "
                    f"submodule/gitlink from {label}: {path}; "
                    "remove the submodule or implement recursive source capture"
                )


def _reject_sparse_checkout(
    root: Path, *, deadline: float | None = None,
) -> None:
    """Fail closed when tracked omissions cannot be distinguished from deletions.

    Sparse checkout intentionally excludes paths that are still part of the
    product. Git represents those index entries with the skip-worktree flag.
    Do not issue deletion tombstones for them: the original source was never
    inspected, and a deletion-only report could otherwise receive false PASS.
    Both checks matter: manual --skip-worktree can be set without sparse mode,
    and sparse mode may hide an entire sparse-index directory or a staged
    deletion may have removed an entry from the index.
    """
    timeout = _remaining_git_timeout(deadline)
    try:
        mode = subprocess.run(
            ["git", "-C", str(root), "config", "--bool", "--get",
             "core.sparseCheckout"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if deadline is not None:
            raise TimeoutError("live evaluation sparse-checkout timed out") from exc
        raise RuntimeError("unable to verify Git sparse-checkout state") from exc
    except OSError as exc:
        raise RuntimeError("unable to verify Git sparse-checkout state") from exc
    if mode.returncode not in (0, 1):
        raise RuntimeError("unable to verify Git sparse-checkout configuration")
    if mode.returncode == 0:
        value = mode.stdout.strip()
        if value == b"true":
            raise ValueError(
                "evaluation snapshot refuses sparse checkout: materialize "
                "the full working tree before final evaluation"
            )
        if value != b"false":
            raise RuntimeError("unrecognized Git sparse-checkout configuration")
    for record in _git(root, "ls-files", "-t", "-z", deadline=deadline).split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise RuntimeError("cannot verify tracked Git index flags")
        if record[:1] == b"S":
            raise ValueError(
                "evaluation snapshot refuses skip-worktree tracked path: "
                + record[2:].decode("utf-8")
            )


def _files(
    root: Path, head: str | None, *, max_files: int = MAX_FILES,
    deadline: float | None = None,
) -> tuple[list[str], set[str], set[str]]:
    """List current files *and* tracked paths removed from the worktree.

    Git's cached index retains unstaged deletions, whereas staged deletions
    vanish from the index. Include HEAD, index and untracked paths to capture
    current files, but accept deletions ONLY for paths present in HEAD.
    Index-only absent paths (intent-to-add or a staged addition subsequently
    removed from the worktree) have no previous committed content and must
    never become trusted deletion evidence.
    """
    _reject_gitlinks(root, head, deadline=deadline)
    _reject_sparse_checkout(root, deadline=deadline)
    tracked = _git_paths(root, "ls-files", "--cached", "-z", deadline=deadline)
    head_paths = (
        _git_paths(root, "ls-tree", "-r", "--name-only", "-z", head, deadline=deadline)
        if head is not None else set()
    )
    tracked.update(head_paths)
    others = _git_paths(
        root, "ls-files", "--others", "--exclude-standard", "-z",
        deadline=deadline,
    )
    names = sorted(tracked | others)
    if not names or len(names) > max_files:
        raise ValueError("evaluation snapshot has no files or exceeds file-count limit")
    for name in names:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("live evaluation Git discovery exceeded scan deadline")
        parts = Path(name).parts
        if any(name == reserved or name.startswith(reserved + "/")
               for reserved in RESERVED_EVALUATION_PATHS):
            raise ValueError(
                "evaluation snapshot reserved metadata path collision: " + name
            )
        if (Path(name).is_absolute() or not parts or
                any(part in {"", ".", ".."} for part in parts) or
                any(part in FORBIDDEN_PATH_PARTS for part in parts) or
                Path(name).name in FORBIDDEN_BASENAMES or
                (Path(name).name.startswith(".env.") and Path(name).name != ".env.example")):
            raise ValueError("unsafe or sensitive file in evaluation snapshot: " + name)
    return names, tracked, head_paths


def _file_present(root: Path, relative: str) -> bool:
    """Distinguish legitimate tracked deletion from unsafe symlink traversal."""
    path = root
    components = Path(relative).parts
    for index, component in enumerate(components):
        path = path / component
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(mode):
            raise ValueError("evaluation snapshot refuses symlink: " + relative)
        if stat.S_ISDIR(mode):
            # A directory replacing an old tracked file is a deletion of
            # that file; any new children appear as untracked source files.
            if index == len(components) - 1:
                return False
        elif index != len(components) - 1:
            return False
    return True


def _read_file(root: Path, relative: str) -> tuple[bytes, int]:
    # lstat every component to prevent symlinks in tracked/untracked paths.
    path = root
    for component in Path(relative).parts:
        path = path / component
        if stat.S_ISLNK(path.lstat().st_mode):
            raise ValueError("evaluation snapshot refuses symlink: " + relative)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
        raise ValueError("unsafe or oversized evaluation file: " + relative)
    # Open with O_NOFOLLOW where supported; compare inode/mtime after read.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("evaluation file changed during snapshot: " + relative)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (len(data) > MAX_FILE_BYTES or
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise RuntimeError("evaluation source changed or exceeded limit: " + relative)
    return data, stat.S_IMODE(after.st_mode)


def inspect_roots(roots: dict[str, Path]) -> dict[str, Any]:
    """Pure inspection: deterministic manifest includes uncommitted and untracked files."""
    if not roots:
        raise ValueError("evaluation needs at least one repository")
    manifest: dict[str, Any] = {}
    total = 0
    for name, root in sorted(roots.items()):
        if not root.is_dir() or root != root.resolve():
            raise ValueError("evaluation repository root must be canonical")
        # Newly initialized Git worktrees may have no commit yet.
        try:
            head = _git(root, "rev-parse", "--verify", "HEAD").decode().strip()
        except RuntimeError:
            head = None
        entries = []
        deleted_paths = []
        names, tracked, head_paths = _files(root, head)
        for relative in names:
            if not _file_present(root, relative):
                if relative not in tracked:
                    # A previously untracked path disappearing after Git
                    # discovery is a concurrent mutation, not a deletion to
                    # hide from the manifest.
                    raise RuntimeError(
                        "untracked evaluation file disappeared: " + relative
                    )
                if relative not in head_paths:
                    # Intent-to-add and staged new files have no committed
                    # previous content. Classifying their absence as a
                    # deletion would allow fabricated deletion-only PASS.
                    raise ValueError(
                        "evaluation snapshot refuses missing index-only path "
                        "without HEAD history: " + relative
                    )
                deleted_paths.append(relative)
                continue
            try:
                data, mode = _read_file(root, relative)
            except (FileNotFoundError, NotADirectoryError) as exc:
                raise RuntimeError(
                    "evaluation file disappeared during snapshot: " + relative
                ) from exc
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("evaluation snapshot exceeds aggregate byte limit")
            entries.append({
                "path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data), "mode": mode,
            })
        manifest[name] = {
            "root": str(root), "head": head,
            "files": entries, "deleted_paths": deleted_paths,
        }
    return manifest


def _hash_live_file(
    root: Path, relative: str, *, deadline: float | None = None,
) -> tuple[str, int, int]:
    """Hash a live file in bounded memory without copying it or reading it all.

    Includes unchanged binary dependencies (JAR/WAR/etc.) in freshness
    checks, rather than applying the snapshot's 1 MB copy limit. Retain
    symlink/inode/mtime checks to fail closed on concurrent modification.
    """
    path = root
    for component in Path(relative).parts:
        path = path / component
        if stat.S_ISLNK(path.lstat().st_mode):
            raise ValueError("evaluation refuses symlink: " + relative)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("evaluation refuses non-regular file: " + relative)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("evaluation file changed during hash: " + relative)
        digest = hashlib.sha256()
        length = 0
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("live evaluation hash exceeded scan deadline")
            data = os.read(fd, 1024 * 1024)
            if not data:
                break
            digest.update(data)
            length += len(data)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (length != before.st_size or
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
             before.st_ctime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             after.st_ctime_ns)):
        raise RuntimeError("evaluation file changed during hash: " + relative)
    return digest.hexdigest(), length, stat.S_IMODE(after.st_mode)


def inspect_live_roots(roots: dict[str, Path]) -> dict[str, Any]:
    """Read-only manifest of registered worktrees without snapshot copies.

    File contents never enter a copied worktree. Large binary dependencies
    are hashed in 1 MiB blocks, not rejected for exceeding 1 MiB.
    """
    if not roots:
        raise ValueError("evaluation needs at least one repository")
    manifest: dict[str, Any] = {}
    deadline = time.monotonic() + 55
    for name, root in sorted(roots.items()):
        if not root.is_dir() or root != root.resolve():
            raise ValueError("evaluation repository root must be canonical")
        try:
            head = _git(
                root, "rev-parse", "--verify", "HEAD", deadline=deadline,
            ).decode().strip()
        except RuntimeError:
            head = None
        # Preserve Git tracking/ignore, sparse-checkout, submodule, sensitive
        # file and tombstone checks. The live mode has no copy-byte budget.
        names, tracked, head_paths = _files(
            root, head, max_files=20000, deadline=deadline,
        )
        entries = []
        deleted_paths = []
        for relative in names:
            if time.monotonic() > deadline:
                raise TimeoutError("live evaluation scan exceeded 55 seconds")
            if not _file_present(root, relative):
                if relative not in tracked:
                    raise RuntimeError("untracked evaluation file disappeared: " + relative)
                if relative not in head_paths:
                    raise ValueError(
                        "evaluation refuses missing index-only path: " + relative
                    )
                deleted_paths.append(relative)
                continue
            sha, size, mode = _hash_live_file(
                root, relative, deadline=deadline,
            )
            entries.append({
                "path": relative, "sha256": sha, "size": size, "mode": mode,
            })
        manifest[name] = {
            "root": str(root), "head": head, "inspection_mode": "live",
            "files": entries, "deleted_paths": deleted_paths,
        }
    return manifest


def _live_scan_worker(roots: dict[str, Path], sink: Any) -> None:
    """Isolated scanner: a blocked filesystem syscall cannot pin MCP threads."""
    try:
        manifest = inspect_live_roots(roots)
        sink.send(("ok", manifest, manifest_digest(manifest)))
    except BaseException as exc:
        try:
            sink.send(("error", type(exc).__name__, str(exc)))
        except (OSError, EOFError, BrokenPipeError):
            pass
    finally:
        sink.close()


def _isolated_live_scan_worker(
    roots: dict[str, Path], sink: Any, worker: Any,
) -> None:
    """Create a scanner-owned POSIX session before it can launch git."""
    if os.name == "posix":
        os.setsid()
    (worker or _live_scan_worker)(roots, sink)


def _stop_scanner_tree(process: Any) -> None:
    """Terminate Git descendants together with their scanner supervisor.

    POSIX: each scanner is its own process group. Windows: taskkill /T /F
    terminates the full descendant tree using its parent PID.
    """
    if process.pid is None:
        return
    if os.name == "posix":
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()  # worker exited before setsid()
        except ProcessLookupError:
            return
        except OSError:
            process.terminate()
        process.join(timeout=0.8)
        try:
            # Descendants may outlive the supervising scanner. Kill the whole
            # original group even if the parent has already exited.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.is_alive():
                process.kill()
    else:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            if process.is_alive():
                process.terminate()
    process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)


def scan_live_roots_bounded(
    roots: dict[str, Path], *, cancel_event: Any = None,
    timeout: float = 65, _worker: Any = None,
) -> tuple[dict[str, Any], str]:
    """Hard-limit an entire live scan, INCLUDING blocking os.read().

    multiprocessing.spawn avoids inheriting the MCP server's open SQLite
    connections, locks and event-loop state. On timeout or cancellation the
    scan child is terminated, not merely a Future backed by a blocked thread.
    The child produces both manifest and digest, so neither requires JSON
    serialization on the caller's event loop.
    """
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_isolated_live_scan_worker, args=(roots, writer, _worker),
    )
    succeeded = False
    try:
        process.start()
        writer.close()
        expires = time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("live evaluation scan cancelled")
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("live evaluation scanner process timed out")
            if reader.poll(min(0.1, remaining)):
                try:
                    result = reader.recv()
                except EOFError as exc:
                    raise RuntimeError("live evaluation scanner exited without result") from exc
                if result[0] == "ok":
                    succeeded = True
                    return result[1], result[2]
                kind, detail = result[1], result[2]
                if kind == "TimeoutError":
                    raise TimeoutError(detail)
                if kind == "ValueError":
                    raise ValueError(detail)
                raise RuntimeError(
                    "live evaluation scanner failed: " + kind + ": " + detail
                )
            if not process.is_alive():
                raise RuntimeError(
                    "live evaluation scanner exited unexpectedly: "
                    + str(process.exitcode)
                )
    finally:
        reader.close()
        writer.close()
        if process.pid is not None:
            if succeeded:
                process.join(timeout=0.2)
            if process.is_alive() or not succeeded:
                _stop_scanner_tree(process)
            else:
                process.join(timeout=0.2)


class LiveEvaluationView:
    """No repository copy; the evaluator reads original registered roots."""

    def __init__(
        self, roots: dict[str, Path],
        precomputed: dict[str, Any] | None = None,
        precomputed_digest: str | None = None,
    ):
        self.paths = roots
        self.manifest: dict[str, Any] = precomputed or {}
        self.digest = precomputed_digest or ""

    def __enter__(self) -> "LiveEvaluationView":
        if not self.manifest:
            self.manifest, self.digest = scan_live_roots_bounded(self.paths)
        if not self.digest:
            self.digest = manifest_digest(self.manifest)
        return self

    def __exit__(self, *_args: Any) -> None:
        pass


def manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        manifest, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class EvaluationSnapshot:
    """Temporary copies of all TaskGraph repositories for one native evaluation."""

    def __init__(self, roots: dict[str, Path]):
        self.roots = roots
        self._temp: Path | None = None
        self._retained = False
        self.paths: dict[str, Path] = {}
        self.manifest: dict[str, Any] = {}
        self.digest = ""
    def __enter__(self) -> "EvaluationSnapshot":
        first = inspect_roots(self.roots)
        self._temp = Path(tempfile.mkdtemp(prefix="qiqi-final-eval-"))
        try:
            base = self._temp
            for name, info in sorted(first.items()):
                # Registered repository names may contain filesystem separators.
                # Never derive a destination directory from untrusted names.
                destination = base / ("repo-" + hashlib.sha256(
                    name.encode("utf-8")).hexdigest()[:20])
                destination.mkdir(mode=0o700)
                root = self.roots[name]
                for item in info["files"]:
                    payload, mode = _read_file(root, item["path"])
                    if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                        raise RuntimeError("source changed during snapshot materialization")
                    target = destination / item["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as handle:
                        handle.write(payload)
                    # Preserve executable bits in this disposable working copy.
                    # --add-dir is directory access, not a read-only mount.
                    target.chmod(mode)
                # Evaluator can look up hashes by repository-relative path
                # without embedding thousands of file hashes in the prompt.
                (destination / ".qiqi-evaluation-manifest.json").write_text(
                    json.dumps({
                        "repository": name, "head": info["head"],
                        "files": info["files"],
                        "deleted_paths": info["deleted_paths"],
                    }, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                self.paths[name] = destination
            second = inspect_roots(self.roots)
            if first != second:
                raise RuntimeError("evaluation source changed while capturing snapshot")
            self.manifest = first
            self.digest = manifest_digest(first)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    @property
    def temporary_root(self) -> Path:
        """Absolute location of this active snapshot, for crash recovery."""
        if self._temp is None:
            raise RuntimeError("evaluation snapshot is not active")
        return self._temp

    def retain(self) -> Path:
        """Keep snapshot files when a native evaluator has not confirmed shutdown."""
        if self._temp is None:
            raise RuntimeError("evaluation snapshot is not active")
        self._retained = True
        return self._temp

    def __exit__(self, *_args: Any) -> None:
        if self._temp is not None and not self._retained:
            cleanup_temporary_tree(self._temp)
        self._temp = None
        self.paths = {}


def evidence_is_in_manifest(
    manifest: dict[str, Any], repository: str, path: str,
    sha256: str | None = None, *, kind: str = "file",
) -> bool:
    """Validate explicit file-hash or tracked-deletion evidence.

    Deletion tombstones are names from Git HEAD/index absent in the captured
    worktree, not files: they intentionally have no SHA256 content hash.
    They are bound to the same frozen manifest digest as all other evidence.
    """
    entry = manifest.get(repository)
    if not isinstance(entry, dict):
        return False
    if kind == "deleted":
        return sha256 is None and path in entry.get("deleted_paths", ())
    if kind != "file" or not isinstance(sha256, str):
        return False
    return any(
        row["path"] == path and row["sha256"] == sha256
        for row in entry.get("files", ())
    )
