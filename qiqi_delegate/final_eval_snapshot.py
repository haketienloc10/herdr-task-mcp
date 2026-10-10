"""Reproducible, bounded, isolated snapshots for graph-level final evaluation.

Do not mount original registered working trees in an Evaluator. Snapshot all
tracked and non-ignored untracked files, not just HEAD: Peer output may be dirty.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MAX_FILES = 3000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 24_000_000
FORBIDDEN_BASENAMES = {".env", ".env.local", ".env.production", ".npmrc",
                       ".pypirc", "id_rsa", "id_ed25519", "credentials.json"}
FORBIDDEN_PATH_PARTS = {".git", ".herdr-task-mcp", ".qiqi", ".ssh", ".aws",
                        ".venv", "node_modules", "__pycache__"}


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("unable to produce verified Git evaluation snapshot") from exc


def _files(root: Path) -> list[str]:
    # --cached includes staged deletions; absence on disk will fail closed.
    payload = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    names = sorted({part.decode("utf-8") for part in payload.split(b"\0") if part})
    if not names or len(names) > MAX_FILES:
        raise ValueError("evaluation snapshot has no files or exceeds file-count limit")
    for name in names:
        parts = Path(name).parts
        if (Path(name).is_absolute() or not parts or
                any(part in {"", ".", ".."} for part in parts) or
                any(part in FORBIDDEN_PATH_PARTS for part in parts) or
                Path(name).name in FORBIDDEN_BASENAMES or
                (Path(name).name.startswith(".env.") and Path(name).name != ".env.example")):
            raise ValueError("unsafe or sensitive file in evaluation snapshot: " + name)
    return names


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
        head = _git(root, "rev-parse", "HEAD").decode().strip()
        entries = []
        for relative in _files(root):
            data, mode = _read_file(root, relative)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("evaluation snapshot exceeds aggregate byte limit")
            entries.append({
                "path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data), "mode": mode,
            })
        manifest[name] = {"root": str(root), "head": head, "files": entries}
    return manifest


def manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        manifest, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class EvaluationSnapshot:
    """Temporary immutable-input snapshot; all evaluator writes go to a copy."""

    def __init__(self, roots: dict[str, Path]):
        self.roots = roots
        self._temp: tempfile.TemporaryDirectory[str] | None = None
        self.paths: dict[str, Path] = {}
        self.manifest: dict[str, Any] = {}
        self.digest = ""

    def __enter__(self) -> "EvaluationSnapshot":
        first = inspect_roots(self.roots)
        self._temp = tempfile.TemporaryDirectory(prefix="qiqi-final-eval-")
        try:
            base = Path(self._temp.name)
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
                    # Read-only input snapshot; sandboxed tests may write to
                    # separate ephemeral artifact directories.
                    target.chmod(0o444 if mode & 0o111 == 0 else 0o555)
                # Evaluator can look up hashes by repository-relative path
                # without embedding thousands of file hashes in the prompt.
                (destination / ".qiqi-evaluation-manifest.json").write_text(
                    json.dumps({
                        "repository": name, "head": info["head"],
                        "files": info["files"],
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

    def __exit__(self, *_args: Any) -> None:
        if self._temp:
            self._temp.cleanup()
        self._temp = None
        self.paths = {}


def evidence_is_in_manifest(
    manifest: dict[str, Any], repository: str, path: str, sha256: str,
) -> bool:
    entry = manifest.get(repository)
    return bool(entry and any(
        row["path"] == path and row["sha256"] == sha256
        for row in entry["files"]
    ))
