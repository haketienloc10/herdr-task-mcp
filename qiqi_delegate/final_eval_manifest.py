"""Git worktree inspection and evidence verification for direct native evaluation.

Never create a repository copy; hash live registered roots before and after review.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any


FORBIDDEN_BASENAMES = {".env", ".env.local", ".env.production", ".npmrc",
                       ".pypirc", "id_rsa", "id_ed25519", "credentials.json"}
FORBIDDEN_PATH_PARTS = {".git", ".herdr-task-mcp", ".qiqi", ".ssh", ".aws",
                        ".venv", "node_modules", "__pycache__"}
# Generated metadata is placed inside isolated repository copies. Collision
# with a real tracked/untracked product file would silently overwrite bytes
# while leaving the original SHA256 recorded in the evaluation manifest.
RESERVED_EVALUATION_PATHS: set[str] = set()


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("unable to produce verified Git evaluation snapshot") from exc


def _git_paths(root: Path, *args: str) -> set[str]:
    return {
        part.decode("utf-8")
        for part in _git(root, *args).split(b"\0")
        if part
    }


def _reject_gitlinks(root: Path, head: str | None) -> None:
    """Fail closed for Git submodules instead of interpreting dirs as deletions.

    Git records a submodule as mode 160000 (a gitlink). The submodule's
    populated working tree and its own dirty changes are outside the normal
    ls-files manifest; a digest would otherwise falsely remain unchanged.
    Check BOTH the index and HEAD so staged removal also fails closed.
    """
    for label, payload in [
        ("index", _git(root, "ls-files", "--stage", "-z")),
        ("HEAD", _git(root, "ls-tree", "-r", "-z", head) if head else b""),
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


def _reject_sparse_checkout(root: Path) -> None:
    """Fail closed when tracked omissions cannot be distinguished from deletions.

    Sparse checkout intentionally excludes paths that are still part of the
    product. Git represents those index entries with the skip-worktree flag.
    Do not issue deletion tombstones for them: the original source was never
    inspected, and a deletion-only report could otherwise receive false PASS.
    Both checks matter: manual --skip-worktree can be set without sparse mode,
    and sparse mode may hide an entire sparse-index directory or a staged
    deletion may have removed an entry from the index.
    """
    try:
        mode = subprocess.run(
            ["git", "-C", str(root), "config", "--bool", "--get",
             "core.sparseCheckout"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
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
    for record in _git(root, "ls-files", "-t", "-z").split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise RuntimeError("cannot verify tracked Git index flags")
        if record[:1] == b"S":
            raise ValueError(
                "evaluation snapshot refuses skip-worktree tracked path: "
                + record[2:].decode("utf-8")
            )


def _files(root: Path, head: str | None) -> tuple[list[str], set[str], set[str]]:
    """List current files *and* tracked paths removed from the worktree.

    Git's cached index retains unstaged deletions, whereas staged deletions
    vanish from the index. Include HEAD, index and untracked paths to capture
    current files, but accept deletions ONLY for paths present in HEAD.
    Index-only absent paths (intent-to-add or a staged addition subsequently
    removed from the worktree) have no previous committed content and must
    never become trusted deletion evidence.
    """
    _reject_gitlinks(root, head)
    _reject_sparse_checkout(root)
    tracked = _git_paths(root, "ls-files", "--cached", "-z")
    head_paths = (
        _git_paths(root, "ls-tree", "-r", "--name-only", "-z", head)
        if head is not None else set()
    )
    tracked.update(head_paths)
    others = _git_paths(
        root, "ls-files", "--others", "--exclude-standard", "-z",
    )
    names = sorted(tracked | others)
    if not names:
        raise ValueError("evaluation source has no files")
    for name in names:
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


def _hash_file(root: Path, relative: str) -> tuple[str, int, int]:
    """Stream a live file without copying it; detect concurrent in-place mutation."""
    path = root
    for component in Path(relative).parts:
        path = path / component
        if stat.S_ISLNK(path.lstat().st_mode):
            raise ValueError("evaluation refuses symlink: " + relative)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("evaluation source is not a regular file: " + relative)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("evaluation file changed while opening: " + relative)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb", closefd=False) as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
        before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                              after.st_mtime_ns, after.st_ctime_ns) or size != after.st_size:
        raise RuntimeError("evaluation source changed during inspection: " + relative)
    return digest.hexdigest(), size, stat.S_IMODE(after.st_mode)


def inspect_roots(roots: dict[str, Path]) -> dict[str, Any]:
    """Pure inspection: deterministic manifest includes uncommitted and untracked files."""
    if not roots:
        raise ValueError("evaluation needs at least one repository")
    manifest: dict[str, Any] = {}
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
                sha256, size, mode = _hash_file(root, relative)
            except (FileNotFoundError, NotADirectoryError) as exc:
                raise RuntimeError(
                    "evaluation file disappeared during snapshot: " + relative
                ) from exc
            entries.append({
                "path": relative, "sha256": sha256, "size": size, "mode": mode,
            })
        manifest[name] = {
            "root": str(root), "head": head,
            "files": entries, "deleted_paths": deleted_paths,
        }
    return manifest


def manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        manifest, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


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
