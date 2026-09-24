from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


_VALID_MODES = {"disabled", "shadow", "active"}


def _integer(value: str, name: str, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return parsed


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class AgentSettings:
    mode: str = "active"
    checkpointer: str = "sqlite"
    checkpoint_path: Path = Path(".seksun-cnc/langgraph.sqlite")
    max_local_retries: int = 3
    max_setup_replans: int = 1
    max_global_replans: int = 1
    human_review_enabled: bool = True

    @property
    def enabled(self) -> bool:
        return self.mode != "disabled"

    @property
    def writes_production_results(self) -> bool:
        return self.mode == "active"


def load_agent_settings(environment: Mapping[str, str] | None = None) -> AgentSettings:
    env = os.environ if environment is None else environment
    mode = env.get("CNC_AGENT_MODE", "active").strip().lower()
    if mode not in _VALID_MODES:
        choices = ", ".join(sorted(_VALID_MODES))
        raise ValueError(f"CNC_AGENT_MODE must be one of: {choices}")

    checkpointer = env.get("CNC_AGENT_CHECKPOINTER", "sqlite").strip().lower()
    if checkpointer != "sqlite":
        raise ValueError("CNC_AGENT_CHECKPOINTER currently supports only sqlite")

    return AgentSettings(
        mode=mode,
        checkpointer=checkpointer,
        checkpoint_path=Path(
            env.get("CNC_AGENT_CHECKPOINT_PATH", ".seksun-cnc/langgraph.sqlite")
        ),
        max_local_retries=_integer(
            env.get("CNC_AGENT_MAX_LOCAL_RETRIES", "3"),
            "CNC_AGENT_MAX_LOCAL_RETRIES",
        ),
        max_setup_replans=_integer(
            env.get("CNC_AGENT_MAX_SETUP_REPLANS", "1"),
            "CNC_AGENT_MAX_SETUP_REPLANS",
        ),
        max_global_replans=_integer(
            env.get("CNC_AGENT_MAX_GLOBAL_REPLANS", "1"),
            "CNC_AGENT_MAX_GLOBAL_REPLANS",
        ),
        human_review_enabled=_boolean(
            env.get("CNC_AGENT_HUMAN_REVIEW_ENABLED", "true"),
            "CNC_AGENT_HUMAN_REVIEW_ENABLED",
        ),
    )
