from __future__ import annotations

import json
import sqlite3
import statistics
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
                deal_score INTEGER,
                deal_tier TEXT,
                verdict_json TEXT,
                scored_at TEXT,
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
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_key TEXT NOT NULL,
                brand TEXT,
                model TEXT,
                model_year INTEGER,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                price_czk INTEGER NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_price_history_model
                ON price_history (model_key, observed_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_price_history_listing
                ON price_history (source, external_id);
            """
        )
        self._add_missing_columns()
        self.connection.commit()

    def _add_missing_columns(self) -> None:
        """Идемпотентно доносит новые колонки в БД, созданные ранней версией."""
        existing = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(listings)").fetchall()
        }
        additions = {
            "deal_score": "INTEGER",
            "deal_tier": "TEXT",
            "verdict_json": "TEXT",
            "scored_at": "TEXT",
        }
        for column, column_type in additions.items():
            if column not in existing:
                self.connection.execute(
                    f"ALTER TABLE listings ADD COLUMN {column} {column_type}"
                )

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
        return inserted

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

    def mark_scored(
        self, listing: Listing, verdict, message_id: int | None, keep_pending: bool = False
    ) -> None:
        """Сохраняет результат скоринга.

        message_id=None → archive (оценено, в базе, но не отправлено в Telegram).
        keep_pending=True → не проставлять sent_at, чтобы объявление вернулось
        в очередь на переоценку (например, archive без уточнённой цены нового).
        """
        verdict_payload = {
            "score": verdict.score,
            "tier": verdict.tier,
            "components": {
                "margin": verdict.components.margin,
                "liquidity": verdict.components.liquidity,
                "risk": verdict.components.risk,
                "quality": verdict.components.quality,
                "freshness": verdict.components.freshness,
            },
            "reasons": verdict.reasons,
            "red_flags": verdict.red_flags,
            "expected_resale_czk": verdict.expected_resale_czk,
            "expected_profit_czk": verdict.expected_profit_czk,
            "age_years": verdict.age_years,
        }
        mark_sent_value = None if keep_pending else message_id
        with self.connection:
            self.connection.execute(
                """
                UPDATE listings
                SET deal_score = ?, deal_tier = ?, verdict_json = ?, scored_at = ?,
                    sent_at = CASE WHEN ? IS NOT NULL THEN ? ELSE sent_at END,
                    telegram_message_id = COALESCE(?, telegram_message_id)
                WHERE source = ? AND external_id = ?
                """,
                (
                    verdict.score,
                    verdict.tier,
                    json.dumps(verdict_payload, ensure_ascii=False),
                    _now(),
                    mark_sent_value, _now(),
                    message_id,
                    listing.source, listing.external_id,
                ),
            )

    def record_feedback(self, source: str, external_id: str, label: str, user: str = "") -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO feedback (source, external_id, label, telegram_user, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source, external_id, label, user, _now()),
            )

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

    def record_price_observation(
        self,
        listing: Listing,
        model_key: str,
        brand: str = "",
        model: str = "",
        model_year: int | None = None,
    ) -> None:
        """Сохраняет цену объявления как точку в истории цен б/у по модели.
        Идемпотентно по (source, external_id): повторный показ/TOP не задваивает.
        Объявления без распознанной цены или без модели пропускаются."""
        if listing.price_czk is None or not model_key or model_key.count("?") >= 2:
            return
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO price_history
                (model_key, brand, model, model_year, source, external_id, price_czk, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_key, brand, model, model_year,
                    listing.source, listing.external_id, listing.price_czk, _now(),
                ),
            )

    def used_price_stats(self, model_key: str, days: int = 120, min_samples: int = 3):
        """Возвращает статистику реальных цен б/у по модели за период или None,
        если данных меньше min_samples. Основа для будущей замены амортизации
        на фактические цены рынка."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.connection.execute(
            """
            SELECT price_czk FROM price_history
            WHERE model_key = ? AND observed_at >= ?
            ORDER BY price_czk
            """,
            (model_key, cutoff),
        ).fetchall()
        prices = [int(r["price_czk"]) for r in rows]
        if len(prices) < min_samples:
            return None
        return {
            "count": len(prices),
            "median_czk": int(round(statistics.median(prices))),
            "min_czk": prices[0],
            "max_czk": prices[-1],
        }

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
