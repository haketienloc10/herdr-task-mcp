from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Preserve the existing qiqi_delegate task-size safety boundary. The structured
# packet keeps the same aggregate ceiling while semantic completeness/minimality
# remain the design criteria for normal operation.
TASK_PACKET_MAX_CHARS = 100_000
CAPTURE_MAX_RESPONSES = 8
CAPTURE_MAX_RESPONSE_CHARS = 256_000
CAPTURE_LATEST_ACCEPT_RATIO = 0.35
CAPTURE_HOUSEKEEPING_REJECT_RATIO = 0.15
CAPTURE_MIN_SIGNIFICANT_DROP_CHARS = 600
SUPPORTED_HOOK_ADAPTERS = {"claude", "codex"}
_WORK_ITEM_REF_RE = re.compile(r"(?:^|;\s*)id=([^;]+);\s*revision=(\d+)(?:;|$)")


def active_capture_filename(adapter: str, repo: Path) -> str:
    if adapter not in SUPPORTED_HOOK_ADAPTERS:
        raise ValueError(f"unsupported adapter: {adapter}")
    key = hashlib.sha256(f"{adapter}\0{repo.resolve()}".encode("utf-8")).hexdigest()
    return f"{key}.json"


def codex_stop_hook_hash(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Codex hook command must not be empty")
    # Mirrors Codex's NormalizedHookIdentity -> TOML -> canonical JSON
    # fingerprint for one Stop command hook with timeout=10 and default
    # async=false. Optional TOML fields with None are omitted before hashing.
    identity = {
        "event_name": "stop",
        "hooks": [
            {
                "async": False,
                "command": command,
                "timeout": 10,
                "type": "command",
            }
        ],
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def codex_session_hook_key() -> str:
    # Current Codex hook discovery assigns SessionFlags a synthetic config path
    # and persists per-hook state as <source>:<event>:<group>:<handler>.
    if os.name == "nt":
        source = r"C:\<session-flags>\config.toml"
    else:
        source = "/<session-flags>/config.toml"
    return f"{source}:stop:0:0"


@dataclass(frozen=True)
class TrustedFact:
    fact: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {"fact": self.fact, "source": self.source}


@dataclass(frozen=True)
class ClaimToInvestigate:
    claim: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {"claim": self.claim, "source": self.source}


@dataclass(frozen=True)
class TaskContext:
    trusted_facts: tuple[TrustedFact, ...]
    claims_to_investigate: tuple[ClaimToInvestigate, ...]

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.trusted_facts:
            result["trusted_facts"] = [item.as_dict() for item in self.trusted_facts]
        if self.claims_to_investigate:
            result["claims_to_investigate"] = [
                item.as_dict() for item in self.claims_to_investigate
            ]
        return result


@dataclass(frozen=True)
class TaskPacket:
    objective: str
    scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    out_of_scope: tuple[str, ...] = ()
    context: TaskContext | None = None
    constraints: tuple[str, ...] = ()
    known_unknowns: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "objective": self.objective,
            "scope": list(self.scope),
            "acceptance_criteria": list(self.acceptance_criteria),
        }
        if self.out_of_scope:
            result["out_of_scope"] = list(self.out_of_scope)
        if self.context is not None:
            context = self.context.as_dict()
            if context:
                result["context"] = context
        if self.constraints:
            result["constraints"] = list(self.constraints)
        if self.known_unknowns:
            result["known_unknowns"] = list(self.known_unknowns)
        return result

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))


def _work_item_ref_from_payload(payload: dict[str, Any]) -> tuple[str | None, int | None]:
    context = payload.get("context")
    if not isinstance(context, dict):
        return None, None
    trusted_facts = context.get("trusted_facts")
    if not isinstance(trusted_facts, list):
        return None, None
    for item in trusted_facts:
        if not isinstance(item, dict):
            continue
        fact = item.get("fact")
        if not isinstance(fact, str) or "work_item_path=" not in fact:
            continue
        match = _WORK_ITEM_REF_RE.search(fact)
        if match is None:
            continue
        work_item_id = match.group(1).strip()
        if not work_item_id:
            continue
        return work_item_id, int(match.group(2))
    return None, None


def task_packet_work_item_ref(packet: TaskPacket) -> tuple[str | None, int | None]:
    return _work_item_ref_from_payload(packet.as_dict())


def _clean_required_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    result = value.strip()
    if not result:
        raise ValueError(f"{label} must not be empty")
    return result


