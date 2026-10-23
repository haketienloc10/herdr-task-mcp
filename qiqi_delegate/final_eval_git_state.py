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


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    # Ignore FSMonitor reports and honor ctime even if the repository disables it.
    # Per-command overrides do not modify repository configuration.
    try:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.trustctime=true",
             "-C", str(root), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=stdin, timeout=60,
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
        # Git can report an untracked embedded repository as one directory.
        # Directory device metadata is not a fingerprint of its contents.
        raise ValueError(
            "non-file Git entry cannot be safely fingerprinted: " + os.fsdecode(raw)
        )
    return info.st_mode.to_bytes(8, "big") + content.digest()


def _tracked_mode(root: Path, raw: bytes) -> bytes:
    """Capture worktree file type and permission bits, never file contents.

    Sparse/index-only paths can be absent. Do not traverse symlinked parent
    directories when examining a tracked path.
    """
    path = root
    parts = Path(os.fsdecode(raw)).parts
    for part in parts[:-1]:
        path = path / part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return b"missing-parent"
        if not stat.S_ISDIR(mode):
            raise RuntimeError("tracked Git path has a non-directory parent: "
                               + os.fsdecode(raw))
    try:
        mode = (path / parts[-1]).lstat().st_mode
    except FileNotFoundError:
        return b"missing-entry"
    return mode.to_bytes(8, "big")


def review_scope(roots: dict[str, Path]) -> dict[str, Any]:
    """Fingerprint Git diffs, tracked worktree modes and untracked contents.

    This is a freshness check, not an immutable snapshot or an access sandbox.
    No tracked file contents are read except for exceptional index flags.
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
        # With core.fileMode=false, Git ignores executable-bit changes.
        # Reject this configuration rather than stat() every tracked file.
        filemode = subprocess.run(
            ["git", "-C", str(root), "config", "--bool", "--get", "core.fileMode"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if filemode.returncode not in (0, 1):
            raise RuntimeError("cannot inspect Git core.fileMode: " + str(root))
        if filemode.stdout.strip() == b"false":
            raise ValueError("Git core.fileMode=false hides source mode changes: "
                             + str(root))
        # Under core.symlinks=false, Git treats a mode-120000 checkout as
        # text containing the link target. Switching between a real symlink
        # and a plain file can then leave Git's diff unchanged.
        symlinks = subprocess.run(
            ["git", "-C", str(root), "config", "--bool", "--get", "core.symlinks"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if symlinks.returncode not in (0, 1):
            raise RuntimeError("cannot inspect Git core.symlinks: " + str(root))
        if symlinks.stdout.strip() == b"false":
            raise ValueError("Git core.symlinks=false hides tracked symlink changes: "
                             + str(root))
        autocrlf = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "core.autocrlf"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if autocrlf.returncode not in (0, 1):
            raise RuntimeError("cannot inspect Git core.autocrlf: " + str(root))
        if autocrlf.stdout.strip().lower() not in (b"", b"false"):
            raise ValueError("Git core.autocrlf may hide source line-ending changes: "
                             + str(root))
        head = _git(root, "rev-parse", "--verify", "HEAD") if (
            subprocess.run(["git", "-C", str(root), "rev-parse", "-q", "--verify", "HEAD"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=30).returncode == 0
        ) else b""
        digest = hashlib.sha256()
        # Git hides skip-worktree and assume-unchanged working-tree edits
        # from diff. Fingerprint only those exceptional tracked entries; don't
        # hash the entire tracked tree or reject normal sparse checkouts.
        tagged = _git(root, "ls-files", "-v", "-z")
        digest.update(len(tagged).to_bytes(8, "big"))
        digest.update(tagged)
        entries = [entry for entry in tagged.split(b"\0") if entry]
        # Git tracks only executable-mode transitions for regular files.
        # Inspect metadata (not bytes) for every tracked worktree entry so
        # chmod 0600->0644, type changes, and missing sparse entries are
        # reflected without a per-file source hash or directory walk.
        for entry in entries:
            relative = entry[2:]
            mode = _tracked_mode(root, relative)
            digest.update(len(mode).to_bytes(8, "big"))
            digest.update(mode)
        # Git diffs compare canonicalized content. Check-in attributes may
        # hide raw working-tree changes even if the index/diff stay identical.
        # Reject them instead of hashing all tracked worktree contents.
        conversions = ("filter", "text", "eol", "ident", "working-tree-encoding", "crlf")
        if entries:
            tracked_paths = b"\0".join(entry[2:] for entry in entries) + b"\0"
            attrs = _git(root, "check-attr", "-z", "--stdin", *conversions,
                         stdin=tracked_paths).split(b"\0")
            if (attrs[-1:] != [b""] or
                    len(attrs[:-1]) != 3 * len(entries) * len(conversions)):
                raise RuntimeError("invalid Git conversion attribute inspection")
            for i in range(0, len(attrs) - 1, 3):
                if (attrs[i + 1] not in tuple(x.encode("ascii") for x in conversions)
                        or attrs[i + 2] not in (b"unspecified", b"unset")):
                    raise ValueError(
                        "Git clean filter/check-in conversion may hide source changes: "
                        + os.fsdecode(attrs[i]) + " (" +
                        os.fsdecode(attrs[i + 1]) + ")"
                    )
        # Perform potentially executable Git worktree diff only *after*
        # checking all tracked attributes. A configured clean filter can run
        # arbitrary code during diff, even if the evaluation later rejects it.
        # Index and working-tree changes are represented by Git's own diff,
        # including renames, staged edits, deletions and symlink target changes.
        gitlinks: list[bytes] = []
        for args in (
            ("diff", "--no-ext-diff", "--no-textconv", "--binary", "--cached", "--"),
            ("diff", "--no-ext-diff", "--no-textconv", "--binary", "--"),
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
        missing_assumed: list[bytes] = []
        for entry in entries:
            if not entry:
                continue
            flag, relative = entry[:1], entry[2:]
            if flag == b"S" or flag.islower():
                digest.update(len(relative).to_bytes(8, "big"))
                digest.update(relative)
                state = _entry_fingerprint(root, relative, allow_missing=True)
                digest.update(state)
                # Git does not report deletions of assume-unchanged files.
                # Missing skip-worktree ('S'/'s') entries, however, can be
                # intentional sparse-checkout omissions, not deletions.
                if (flag.islower() and flag != b"s" and
                        state in (b"missing-entry", b"missing-parent")):
                    missing_assumed.append(relative)
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
        deleted = set(_git(
            root, "diff", "--no-renames", "--name-only", "--diff-filter=D",
            "-z", "HEAD", "--",
        ).split(b"\0")) if head else set()
        if missing_assumed and head:
            # Only HEAD-tracked paths are legitimate deletion tombstones;
            # a newly staged file later removed was never part of HEAD.
            head_paths = set(_git(
                root, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--",
            ).split(b"\0"))
            deleted.update(path for path in missing_assumed if path in head_paths)
        scope[name] = {
            "root": str(root),
            "head": head.decode("ascii").strip() or None,
            "fingerprint": digest.hexdigest(),
            "untracked_paths": [os.fsdecode(x) for x in untracked],
            "deleted_paths": [os.fsdecode(x) for x in sorted(deleted) if x],
        }
    return scope
