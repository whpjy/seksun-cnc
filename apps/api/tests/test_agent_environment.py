from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.checkpoint import open_sqlite_checkpointer
from app.agent.config import load_agent_settings
from app.agent.graph import build_environment_validation_graph, run_environment_validation


def test_agent_settings_default_to_safe_shadow_mode() -> None:
    settings = load_agent_settings({})

    assert settings.mode == "shadow"
    assert settings.enabled is True
    assert settings.writes_production_results is False
    assert settings.max_local_retries == 3


def test_agent_settings_reject_invalid_mode() -> None:
    with pytest.raises(ValueError, match="CNC_AGENT_MODE"):
        load_agent_settings({"CNC_AGENT_MODE": "uncontrolled"})


def test_sqlite_checkpoint_survives_reopen(tmp_path: Path) -> None:
    database = tmp_path / "langgraph.sqlite"
    settings = load_agent_settings(
        {
            "CNC_AGENT_MODE": "shadow",
            "CNC_AGENT_CHECKPOINT_PATH": str(database),
        }
    )

    with open_sqlite_checkpointer(database) as checkpointer:
        graph = build_environment_validation_graph(settings, checkpointer)
        first = run_environment_validation(graph, settings, job_id="job-001")

    with open_sqlite_checkpointer(database) as checkpointer:
        graph = build_environment_validation_graph(settings, checkpointer)
        second = run_environment_validation(graph, settings, job_id="job-001")
        separate = run_environment_validation(graph, settings, job_id="job-002")

    assert first["status"] == "ready"
    assert first["validation_count"] == 1
    assert second["validation_count"] == 2
    assert separate["validation_count"] == 1
    assert database.is_file()
