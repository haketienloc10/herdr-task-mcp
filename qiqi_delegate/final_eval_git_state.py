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


def _entry_fingerprint(root: Path, raw: bytes, *, allow_missing: bool) -> bytes:
    """Hash one exceptional Git entry, with its own unambiguous boundary."""
    path = root
    parts = Path(os.fsdecode(raw)).parts
    for part in parts[:-1]:
        path = path / part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            mode = 0
        if not stat.S_ISDIR(mode):
            if allow_missing and not mode:
                return b"missing-parent"
            raise RuntimeError("cannot fingerprint Git path under non-directory: "
                               + os.fsdecode(raw))
    path = path / parts[-1]
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return b"missing-entry"
        raise RuntimeError("untracked Git file vanished: " + os.fsdecode(raw))
    content = hashlib.sha256()
    if stat.S_ISLNK(info.st_mode):
        content.update(os.fsencode(os.readlink(path)))
    elif stat.S_ISREG(info.st_mode):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                content.update(block)
    else:
        content.update(str(info.st_rdev).encode("ascii"))
    return info.st_mode.to_bytes(8, "big") + content.digest()


def review_scope(roots: dict[str, Path]) -> dict[str, Any]:
    """Fingerprint Git diffs and untracked contents, without walking tracked files.

    This is a freshness check, not an immutable snapshot or an access sandbox.
    Git handles tracked symlinks; dirty submodules are fingerprinted recursively.
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
        gitlinks: list[bytes] = []
        for args in (
            ("diff", "--no-ext-diff", "--binary", "--cached", "--"),
            ("diff", "--no-ext-diff", "--binary", "--"),
            ("ls-files", "--stage", "-z"),
        ):
            payload = _git(root, *args)
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            if args[0] == "ls-files":
                # Mode 160000 is a Git submodule: a parent diff cannot
                # distinguish two different dirty states at the same HEAD.
                for record in payload.split(b"\0"):
                    header, separator, relative = record.partition(b"\t")
                    if separator and header.startswith(b"160000 "):
                        gitlinks.append(relative)
        for relative in sorted(set(gitlinks)):
            subroot = root / os.fsdecode(relative)
            # Reuse the same lightweight Git fingerprint for submodule roots
            # (including nested ones). Missing/uninitialized submodules fail
            # closed rather than approving a source the evaluator cannot read.
            sub = review_scope({"submodule": subroot})["submodule"]
            digest.update(b"gitlink\0")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update((sub["head"] or "").encode("ascii"))
            digest.update(bytes.fromhex(sub["fingerprint"]))
        # Git hides skip-worktree and assume-unchanged working-tree edits
        # from diff. Fingerprint only those exceptional tracked entries; don't
        # hash the entire tracked tree or reject normal sparse checkouts.
        tagged = _git(root, "ls-files", "-v", "-z")
        digest.update(len(tagged).to_bytes(8, "big"))
        digest.update(tagged)
        for entry in tagged.split(b"\0"):
            if not entry:
                continue
            flag, relative = entry[:1], entry[2:]
            if flag == b"S" or flag.islower():
                digest.update(len(relative).to_bytes(8, "big"))
                digest.update(relative)
                digest.update(_entry_fingerprint(root, relative, allow_missing=True))
        untracked = sorted(part for part in _git(
            root, "ls-files", "--others", "--exclude-standard", "-z",
        ).split(b"\0") if part)
        for raw in untracked:
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
            try:
                # A fixed-size per-file hash prevents adjacent file contents
                # from being misinterpreted as each other's record headers.
                digest.update(_entry_fingerprint(root, raw, allow_missing=False))
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
