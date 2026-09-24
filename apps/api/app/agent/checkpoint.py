from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from langgraph.checkpoint.sqlite import SqliteSaver


@contextmanager
def open_sqlite_checkpointer(path: Path) -> Iterator[SqliteSaver]:
    """Open a durable local checkpointer owned by the current process."""

    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(resolved, check_same_thread=False)
    try:
        saver = SqliteSaver(connection)
        saver.setup()
        yield saver
    finally:
        connection.close()