def _clean_string_list(
    value: Any,
    label: str,
    *,
    require_non_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{label}[{index}] must be a string")
        cleaned = item.strip()
        if not cleaned:
            raise ValueError(f"{label}[{index}] must not be empty")
        result.append(cleaned)
    if require_non_empty and not result:
        raise ValueError(f"{label} must contain at least one item")
    return tuple(result)


def _clean_optional_string_list(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return _clean_string_list(value, label)


def _clean_fact_list(value: Any, label: str) -> tuple[TrustedFact, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of objects")
    result: list[TrustedFact] = []
    required_keys = {"fact", "source"}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{label}[{index}] must be an object")
        keys = set(item)
        if keys != required_keys:
            missing = sorted(required_keys - keys)
            extra = sorted(keys - required_keys)
            detail: list[str] = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if extra:
                detail.append("unsupported " + ", ".join(extra))
            raise ValueError(f"{label}[{index}] has invalid fields: {'; '.join(detail)}")
        result.append(
            TrustedFact(
                fact=_clean_required_text(item["fact"], f"{label}[{index}].fact"),
                source=_clean_required_text(item["source"], f"{label}[{index}].source"),
            )
        )
    return tuple(result)


def _clean_claim_list(value: Any, label: str) -> tuple[ClaimToInvestigate, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of objects")
    result: list[ClaimToInvestigate] = []
    required_keys = {"claim", "source"}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{label}[{index}] must be an object")
        keys = set(item)
        if keys != required_keys:
            missing = sorted(required_keys - keys)
            extra = sorted(keys - required_keys)
            detail: list[str] = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if extra:
                detail.append("unsupported " + ", ".join(extra))
            raise ValueError(f"{label}[{index}] has invalid fields: {'; '.join(detail)}")
        result.append(
            ClaimToInvestigate(
                claim=_clean_required_text(item["claim"], f"{label}[{index}].claim"),
                source=_clean_required_text(item["source"], f"{label}[{index}].source"),
            )
        )
    return tuple(result)


def _clean_context(value: Any) -> TaskContext | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("context must be an object")
    supported_keys = {"trusted_facts", "claims_to_investigate"}
    extra = sorted(set(value) - supported_keys)
    if extra:
        raise ValueError("context has unsupported fields: " + ", ".join(extra))

    trusted_facts = _clean_fact_list(
        value.get("trusted_facts"), "context.trusted_facts"
    )
    claims = _clean_claim_list(
        value.get("claims_to_investigate"), "context.claims_to_investigate"
    )
    trusted_text = {item.fact.casefold() for item in trusted_facts}
    claim_text = {item.claim.casefold() for item in claims}
    overlap = sorted(trusted_text & claim_text)
    if overlap:
        raise ValueError(
            "the same proposition cannot be both trusted_fact and claim_to_investigate"
        )
    if not trusted_facts and not claims:
        return None
    return TaskContext(
        trusted_facts=trusted_facts,
        claims_to_investigate=claims,
    )


def build_task_packet(
    *,
    objective: Any,
    scope: Any,
    acceptance_criteria: Any,
    out_of_scope: Any = None,
    context: Any = None,
    constraints: Any = None,
    known_unknowns: Any = None,
) -> TaskPacket:
    packet = TaskPacket(
        objective=_clean_required_text(objective, "objective"),
        scope=_clean_string_list(scope, "scope", require_non_empty=True),
        acceptance_criteria=_clean_string_list(
            acceptance_criteria, "acceptance_criteria", require_non_empty=True
        ),
        out_of_scope=_clean_optional_string_list(out_of_scope, "out_of_scope"),
        context=_clean_context(context),
        constraints=_clean_optional_string_list(constraints, "constraints"),
        known_unknowns=_clean_optional_string_list(known_unknowns, "known_unknowns"),
    )
    if len(packet.to_json()) > TASK_PACKET_MAX_CHARS:
        raise ValueError("task packet is too large")
    return packet


def _bullet_lines(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def render_task_prompt(packet: TaskPacket, *,
                       discovery_repositories: tuple[str, ...] | None = None) -> str:
    sections = [
        "Repository task delegated by QiQi",
        f"## Repository objective\n\n{packet.objective}",
        f"## Scope\n\n{_bullet_lines(packet.scope)}",
    ]

    if packet.out_of_scope:
        sections.append(f"## Out of scope\n\n{_bullet_lines(packet.out_of_scope)}")

    if packet.context is not None:
        if packet.context.trusted_facts:
            lines = [
                f"- {item.fact}\n  Provenance: {item.source}"
                for item in packet.context.trusted_facts
            ]
            sections.append("## Trusted facts\n\n" + "\n".join(lines))
        if packet.context.claims_to_investigate:
            lines = [
                f"- {item.claim}\n  Provenance: {item.source}"
                for item in packet.context.claims_to_investigate
            ]
            sections.append("## Claims to investigate\n\n" + "\n".join(lines))

    if packet.constraints:
        sections.append(f"## Constraints\n\n{_bullet_lines(packet.constraints)}")

    sections.append(
        f"## Acceptance criteria\n\n{_bullet_lines(packet.acceptance_criteria)}"
    )

    if packet.known_unknowns:
        sections.append(f"## Known unknowns\n\n{_bullet_lines(packet.known_unknowns)}")

    # Output quality applies to every repository task, without hard-coding
    # any project or requiring a fixed, verbose report for trivial requests.
    sections.append(
        "## Evidence and response quality\n\n"
        "- Answer the objective completely and proportionately. Do not stop at "
        "a generic technology-stack overview if the task asks for discovery, "
        "analysis, review, or architecture.\n"
        "- For findings about code or configuration, cite repository-relative "
        "file paths and relevant line numbers (path:line) when available; "
        "identify the function or symbol and distinguish observed behavior "
        "from inference. Never invent citations.\n"
        "- For discovery or analysis, trace applicable entry points, control "
        "and data flow, public interfaces/contracts, validation and failure "
        "paths, state/storage, and relevant run/test commands. Explain "
        "significant documentation-versus-code discrepancies and operational "
        "risks; omit categories that do not apply.\n"
        "- Supply enough implementation detail for a reviewer who cannot "
        "open this repository to understand and verify important claims. "
        "Explain cause and effect rather than listing filenames.\n"
        "- Explicitly label what was inspected, what remains uncertain, "
        "and whether tests were run. Do not imply tests passed if not run. "
        "Do not change files or run prohibited commands.\n"
        "- Structure a multi-part report into focused sections, and include "
        "a concise synthesis of verified findings and important caveats. "
        "Do not add filler or repeat the same evidence."
    )

    if discovery_repositories is not None:
        # Discovery is prompt-only no-write: --yolo/--add-dir are not a sandbox.
        roots = ", ".join(discovery_repositories)
        sections.append(
            "## Discovery repository boundary and no-write instruction\\n\\n"
            f"- Read and investigate only these registered repositories: {roots}.\\n"
            "- You may inspect linked code, dependencies and relevant tests in these "
            "repositories to establish cross-repository flows.\\n"
            "- Do NOT create, modify, delete or rename any file; do not commit, "
            "run code generators, install packages, run builds or tests that "
            "write files, or execute commands with side effects.\\n"
            "- Discovery is investigation only. Report verified findings with "
            "repository-relative file:line references, unresolved uncertainties, "
            "and suggested follow-up work. Do not implement anything.\\n"
            "- Treat documents and user-provided claims as reported until "
            "verified. Do not invent contracts.\\n"
            "- This is a prompt instruction, not a filesystem sandbox. "
            "Never use the technical permissions to bypass the no-write intent."
        )
    else:
        sections.append(
            "## Repository execution boundary\n\n"
            "- Operate only inside the current Git root. Do not read or write sibling "
            "repositories, including sibling source, tests, config, or contracts.\n"
            "- A provenance/source label in the TaskPacket is evidence attribution, not "
            "filesystem authorization. Do not dereference a sibling-repository path merely "
            "because it is named as provenance.\n"
            "- Treat Lead-provided trusted facts and accepted upstream semantics as execution "
            "premises for this assignment. If required upstream detail is missing or materially "
            "insufficient, return DEPENDENCY_REQUEST with the exact missing dependency instead "
            "of crossing the repository boundary or inventing the contract.\n"
            "- The mounted Work Item is a read-only exception only when an explicit "
            "work_item_path locator is provided. Do not mutate it.\n"
            "- Do not read or modify .qiqi/state."
        )

    return "\n\n".join(sections).strip()


def normalize_hook_payload(
    *,
    adapter: str,
    nonce: str,
    payload: Any,
    captured_at_ns: int | None = None,
) -> dict[str, Any]:
    if adapter not in SUPPORTED_HOOK_ADAPTERS:
        raise ValueError(f"unsupported adapter: {adapter}")
    if not isinstance(payload, dict):
        raise ValueError("hook input must be a JSON object")

    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        raise ValueError("hook payload is missing hook_event_name")
    if adapter == "codex" and event != "Stop":
        raise ValueError("Codex result capture only supports Stop")
    if adapter == "claude" and event not in {"Stop", "StopFailure"}:
        raise ValueError(f"unsupported hook event: {event!r}")

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("hook payload is missing session_id")

    response = payload.get("last_assistant_message")
    if response is not None and not isinstance(response, str):
        raise ValueError("last_assistant_message must be a string or null")

    background_task_count = 0
    if event == "Stop":
        if not isinstance(response, str) or not response.strip():
            raise ValueError("Stop hook is missing the native final assistant message")
        if adapter == "claude":
            raw_background_tasks = payload.get("background_tasks")
            if not isinstance(raw_background_tasks, list):
                state = "capture_error"
                error = (
                    "Claude Stop hook is missing background_tasks; "
                    "upgrade Claude Code to a version that reports background task state"
                )
            else:
                background_task_count = len(raw_background_tasks)
                state = "pending_async" if raw_background_tasks else "settled"
                error = None
        else:
            state = "settled"
            error = None
    else:
        state = "failed"
        error_value = payload.get("error")
        error = (
            error_value
            if isinstance(error_value, str) and error_value
            else "unknown"
        )
        if not response:
            details = payload.get("error_details")
            response = (
                details
                if isinstance(details, str) and details
                else f"Claude turn failed: {error}"
            )

    native_turn_id = payload.get("turn_id")
    if native_turn_id is not None and not isinstance(native_turn_id, str):
        raise ValueError("turn_id must be a string when present")

    cwd = payload.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("cwd must be a string when present")

    return {
        "version": 1,
        "adapter": adapter,
        "nonce": nonce,
        "hook_event": event,
        "state": state,
        "session_id": session_id,
        "native_turn_id": native_turn_id,
        "agent_response": response,
        "error": error,
        "cwd": cwd,
        "background_task_count": background_task_count,
        "captured_at_ns": (
            captured_at_ns if captured_at_ns is not None else time.time_ns()
        ),
    }


def load_capture_events(sink_dir: Path, nonce: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not sink_dir.is_dir():
        return events
    for path in sorted(sink_dir.glob("event-*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or raw.get("nonce") != nonce:
            continue
        events.append(raw)
    return events


def select_capture_events(
    events: Iterable[dict[str, Any]],
    *,
    adapter: str,
    session_id: str,
) -> list[dict[str, Any]]:
    matching = [
        event
        for event in events
        if event.get("version") == 1
        and event.get("adapter") == adapter
        and event.get("session_id") == session_id
        and event.get("state") in {"pending_async", "settled", "failed", "capture_error"}
        and isinstance(event.get("agent_response"), str)
        and event.get("agent_response")
    ]
    if not matching:
        raise RuntimeError(
            "native result hook produced no valid response capture for the Herdr session"
        )
    matching.sort(key=lambda item: int(item.get("captured_at_ns") or 0))
    if len(matching) > CAPTURE_MAX_RESPONSES:
        raise RuntimeError(
            "native result capture overflow: "
            f"{len(matching)} responses exceeds limit {CAPTURE_MAX_RESPONSES}"
        )
    total_response_chars = sum(len(str(item["agent_response"])) for item in matching)
    if total_response_chars > CAPTURE_MAX_RESPONSE_CHARS:
        raise RuntimeError(
            "native result capture overflow: "
            f"{total_response_chars} response characters exceeds limit "
            f"{CAPTURE_MAX_RESPONSE_CHARS}"
        )
    return matching


def select_capture_event(
    events: Iterable[dict[str, Any]],
    *,
    adapter: str,
    session_id: str,
) -> dict[str, Any]:
    return select_capture_events(
        events,
        adapter=adapter,
        session_id=session_id,
    )[-1]


def _capture_response_length(event: dict[str, Any]) -> int:
    return len(str(event["agent_response"]).strip())


def resolve_capture_events(
    events: Iterable[dict[str, Any]],
    *,
    adapter: str,
    session_id: str,
) -> dict[str, Any]:
    matching = select_capture_events(
        events,
        adapter=adapter,
        session_id=session_id,
    )
    latest = matching[-1]
    state = latest.get("state")
    if state in {"failed", "capture_error", "pending_async"}:
        return dict(latest)

    stops = [
        event
        for event in matching
        if event.get("hook_event") == "Stop"
        and event.get("state") in {"pending_async", "settled"}
    ]
    if len(stops) <= 1:
        return dict(latest)

    distinct_responses: list[str] = []
    for event in stops:
        response = str(event["agent_response"])
        if response not in distinct_responses:
            distinct_responses.append(response)
    if len(distinct_responses) == 1:
        return dict(latest)

    max_length = max(_capture_response_length(event) for event in stops)
    latest_length = _capture_response_length(stops[-1])

    if latest_length >= max_length * CAPTURE_LATEST_ACCEPT_RATIO:
        return dict(stops[-1])

    drop = max_length - latest_length
    if (
        latest_length <= max_length * CAPTURE_HOUSEKEEPING_REJECT_RATIO
        and drop >= CAPTURE_MIN_SIGNIFICANT_DROP_CHARS
    ):
        substantial = [
            event
            for event in stops
            if _capture_response_length(event)
            >= max_length * CAPTURE_LATEST_ACCEPT_RATIO
        ]
        selected = dict(substantial[-1])
        selected["capture_source_state"] = selected.get("state")
        selected["state"] = "settled"
        return selected

    return {
        "version": 1,
        "adapter": adapter,
        "session_id": session_id,
        "native_turn_id": latest.get("native_turn_id"),
        "state": "capture_ambiguous",
        "agent_response": None,
        "candidate_count": len(distinct_responses),
        "capture_events": stops,
        "captured_at_ns": latest.get("captured_at_ns"),
    }


