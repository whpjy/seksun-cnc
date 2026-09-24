from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.checkpoint import open_sqlite_checkpointer
from app.agent.config import load_agent_settings
from app.agent.graph import build_environment_validation_graph, run_environment_validation


def main() -> None:
    settings = load_agent_settings()
    with open_sqlite_checkpointer(settings.checkpoint_path) as checkpointer:
        graph = build_environment_validation_graph(settings, checkpointer)
        result = run_environment_validation(graph, settings)
    print(json.dumps(dict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
