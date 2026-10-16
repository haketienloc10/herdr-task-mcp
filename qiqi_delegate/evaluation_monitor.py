"""Fail-closed mutation event guard for final-evaluation snapshot inputs.

The SHA256 fingerprint taken before/after evaluation cannot detect an agent
that edits a file and restores its bytes before completing. Linux inotify
records write/attribute/create/delete/move events while the native worker is
running; the events remain queued until the Coordinator verifies them.

This is NOT a write sandbox or a substitute for OS-enforced isolation. Linux
inotify cannot observe every possible memory-mapped write, and a permissive
same-UID agent can still write outside the snapshot. Unsupported platforms
fail closed rather than silently relying on final-state hashing alone.
"""
from __future__ import annotations

import ctypes
import os
import stat
import sys
from pathlib import Path
from typing import Iterable

# inotify.h: never subscribe to IN_OPEN or IN_ACCESS (normal inspection).
_MUTATION_EVENTS = (
    0x00000002  # IN_MODIFY
    | 0x00000004  # IN_ATTRIB
    | 0x00000008  # IN_CLOSE_WRITE
    | 0x00000040  # IN_MOVED_FROM
    | 0x00000080  # IN_MOVED_TO
    | 0x00000100  # IN_CREATE
    | 0x00000200  # IN_DELETE
    | 0x00000400  # IN_DELETE_SELF
    | 0x00000800  # IN_MOVE_SELF
    | 0x00002000  # IN_UNMOUNT
    | 0x00004000  # IN_Q_OVERFLOW
    | 0x00008000  # IN_IGNORED
)
_ONLY_DIRECTORY = 0x01000000  # IN_ONLYDIR
_NO_FOLLOW = 0x02000000  # IN_DONT_FOLLOW


class SnapshotMutationMonitor:
    """Record all filesystem mutation events until native evaluation ends."""

    def __init__(self, paths: Iterable[Path]):
        self._roots = tuple(paths)
        self._fd: int | None = None

    def arm(self) -> None:
        if self._fd is not None:
            raise RuntimeError("snapshot mutation monitor already armed")
        if sys.platform != "linux":
            raise RuntimeError(
                "Final Evaluation requires a supported kernel snapshot "
                "mutation monitor (Linux inotify); refusing unmonitored evaluation"
            )
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            init = libc.inotify_init1
            watch = libc.inotify_add_watch
        except AttributeError as exc:
            raise RuntimeError("Linux inotify unavailable for Final Evaluation") from exc
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        watch.restype = ctypes.c_int
        fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, "cannot initialize evaluator snapshot mutation monitor")
        try:
            count = 0
            for root in self._roots:
                for directory, subdirs, _files in os.walk(root, followlinks=False):
                    # Symlinked directories are not inspectable evidence and
                    # must not silently bypass recursive mutation monitoring.
                    for subdir in subdirs:
                        entry = Path(directory) / subdir
                        if not stat.S_ISDIR(entry.lstat().st_mode):
                            raise RuntimeError(
                                "unsafe symlink in evaluator snapshot: " + str(entry)
                            )
                    wd = watch(
                        fd, os.fsencode(directory),
                        _MUTATION_EVENTS | _ONLY_DIRECTORY | _NO_FOLLOW,
                    )
                    if wd < 0:
                        err = ctypes.get_errno()
                        raise OSError(
                            err, "cannot watch evaluator snapshot: " + directory
                        )
                    count += 1
            if count == 0:
                raise RuntimeError("no evaluator snapshot directories were watched")
            self._fd = fd
        except BaseException:
            os.close(fd)
            raise

    def assert_clean(self) -> None:
        if self._fd is None:
            raise RuntimeError("snapshot mutation monitor not armed")
        try:
            events = os.read(self._fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            raise RuntimeError("evaluator snapshot mutation monitor failed") from exc
        if events:
            # Any event counts. Queue overflows, removed watches and changes
            # that were later reversed must all fail closed.
            raise RuntimeError(
                "evaluator snapshot changed during native execution "
                "(kernel mutation event observed)"
            )

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
