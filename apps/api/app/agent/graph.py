from __future__ import annotations

import platform
import sys
from collections.abc import Mapping
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .config import AgentSettings
from .state import EnvironmentValidationState


def _validate_environment(
    state: EnvironmentValidationState,
) -> EnvironmentValidationState:
    count = int(state.get("validation_count", 0)) + 1
    events = [*state.get("events", []), f"environment_validation:{count}"]
    return {
        "runtime": f"{platform.python_implementation()} {sys.version_info.major}.{sys.version_info.minor}",
        "validation_count": count,
        "status": "ready",
        "events": events,
    }


def build_environment_validation_graph(
    settings: AgentSettings,
    checkpointer: BaseCheckpointSaver,
):
    workflow = StateGraph(EnvironmentValidationState)
    workflow.add_node("validate_environment", _validate_environment)
    workflow.add_edge(START, "validate_environment")
    workflow.add_edge("validate_environment", END)
    return workflow.compile(checkpointer=checkpointer)


def run_environment_validation(
    graph: Any,
    settings: AgentSettings,
    *,
    job_id: str = "langgraph-environment-smoke",
) -> Mapping[str, object]:
    config = {"configurable": {"thread_id": job_id}}
    previous = graph.get_state(config)
    previous_count = int((previous.values or {}).get("validation_count", 0))
    return graph.invoke(
        {
            "job_id": job_id,
            "mode": settings.mode,
            "validation_count": previous_count,
            "events": [],
        },
        config,
    )
