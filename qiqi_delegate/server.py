"""Standalone QiQi MCP tools: delegation and TaskGraph; no SLP/Supervisor."""
from __future__ import annotations

import os
from functools import wraps
from typing import Any, Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from qiqi_delegate.core import build_task_packet
from qiqi_delegate.runtime import AgentStartupBlocked, DelegateRuntime, workspace_root
from qiqi_delegate.task_graph_runtime import (
    GraphRuntime, task_graph_from_payload, decisions_from_payload,
)
from qiqi_delegate.task_graph_store import GraphRuntimeStore
from qiqi_delegate.task_graph import GraphNode

class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact: str
    source: str

class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str
    source: str

class Context(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trusted_facts: list[Fact] = Field(default_factory=list)
    claims_to_investigate: list[Claim] = Field(default_factory=list)

class Packet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str
    scope: list[str]
    acceptance_criteria: list[str]
    out_of_scope: list[str] = Field(default_factory=list)
    context: Context | None = None
    constraints: list[str] = Field(default_factory=list)
    known_unknowns: list[str] = Field(default_factory=list)

class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    repository: str
    route: str
    task_packet: Packet
    depends_on: list[str] = Field(default_factory=list)
    kind: Literal["repo_task"] = "repo_task"

class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[Node] = Field(min_length=1)

class ReviewLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    attempt_id: str

class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    action: Literal["accept", "retry", "replan", "block"]
    resume_session: bool = False
    feedback: list[str] = Field(
        default_factory=list,
        description="Only valid for action='retry'; not allowed for block/replan/accept.",
    )
    owner: str | None = Field(
        default=None,
        description="Required with action='block' or 'replan' to assign recovery owner.",
    )
    return_checkpoint: str | None = Field(
        default=None,
        description="Required with action='block' or 'replan' to define next review point.",
    )

runtime = DelegateRuntime(workspace_root())
graph_runtime = GraphRuntime(GraphRuntimeStore(runtime.db))
mcp = MCPServer(
    "QiQi Delegate (standalone)",
    instructions=(
        "Delegate repository-local tasks using a self-sufficient TaskPacket. "
        "Pass the exact repository name from workspace repos.yaml. "
        "Lead owns technical acceptance. Native result hooks are the only answer source. "
        "No terminal scraping, Supervisor, or sibling repository reads. "
        "Call workspace_info first for registered repositories and route names. "
        "For multiple nodes: start_graph, delegate_next, get_node_reviews, submit_decisions. "
        "A settled Peer response does not imply ACCEPT; Lead must explicitly accept it. "
        "For discovery, analysis or review, author acceptance criteria requiring relevant "
        "file:line evidence, implementation/data flow and limitations, not just a summary. "
        "Before ACCEPT, read the exact captured Peer response via get_node_reviews, "
        "check evidence against the criteria and RETRY with targeted feedback if shallow. "
        "In the final answer, preserve concrete findings and citations across Peer reports; "
        "do not replace them with a generic high-level paraphrase. "
        "If a Peer cannot start because Herdr is unavailable, review the runtime error "
        "and do not inspect or implement in the target repository directly. "
        "For block/replan decisions provide owner and return_checkpoint; "
        "feedback and resume_session are retry-only."
    ),
)

def _public_tool_errors(func):
    """Expose actionable input/runtime failures instead of 'Error executing tool'."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, RuntimeError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            lowered = detail.lower()
            if "agent_not_ready" in lowered:
                code = "agent_startup_blocked"
                action = (
                    "inspect the preserved Herdr startup pane using the exact "
                    "agent name and workspace ID in the error; handle any "
                    "interactive trust/auth prompt manually. After closing "
                    "the workspace and confirming the worker stopped, arrange "
                    "operator-side claim cleanup before retrying."
                )
            elif "repos.yaml" in lowered or "repository" in lowered:
                code = "repository_registry_invalid"
                action = (
                    "check repos.yaml paths against existing exact Git roots; "
                    "paths inside the workspace or to registered sibling repos "
                    "under the workspace parent are supported"
                )
            elif "route" in lowered or "agent-routing.yaml" in lowered:
                code = "routing_invalid"
                action = "check agent-routing.yaml for an exact supported route and CLI arguments"
            elif "herdr" in lowered or "agent" in lowered:
                code = "worker_runtime_failed"
                action = "check the Herdr CLI, integration status, agent pane and runtime logs"
            else:
                code = "delegation_invalid"
                action = "inspect the reported input, task state or runtime stage and retry"
            # Startup recovery may contain a long, shell-quoted workspace path.
            # Clip only diagnostic evidence, never the exact operator command.
            public_detail = (
                exc.actionable_detail()
                if isinstance(exc, AgentStartupBlocked)
                else detail[:1200]
            )
            raise ToolError(
                f"code={code}; {public_detail}; action={action}"
            ) from exc
    return wrapper


def _to_packet(value: Packet):
    return build_task_packet(**value.model_dump(exclude_none=True))


def _check_graph_routes(authored) -> None:
    """Fail before writing any TaskGraph snapshot if a route does not exist."""
    for node in authored.nodes:
        if not node.route:
            raise ValueError(f"node {node.node_id!r} has no route")
        runtime.route(node.route)


@mcp.tool()
@_public_tool_errors
async def workspace_info() -> dict[str, Any]:
    """List registered repository names, agent route names and selected Herdr session.

    Call this before start_graph or delegate_repo_task. Pass exact route_names keys, never agent kinds.
    """
    repositories = runtime.repos()
    cfg = runtime._load_yaml("agent-routing.yaml")
    routes = cfg.get("routes", {})
    if not isinstance(routes, dict):
        raise ValueError("agent-routing.yaml routes must be an object")
    summary = {}
    for name in routes:
        agent, _ = runtime.route(name)
        summary[name] = agent
    return {
        "repositories": sorted(repositories),
        "routes": summary,
        "route_names": sorted(summary),
        "route_usage": (
            "Use a key from route_names as the route argument, "
            "not an agent kind value from routes (for example, codex)."
        ),
        "herdr_session": runtime.herdr_session or "inherited/default",
        "herdr_socket_env_present": bool(os.environ.get("HERDR_SOCKET_PATH")),
    }

@mcp.tool()
@_public_tool_errors
async def delegate_repo_task(
    repository: Annotated[str, Field(description="Exact repos.yaml name")],
    route: str,
    objective: str,
    scope: list[str],
    acceptance_criteria: list[str],
    out_of_scope: list[str] | None = None,
    context: Context | None = None,
    constraints: list[str] | None = None,
    known_unknowns: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """START or RESUME an interactive Codex/Claude Peer in one repository."""
    packet = build_task_packet(
        objective=objective, scope=scope, acceptance_criteria=acceptance_criteria,
        out_of_scope=out_of_scope, constraints=constraints, known_unknowns=known_unknowns,
        context=context.model_dump() if context else None,
    )
    return await runtime.delegate(repository=repository, route=route,
                                  packet=packet, session_id=session_id)

@mcp.tool()
@_public_tool_errors
async def start_graph(graph: Graph) -> dict[str, Any]:
    """Validate and persist a dependency DAG. Does not launch any Peer."""
    authored = task_graph_from_payload(graph.model_dump(exclude_none=True))
    _check_graph_routes(authored)
    return graph_runtime.start_graph(authored, repository_names=runtime.repos().keys())

@mcp.tool()
@_public_tool_errors
async def get_graph(graph_run_id: str) -> dict[str, Any]:
    """Return compact TaskGraph state and exact review locators."""
    return graph_runtime.get_graph(graph_run_id)

@mcp.tool()
@_public_tool_errors
async def get_node_reviews(graph_run_id: str,
                           reviews: Annotated[list[ReviewLocator], Field(min_length=1, max_length=8)],
                           expected_revision: int | None = None) -> dict[str, Any]:
    """Load at most eight exact review locators in a single bounded call."""
    locators = [(item.node_id, item.attempt_id) for item in reviews]
    return graph_runtime.get_node_reviews(
        graph_run_id, locators, expected_revision=expected_revision
    )

async def _graph_execute(node: GraphNode, session_id: str | None = None) -> dict:
    if not node.route:
        raise ValueError(f"node {node.node_id} has no route")
    return await runtime.delegate(repository=node.repository, route=node.route,
                                  packet=node.task_packet, session_id=session_id)

@mcp.tool()
@_public_tool_errors
async def delegate_next(graph_run_id: str) -> dict[str, Any]:
    """Execute one conflict-free wave; dependent nodes require ACCEPT."""
    async def start(node: GraphNode):
        return await _graph_execute(node)
    async def resume(node: GraphNode, session_id: str):
        return await _graph_execute(node, session_id)
    return await graph_runtime.delegate_next(graph_run_id, executor=start, resume_executor=resume)

@mcp.tool()
@_public_tool_errors
async def submit_decisions(graph_run_id: str, decisions: list[Decision],
                           expected_revision: int) -> dict[str, Any]:
    """Record Lead decisions after semantic review of exact captured Peer reports.

    For action='block' or 'replan', set owner and return_checkpoint.
    Only action='retry' permits feedback or resume_session.
    Never ACCEPT a failed runtime attempt without captured Peer evidence.
    """
    parsed = decisions_from_payload([
        d.model_dump(exclude_none=True, exclude_defaults=True) for d in decisions
    ])
    dispositions = []
    for decision in parsed:
        persisted = graph_runtime.store.get_node(graph_run_id, decision.node_id)
        if not persisted:
            raise ValueError(f"unknown node: {decision.node_id}")
        turn_id = persisted.get("turn_id")
        if decision.action == "accept":
            if not turn_id:
                raise ValueError("ACCEPT requires a captured Peer turn")
            with runtime._connect() as db:
                turn = db.execute("SELECT state FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
            if not turn or turn["state"] != "settled":
                raise ValueError("ACCEPT requires an exact successful captured Peer response")
        if turn_id:
            reason = f"Lead decision: {decision.action}"
            if decision.feedback:
                reason += "; feedback=" + " | ".join(decision.feedback)
            if decision.action in {"replan", "block"}:
                reason += f"; owner={decision.owner}; return_checkpoint={decision.return_checkpoint}"
            dispositions.append({
                "turn_id": turn_id, "action": decision.action, "reason": reason,
                "node_id": decision.node_id, "attempt_id": persisted.get("current_attempt_id"),
            })
    return graph_runtime.submit_decisions(
        graph_run_id, parsed, expected_revision=expected_revision,
        lead_dispositions=tuple(dispositions),
    )

@mcp.tool()
@_public_tool_errors
async def reconcile_graph(graph_run_id: str, graph: Graph,
                          expected_revision: int) -> dict[str, Any]:
    """Apply a new explicit authored DAG with revision protection."""
    authored = task_graph_from_payload(graph.model_dump(exclude_none=True))
    _check_graph_routes(authored)
    return graph_runtime.reconcile_graph(
        graph_run_id, authored, repository_names=runtime.repos().keys(),
        expected_revision=expected_revision,
    )

def main():
    mcp.run()

if __name__ == "__main__":
    main()
