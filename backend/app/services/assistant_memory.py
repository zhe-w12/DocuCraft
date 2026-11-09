from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock


PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
MEMORY_CATEGORIES = {"semantic", "episodic", "procedural"}
_database_lock = RLock()


def is_valid_profile_id(value: str) -> bool:
    return bool(PROFILE_ID_PATTERN.fullmatch(value or ""))


def _database_path() -> Path:
    storage_root = Path(os.getenv("STORAGE_DIR", "storage"))
    if not storage_root.is_absolute():
        storage_root = Path(__file__).resolve().parents[2] / storage_root
    storage_root.mkdir(parents=True, exist_ok=True)
    return storage_root / "assistant_memory.sqlite3"


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_database_path(), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_memories (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            content TEXT NOT NULL,
            content_key TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'semantic',
            created_at TEXT NOT NULL,
            UNIQUE(profile_id, content_key)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_assistant_memories_profile_date "
        "ON assistant_memories(profile_id, created_at DESC)"
    )
    return connection


@contextmanager
def _connection():
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _memory_row(row: sqlite3.Row) -> dict[str, str]:
    return {
        "id": str(row["id"]),
        "content": str(row["content"]),
        "category": str(row["category"]),
        "created_at": str(row["created_at"]),
    }


def _terms(text: str) -> set[str]:
    normalized = (text or "").casefold()
    terms = set(re.findall(r"[a-z0-9_+-]{2,}", normalized))
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def search_memories(profile_id: str, query: str, *, limit: int = 3) -> list[str]:
    if not is_valid_profile_id(profile_id):
        return []
    normalized_query = " ".join((query or "").split())[:2_000]
    if not normalized_query:
        return []

    with _database_lock, _connection() as connection:
        rows = connection.execute(
            "SELECT content, created_at FROM assistant_memories "
            "WHERE profile_id = ? ORDER BY created_at DESC LIMIT 200",
            (profile_id,),
        ).fetchall()

    query_terms = _terms(normalized_query)
    ranked: list[tuple[int, int, str]] = []
    for index, row in enumerate(rows):
        content = str(row["content"])
        content_terms = _terms(content)
        score = len(query_terms & content_terms)
        if normalized_query.casefold() in content.casefold():
            score += max(2, len(query_terms))
        if score:
            ranked.append((score, -index, content))
    ranked.sort(reverse=True)
    return [item[2] for item in ranked[: max(1, min(limit, 5))]]


def save_memory(profile_id: str, content: str, category: str) -> str:
    if not is_valid_profile_id(profile_id):
        raise ValueError("长期记忆配置无效。")
    normalized_content = " ".join((content or "").split())
    if not normalized_content or len(normalized_content) > 800:
        raise ValueError("记忆内容必须为 1 到 800 个字符。")
    normalized_category = (category or "").strip().lower()
    if normalized_category not in MEMORY_CATEGORIES:
        normalized_category = "semantic"

    content_key = hashlib.sha256(normalized_content.casefold().encode("utf-8")).hexdigest()
    memory_id = hashlib.sha256(f"{profile_id}\0{content_key}".encode("utf-8")).hexdigest()[:32]
    created_at = datetime.now(timezone.utc).isoformat()
    with _database_lock, _connection() as connection:
        connection.execute(
            """
            INSERT INTO assistant_memories (id, profile_id, content, content_key, category, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, content_key) DO UPDATE SET
                content = excluded.content,
                category = excluded.category,
                created_at = excluded.created_at
            """,
            (memory_id, profile_id, normalized_content, content_key, normalized_category, created_at),
        )
    return memory_id


def list_memories(profile_id: str, *, limit: int = 100) -> list[dict[str, str]]:
    if not is_valid_profile_id(profile_id):
        raise ValueError("长期记忆配置无效。")
    with _database_lock, _connection() as connection:
        rows = connection.execute(
            "SELECT id, content, category, created_at FROM assistant_memories "
            "WHERE profile_id = ? ORDER BY created_at DESC LIMIT ?",
            (profile_id, max(1, min(limit, 200))),
        ).fetchall()
    return [_memory_row(row) for row in rows]


def delete_memory(profile_id: str, memory_id: str) -> bool:
    if not is_valid_profile_id(profile_id):
        raise ValueError("长期记忆配置无效。")
    with _database_lock, _connection() as connection:
        cursor = connection.execute(
            "DELETE FROM assistant_memories WHERE id = ? AND profile_id = ?",
            (memory_id, profile_id),
        )
    return cursor.rowcount > 0


def clear_memories(profile_id: str) -> int:
    if not is_valid_profile_id(profile_id):
        raise ValueError("长期记忆配置无效。")
    with _database_lock, _connection() as connection:
        cursor = connection.execute(
            "DELETE FROM assistant_memories WHERE profile_id = ?",
            (profile_id,),
        )
    return cursor.rowcount
