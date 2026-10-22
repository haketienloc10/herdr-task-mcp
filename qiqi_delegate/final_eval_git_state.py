"""Lightweight Git identity for native Final Evaluation.

The evaluator reads live registered roots via --add-dir. QiQi records enough
Git state to reject a stale PASS, not a per-file proof or source archive.
"""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Any


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("cannot inspect evaluator Git root: " + str(root)) from exc


def review_scope(roots: dict[str, Path]) -> dict[str, Any]:
    """Fingerprint Git diffs and untracked contents, without walking tracked files.

    This is a freshness check, not an immutable snapshot or an access sandbox.
    Git handles tracked symlinks; only the link text is hashed for untracked links.
    """
    if not roots:
        raise ValueError("evaluation needs at least one repository")
    scope: dict[str, Any] = {}
    for name, root in sorted(roots.items()):
        if not root.is_dir() or root.resolve() != root:
            raise ValueError("evaluation repository root must be canonical")
        top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").strip())).resolve()
        if top != root:
            raise ValueError("evaluation root is not the exact Git root: " + str(root))
        head = _git(root, "rev-parse", "--verify", "HEAD") if (
            subprocess.run(["git", "-C", str(root), "rev-parse", "-q", "--verify", "HEAD"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=30).returncode == 0
        ) else b""
        digest = hashlib.sha256()
        # Index and working-tree changes are represented by Git's own diff,
        # including renames, staged edits, deletions and symlink target changes.
        for args in (
            ("diff", "--no-ext-diff", "--binary", "--cached", "--"),
            ("diff", "--no-ext-diff", "--binary", "--"),
            ("ls-files", "--stage", "-z"),
        ):
            payload = _git(root, *args)
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
        untracked = sorted(part for part in _git(
            root, "ls-files", "--others", "--exclude-standard", "-z",
        ).split(b"\0") if part)
        for raw in untracked:
            path = root / os.fsdecode(raw)
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
            try:
                info = path.lstat()
                digest.update(info.st_mode.to_bytes(8, "big"))
                if stat.S_ISLNK(info.st_mode):
                    digest.update(os.fsencode(os.readlink(path)))
                elif stat.S_ISREG(info.st_mode):
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    fd = os.open(path, flags)
                    with os.fdopen(fd, "rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                else:
                    digest.update(str(info.st_rdev).encode("ascii"))
            except OSError as exc:
                raise RuntimeError("untracked Git source changed during fingerprint: "
                                   + os.fsdecode(raw)) from exc
        deleted = _git(root, "diff", "--name-only", "--diff-filter=D", "-z", "HEAD", "--"
                       ).split(b"\0") if head else []
        scope[name] = {
            "root": str(root),
            "head": head.decode("ascii").strip() or None,
            "fingerprint": digest.hexdigest(),
            "untracked_paths": [os.fsdecode(x) for x in untracked],
            "deleted_paths": [os.fsdecode(x) for x in deleted if x],
        }
    return scope
