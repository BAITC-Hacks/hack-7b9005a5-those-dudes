"""Optional OpenAI Agents SDK adapter around the deterministic tool library.

Importing this module never calls an external service. SDK queries and node-role
explanations run only after checking the optional dependency and configured key.
"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from typing import Any

from .engine import InvestigationEngine
from .store import GraphDataStore
from .config import AISettings, get_ai_settings


try:  # Optional dependency; offline mode remains fully functional without it.
    from agents import Agent, Runner, RunConfig, ModelSettings, OpenAIResponsesModel
    from openai import AsyncOpenAI

    try:
        from agents import function_tool
    except ImportError:  # Newer SDK releases expose the equivalent `tool` decorator.
        from agents.decorators import tool as function_tool
    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent optional branch
    Agent = None  # type: ignore[assignment]
    Runner = None  # type: ignore[assignment]
    SDK_AVAILABLE = False

    def function_tool(function):  # type: ignore[no-redef]
        return function


_ENGINE: InvestigationEngine | None = None


def configure_tools(engine: InvestigationEngine) -> None:
    global _ENGINE
    _ENGINE = engine


def _engine() -> InvestigationEngine:
    if _ENGINE is None:
        raise RuntimeError("Agent tools are not configured. Call create_agent() first.")
    return _ENGINE


def _json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


@function_tool
def get_node_profile(gid: str) -> str:
    """Return role, confidence, priority, numeric evidence and sampling warnings for one exact gid."""

    return _json(_engine().node_profile(gid))


@function_tool
def explain_node_priority(gid: str) -> str:
    """Explain an exact gid's investigative-priority score using stored numeric components."""

    return _json(_engine().explain_priority(gid))


@function_tool
def get_neighbors(gid: str, direction: str = "both", limit: int = 20) -> str:
    """List observed direct neighbors for gid; direction is in, out, or both."""

    return _json(_engine().neighbors(gid, direction, limit))


@function_tool
def trace_outgoing_routes(
    gid: str, max_hops: int = 3, limit: int = 10, min_kzt: float = 0.0
) -> str:
    """Trace observed outgoing routes without revisiting a node, capped at four hops."""

    return _json(_engine().trace_routes(gid, max_hops, limit, min_kzt))


@function_tool
def find_common_recipients(gids: list[str], limit: int = 20) -> str:
    """Find direct recipients funded by at least two of the supplied exact gids."""

    return _json(_engine().common_recipients(gids, limit))


@function_tool
def compare_nodes(gids: list[str]) -> str:
    """Compare two to ten exact gids on the same role, priority, flow and uncertainty fields."""

    return _json(_engine().compare_nodes(gids))


@function_tool
def get_cluster_summary(cluster_id: str) -> str:
    """Return a cluster's stored hypothesis, role mix and top-priority members."""

    return _json(_engine().cluster_summary(cluster_id))


@function_tool
def list_top_candidates(role: str = "", limit: int = 20) -> str:
    """List nodes by investigative priority, optionally filtered to one role."""

    return _json(_engine().top_candidates(role or None, limit))


@function_tool
def list_uncertain_nodes(limit: int = 20) -> str:
    """List high-priority nodes affected by low role confidence or depth-4 censoring."""

    return _json(_engine().uncertain_nodes(limit))


@function_tool
def get_resilience_summary(strategy: str = "") -> str:
    """Return precomputed network-resilience scenarios, optionally for one ranking strategy."""

    return _json(_engine().resilience_summary(strategy or None))


TOOLS = [
    get_node_profile,
    explain_node_priority,
    get_neighbors,
    trace_outgoing_routes,
    find_common_recipients,
    compare_nodes,
    get_cluster_summary,
    list_top_candidates,
    list_uncertain_nodes,
    get_resilience_summary,
]


def _instructions() -> str:
    prompt_path = Path(__file__).resolve().parent / "docs" / "prompt.md"
    return prompt_path.read_text(encoding="utf-8")


def create_agent(engine: InvestigationEngine):
    """Create one focused SDK Agent without running it or contacting an API."""

    if not SDK_AVAILABLE or Agent is None:
        raise RuntimeError(
            "Optional package openai-agents is not installed. Offline mode is available."
        )
    configure_tools(engine)
    return Agent(
        name="HackAlem AML Graph Analyst",
        instructions=_instructions(),
        tools=TOOLS,
    )


async def run_sdk_query(
    question: str,
    *,
    store: GraphDataStore | None = None,
) -> dict[str, Any]:
    """Run the optional SDK mode. This is intentionally never used by offline tests."""

    settings = get_ai_settings(store.paths.root if store else None)
    if not settings.configured:
        raise RuntimeError(
            "OPENAI_API_KEY is not configured; use offline mode or configure the key explicitly."
        )
    if not SDK_AVAILABLE or Runner is None:
        raise RuntimeError("Install the optional 'llm' dependencies to use SDK mode.")
    engine = InvestigationEngine(store or GraphDataStore(strict_outputs=True))
    agent = create_agent(engine)
    async with AsyncOpenAI(api_key=settings.api_key, base_url="https://api.openai.com/v1", timeout=settings.timeout_seconds, max_retries=0) as client:
        agent.model = OpenAIResponsesModel(model=settings.model, openai_client=client)
        agent.model_settings = ModelSettings(store=False)
        result = await asyncio.wait_for(
            Runner.run(agent, question, run_config=RunConfig(tracing_disabled=True), max_turns=8),
            timeout=settings.timeout_seconds,
        )
    return {
        "status": "ok",
        "mode": "sdk",
        "answer": str(result.final_output),
        "caveat": (
            "Ответ сформирован поверх локальных расчётов и остаётся гипотезой для проверки аналитиком."
        ),
    }


async def generate_role_explanation(payload: dict[str, Any], settings: AISettings) -> str:
    """Explain one prepared decision; no graph access, tools, or classification writes."""
    if not SDK_AVAILABLE:
        raise RuntimeError("SDK_UNAVAILABLE")
    instructions = (Path(__file__).parent / "docs" / "role_explanation.md").read_text(encoding="utf-8")
    async with AsyncOpenAI(api_key=settings.api_key, base_url="https://api.openai.com/v1", timeout=settings.timeout_seconds, max_retries=0) as client:
        agent = Agent(
            name="HackAlem Role Explainer",
            instructions=instructions,
            model=OpenAIResponsesModel(model=settings.model, openai_client=client),
            model_settings=ModelSettings(store=False, max_tokens=1100),
            tools=[],
        )
        result = await asyncio.wait_for(
            Runner.run(agent, _json(payload), run_config=RunConfig(tracing_disabled=True), max_turns=1),
            timeout=settings.timeout_seconds,
        )
    answer = str(result.final_output or "").strip()
    if not answer:
        raise RuntimeError("EMPTY_EXPLANATION")
    return answer
