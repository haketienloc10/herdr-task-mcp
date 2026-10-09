#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from qiqi_delegate.core import active_capture_filename, normalize_hook_payload


def _write_event(sink: Path, event: dict[str, object]) -> None:
    sink.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(sink, 0o700)
    except OSError:
        pass
    name = f"event-{event['captured_at_ns']}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
    destination = sink / name
    temp = sink / f".{name}.tmp"
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with temp.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, destination)


def _hook_failure_event(
    *, adapter: str, nonce: str, payload: object, exc: Exception
) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    session_id = source.get("session_id")
    native_turn_id = source.get("turn_id")
    cwd = source.get("cwd")
    hook_event = source.get("hook_event_name")
    return {
        "version": 1,
        "adapter": adapter,
        "nonce": nonce,
        "hook_event": hook_event if isinstance(hook_event, str) else "HookFailure",
        "state": "capture_error",
        "session_id": session_id if isinstance(session_id, str) else None,
        "native_turn_id": native_turn_id if isinstance(native_turn_id, str) else None,
        "agent_response": None,
        "error": f"{type(exc).__name__}: {exc}",
        "cwd": cwd if isinstance(cwd, str) else None,
        "background_task_count": 0,
        "hook_failure": True,
        "captured_at_ns": time.time_ns(),
    }


def _active_capture_path(state_root: Path, adapter: str, cwd: str) -> Path:
    return state_root / "active-captures" / active_capture_filename(adapter, Path(cwd))


def _load_active_capture(
    state_root: Path, adapter: str, payload: dict[str, object]
) -> tuple[Path, str]:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("hook payload is missing cwd required for active-capture routing")
    repo = Path(cwd).resolve()
    path = _active_capture_path(state_root, adapter, str(repo))
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("active-capture descriptor is invalid")
    if raw.get("adapter") != adapter:
        raise ValueError("active-capture adapter mismatch")
    if raw.get("repo") != str(repo):
        raise ValueError("active-capture repository mismatch")
    sink_value = raw.get("sink")
    nonce = raw.get("nonce")
    if not isinstance(sink_value, str) or not sink_value:
        raise ValueError("active-capture descriptor has no sink")
    if not isinstance(nonce, str) or not nonce:
        raise ValueError("active-capture descriptor has no nonce")
    expected_session_id = raw.get("expected_session_id")
    actual_session_id = payload.get("session_id")
    if expected_session_id is not None:
        if not isinstance(expected_session_id, str) or not expected_session_id:
            raise ValueError("active-capture expected_session_id is invalid")
        if actual_session_id != expected_session_id:
            raise ValueError("active-capture native session mismatch")
    return Path(sink_value), nonce


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", required=True, choices=("claude", "codex"))
    parser.add_argument("--state-root")
    parser.add_argument("--sink")
    parser.add_argument("--nonce")
    args = parser.parse_args()

    payload: object = None
    direct_sink = Path(args.sink) if args.sink else None
    direct_nonce = args.nonce
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")

        if args.state_root:
            if args.sink or args.nonce:
                raise ValueError("--state-root cannot be combined with --sink/--nonce")
            sink, nonce = _load_active_capture(
                Path(args.state_root).resolve(), args.adapter, payload
            )
        else:
            if direct_sink is None or not direct_nonce:
                raise ValueError("provide --state-root or both --sink and --nonce")
            sink = direct_sink
            nonce = direct_nonce

        event = normalize_hook_payload(
            adapter=args.adapter,
            nonce=nonce,
            payload=payload,
        )
        _write_event(sink, event)
    except Exception as exc:
        # Production delegation passes a per-turn sink/nonce directly. Preserve the
        # hook failure there so qiqi_delegate can report the real cause instead of
        # degrading it into a generic no-capture timeout. Static state-root mode is
        # retained only as a compatibility path and may not know a sink on failure.
        if direct_sink is not None and direct_nonce:
            try:
                _write_event(
                    direct_sink,
                    _hook_failure_event(
                        adapter=args.adapter,
                        nonce=direct_nonce,
                        payload=payload,
                        exc=exc,
                    ),
                )
            except Exception as persist_exc:
                print(
                    f"qiqi result capture diagnostic write failed: {persist_exc}",
                    file=sys.stderr,
                )
        # Result capture must never make the native Stop hook continue/block a turn.
        print(f"qiqi result capture failed: {exc}", file=sys.stderr)
        print("{}")
        return 0

    # Both Claude and Codex accept an empty decision object from a successful Stop hook.
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
