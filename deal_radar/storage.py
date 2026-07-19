from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

from deal_radar.bike_identity import has_accessory_terms, identify_listing, normalize_text
from deal_radar.models import Listing, ListingAnalysis, UsedComparable, UsedComparables, Valuation


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
        listing_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(listings)").fetchall()
        }
        listing_schema_changed = False
        for name, definition in (
            ("analysis_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("notification_status", "TEXT NOT NULL DEFAULT 'awaiting_analysis'"),
            ("notification_reason", "TEXT NOT NULL DEFAULT ''"),
            ("last_seen_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in listing_columns:
                self.connection.execute(f"ALTER TABLE listings ADD COLUMN {name} {definition}")
                listing_schema_changed = True
        if listing_schema_changed:
            self.connection.execute(
                """
                UPDATE listings
                SET notification_status = CASE
                    WHEN sent_at IS NOT NULL THEN 'sent'
                    WHEN suppressed = 1 THEN 'not_selected'
                    ELSE 'expired'
                END,
                notification_reason = CASE
                    WHEN sent_at IS NOT NULL THEN ''
                    WHEN suppressed = 1 THEN 'bootstrap_suppressed'
                    ELSE 'pre_stage_1_2_pending'
                END,
                last_seen_at = first_seen_at
                """
            )
        self.connection.commit()

    def is_empty(self) -> bool:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM listings").fetchone()
        return int(row["count"]) == 0

    def register(self, listings: list[Listing], suppress_keys: set[str] | None = None) -> int:
        return len(self.register_new(listings, suppress_keys))

    def register_new(
        self,
        listings: list[Listing],
        suppress_keys: set[str] | None = None,
    ) -> list[Listing]:
        suppress_keys = suppress_keys or set()
        inserted: list[Listing] = []
        with self.connection:
            for listing in listings:
                suppressed = listing.key in suppress_keys
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO listings
                    (source, external_id, data_json, first_seen_at, suppressed,
                     notification_status, notification_reason, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        listing.source,
                        listing.external_id,
                        json.dumps(listing.to_dict(), ensure_ascii=False),
                        _now(),
                        int(suppressed),
                        "not_selected" if suppressed else "awaiting_analysis",
                        "bootstrap_suppressed" if suppressed else "",
                        _now(),
                    ),
                )
                if cursor.rowcount:
                    inserted.append(listing)
                if cursor.rowcount == 0:
                    self.connection.execute(
                        """
                        UPDATE listings SET data_json = ?, last_seen_at = ?
                        WHERE source = ? AND external_id = ?
                        """,
                        (
                            json.dumps(listing.to_dict(), ensure_ascii=False),
                            _now(),
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

    def first_seen_at(self, listing: Listing) -> datetime:
        row = self.connection.execute(
            "SELECT first_seen_at FROM listings WHERE source = ? AND external_id = ?",
            (listing.source, listing.external_id),
        ).fetchone()
        return datetime.fromisoformat(row["first_seen_at"]) if row else datetime.now(UTC)

    def save_analysis(self, listing: Listing, analysis: ListingAnalysis) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE listings
                SET analysis_json = ?, notification_status = ?, notification_reason = ?
                WHERE source = ? AND external_id = ?
                """,
                (
                    json.dumps(analysis.to_dict(), ensure_ascii=False),
                    analysis.notification_status,
                    analysis.notification_reason,
                    listing.source,
                    listing.external_id,
                ),
            )

    def get_analysis(self, source: str, external_id: str) -> ListingAnalysis | None:
        row = self.connection.execute(
            "SELECT analysis_json FROM listings WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        if not row or not row["analysis_json"] or row["analysis_json"] == "{}":
            return None
        return ListingAnalysis.from_dict(json.loads(row["analysis_json"]))

    def set_notification(self, listing: Listing, status: str, reason: str = "") -> None:
        analysis = self.get_analysis(listing.source, listing.external_id)
        if analysis:
            analysis.notification_status = status
            analysis.notification_reason = reason
            self.save_analysis(listing, analysis)
            return
        with self.connection:
            self.connection.execute(
                """
                UPDATE listings SET notification_status = ?, notification_reason = ?
                WHERE source = ? AND external_id = ?
                """,
                (status, reason, listing.source, listing.external_id),
            )

    def find_used_comparables(
        self,
        listing: Listing,
        *,
        max_age_days: int,
    ) -> UsedComparables:
        identity = identify_listing(listing)
        if not identity.brand or not identity.model:
            return UsedComparables()
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        rows = self.connection.execute(
            "SELECT source, external_id, data_json, first_seen_at FROM listings WHERE last_seen_at >= ?",
            (cutoff.isoformat(),),
        ).fetchall()
        matches: list[UsedComparable] = []
        seen_urls: set[str] = set()
        for row in rows:
            candidate = Listing.from_dict(json.loads(row["data_json"]))
            if candidate.key == listing.key or candidate.url in seen_urls:
                continue
            price = candidate.price_amount if candidate.price_amount is not None else candidate.price_czk
            if not price or price <= 0 or has_accessory_terms(candidate.title):
                continue
            candidate_identity = identify_listing(candidate)
            if normalize_text(candidate_identity.brand) != normalize_text(identity.brand):
                continue
            if normalize_text(candidate_identity.model) != normalize_text(identity.model):
                continue
            if identity.bike_type and candidate_identity.bike_type and identity.bike_type != candidate_identity.bike_type:
                continue
            if identity.generation and candidate_identity.generation and normalize_text(identity.generation) != normalize_text(candidate_identity.generation):
                continue
            if identity.model_year and candidate_identity.model_year and identity.model_year != candidate_identity.model_year:
                continue
            seen_urls.add(candidate.url)
            matches.append(
                UsedComparable(
                    source=candidate.source,
                    external_id=candidate.external_id,
                    title=candidate.title,
                    url=candidate.url,
                    price_czk=int(price),
                )
            )
        if len(matches) >= 3:
            center = statistics.median(item.price_czk for item in matches)
            matches = [item for item in matches if center * 0.55 <= item.price_czk <= center * 1.8]
        prices = [item.price_czk for item in matches]
        confidence = "insufficient" if not matches else "low" if len(matches) < 3 else "medium" if len(matches) < 5 else "high"
        return UsedComparables(
            count=len(matches),
            minimum_price_czk=min(prices) if prices else None,
            maximum_price_czk=max(prices) if prices else None,
            median_price_czk=int(round(statistics.median(prices))) if prices else None,
            items=matches,
            confidence=confidence,
        )

    def mark_sent(self, listing: Listing, message_id: int) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE listings SET sent_at = ?, telegram_message_id = ?,
                    notification_status = 'sent', notification_reason = ''
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
