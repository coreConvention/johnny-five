"""Database layer — SQLite with FTS5 and sqlite-vec."""

from claude_memory.db.connection import db_session, get_connection
from claude_memory.db.queries import MemoryRecord
from claude_memory.db.schema import SCHEMA_VERSION, initialize_db

__all__ = [
    "SCHEMA_VERSION",
    "MemoryRecord",
    "db_session",
    "get_connection",
    "initialize_db",
]
