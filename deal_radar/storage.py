from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from deal_radar.models import Listing, Valuation


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    def __init__(self, path: str) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS listings (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                data_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                sent_at TEXT,
                telegram_message_id INTEGER,
                suppressed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source, external_id)
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                label TEXT NOT NULL,
                telegram_user TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS valuation_cache (
                fingerprint TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
        feedback_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(feedback)").fetchall()
        }
        for name, column_type in (
            ("callback_query_id", "TEXT"),
            ("telegram_message_id", "INTEGER"),
            ("telegram_chat_id", "TEXT"),
        ):
            if name not in feedback_columns:
                self.connection.execute(f"ALTER TABLE feedback ADD COLUMN {name} {column_type}")
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS feedback_callback_query_id_uq
            ON feedback(callback_query_id)
            WHERE callback_query_id IS NOT NULL
            """
        )
        self.connection.commit()

    def is_empty(self) -> bool:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM listings").fetchone()
        return int(row["count"]) == 0

    def register(self, listings: list[Listing], suppress_keys: set[str] | None = None) -> int:
        suppress_keys = suppress_keys or set()
        inserted = 0
        with self.connection:
            for listing in listings:
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO listings
                    (source, external_id, data_json, first_seen_at, suppressed)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        listing.source,
                        listing.external_id,
                        json.dumps(listing.to_dict(), ensure_ascii=False),
                        _now(),
                        int(listing.key in suppress_keys),
                    ),
                )
                inserted += cursor.rowcount
                if cursor.rowcount == 0:
                    self.connection.execute(
                        """
                        UPDATE listings SET data_json = ?
                        WHERE source = ? AND external_id = ?
                        """,
                        (
                            json.dumps(listing.to_dict(), ensure_ascii=False),
                            listing.source,
                            listing.external_id,
                        ),
                    )
        return inserted

    def get_listing(self, source: str, external_id: str) -> Listing | None:
        row = self.connection.execute(
            "SELECT data_json FROM listings WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        return Listing.from_dict(json.loads(row["data_json"])) if row else None

    def pending(self, limit: int) -> list[Listing]:
        rows = self.connection.execute(
            """
            SELECT data_json FROM listings
            WHERE sent_at IS NULL AND suppressed = 0
            ORDER BY first_seen_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [Listing.from_dict(json.loads(row["data_json"])) for row in rows]

    def mark_sent(self, listing: Listing, message_id: int) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE listings SET sent_at = ?, telegram_message_id = ?
                WHERE source = ? AND external_id = ?
                """,
                (_now(), message_id, listing.source, listing.external_id),
            )

    def record_feedback(
        self,
        source: str,
        external_id: str,
        label: str,
        user: str = "",
        callback_query_id: str = "",
        telegram_message_id: int | None = None,
        telegram_chat_id: str = "",
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO feedback
                (source, external_id, label, telegram_user, created_at,
                 callback_query_id, telegram_message_id, telegram_chat_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    external_id,
                    label,
                    user,
                    _now(),
                    callback_query_id or None,
                    telegram_message_id,
                    telegram_chat_id or None,
                ),
            )
        return cursor.rowcount > 0

    def get_metadata(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_metadata(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_cached_valuation(self, fingerprint: str) -> Valuation | None:
        row = self.connection.execute(
            "SELECT data_json, expires_at FROM valuation_cache WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            with self.connection:
                self.connection.execute("DELETE FROM valuation_cache WHERE fingerprint = ?", (fingerprint,))
            return None
        return Valuation.from_dict(json.loads(row["data_json"]))

    def cache_valuation(self, fingerprint: str, valuation: Valuation, days: int) -> None:
        self.cache_valuation_hours(fingerprint, valuation, max(1, days * 24))

    def cache_valuation_hours(self, normalized_model_key: str, valuation: Valuation, hours: int) -> None:
        expires_at = (datetime.now(UTC) + timedelta(hours=max(1, hours))).isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO valuation_cache (fingerprint, data_json, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    data_json = excluded.data_json,
                    expires_at = excluded.expires_at
                """,
                (normalized_model_key, json.dumps(valuation.to_dict(), ensure_ascii=False), expires_at),
            )
