from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from deal_radar.bike_identity import has_accessory_terms, identify_listing, normalize_text
from deal_radar.config import PriorityConfig
from deal_radar.duplicates import (
    DuplicateMatch,
    compare_listings,
    contact_fingerprint,
    description_fingerprint,
    duplicate_group_id,
    normalize_duplicate_text,
    text_similarity,
)
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
            CREATE TABLE IF NOT EXISTS listing_duplicates (
                group_id TEXT NOT NULL,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                canonical_source TEXT NOT NULL,
                canonical_external_id TEXT NOT NULL,
                match_level TEXT NOT NULL,
                reason TEXT NOT NULL,
                similarity_score REAL NOT NULL,
                detected_at TEXT NOT NULL,
                is_canonical INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source, external_id)
            );
            CREATE INDEX IF NOT EXISTS listing_duplicates_group_idx
            ON listing_duplicates(group_id);
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
            ("description_fingerprint", "TEXT NOT NULL DEFAULT ''"),
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
                     notification_status, notification_reason, last_seen_at,
                     description_fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        description_fingerprint(listing.description),
                    ),
                )
                if cursor.rowcount:
                    inserted.append(listing)
                if cursor.rowcount == 0:
                    self.connection.execute(
                        """
                        UPDATE listings SET data_json = ?, last_seen_at = ?,
                            description_fingerprint = ?
                        WHERE source = ? AND external_id = ?
                        """,
                        (
                            json.dumps(listing.to_dict(), ensure_ascii=False),
                            _now(),
                            description_fingerprint(listing.description),
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

    @staticmethod
    def _choose_duplicate_canonical(
        records: list[tuple[Listing, datetime]],
        config: PriorityConfig,
    ) -> Listing:
        earliest = min(first_seen for _, first_seen in records)
        tied = [
            (listing, first_seen)
            for listing, first_seen in records
            if (first_seen - earliest).total_seconds() <= config.duplicate_canonical_seen_tie_seconds
        ]

        def key(item: tuple[Listing, datetime]) -> tuple[object, ...]:
            listing, _ = item
            price = listing.price_amount if listing.price_amount is not None else listing.price_czk
            return (
                -len(normalize_text(listing.description)),
                0 if price else 1,
                0 if listing.image_url else 1,
                0 if listing.published_at else 1,
                0 if listing.location else 1,
                listing.key,
            )

        return sorted(tied, key=key)[0][0]

    def backfill_duplicates(
        self,
        config: PriorityConfig,
        *,
        description_loader: Callable[[Listing], str] | None = None,
    ) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT source, external_id, data_json, first_seen_at FROM listings"
        ).fetchall()
        records = [
            (
                Listing.from_dict(json.loads(row["data_json"])),
                datetime.fromisoformat(row["first_seen_at"]),
            )
            for row in rows
        ]
        parent = {listing.key: listing.key for listing, _ in records}
        by_key = {listing.key: (listing, first_seen) for listing, first_seen in records}
        matches: dict[frozenset[str], DuplicateMatch] = {}
        detail_cache: dict[str, str] = {}

        def root(key: str) -> str:
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        def union(first: str, second: str) -> None:
            left, right = root(first), root(second)
            if left != right:
                parent[max(left, right)] = min(left, right)

        for index, (listing, _) in enumerate(records):
            for candidate, _ in records[index + 1 :]:
                match = compare_listings(
                    listing,
                    candidate,
                    confirmed_threshold=config.duplicate_confirmed_similarity,
                    possible_threshold=config.duplicate_possible_similarity,
                )
                if (
                    match.level == "none"
                    and match.reason == "text_similarity_below_threshold"
                    and description_loader is not None
                ):
                    compared: list[Listing] = []
                    for item in (listing, candidate):
                        description = item.description
                        if item.source == "cyklobazar" and len(description) < 300:
                            if item.key not in detail_cache:
                                try:
                                    detail_cache[item.key] = description_loader(item)
                                except Exception:
                                    detail_cache[item.key] = ""
                            description = detail_cache[item.key] or description
                        compared.append(replace(item, description=description))
                    match = compare_listings(
                        compared[0],
                        compared[1],
                        confirmed_threshold=config.duplicate_confirmed_similarity,
                        possible_threshold=config.duplicate_possible_similarity,
                    )
                if match.level in {"confirmed", "possible"}:
                    matches[frozenset((listing.key, candidate.key))] = match
                    union(listing.key, candidate.key)

        components: dict[str, list[str]] = {}
        for key in parent:
            components.setdefault(root(key), []).append(key)
        groups = [sorted(keys) for keys in components.values() if len(keys) > 1]
        existing_times = {
            str(row["group_id"]): str(row["detected_at"])
            for row in self.connection.execute(
                "SELECT group_id, MIN(detected_at) AS detected_at FROM listing_duplicates GROUP BY group_id"
            )
        }
        confirmed_groups = 0
        possible_groups = 0
        with self.connection:
            for listing, _ in records:
                self.connection.execute(
                    """
                    UPDATE listings SET description_fingerprint = ?
                    WHERE source = ? AND external_id = ?
                    """,
                    (description_fingerprint(listing.description), listing.source, listing.external_id),
                )
            self.connection.execute("DELETE FROM listing_duplicates")
            for keys in groups:
                group_id = duplicate_group_id(keys)
                component_records = [by_key[key] for key in keys]
                canonical = self._choose_duplicate_canonical(component_records, config)
                edge_matches = [
                    match
                    for pair, match in matches.items()
                    if pair.issubset(set(keys))
                ]
                group_level = "confirmed" if any(match.level == "confirmed" for match in edge_matches) else "possible"
                if group_level == "confirmed":
                    confirmed_groups += 1
                else:
                    possible_groups += 1
                detected_at = existing_times.get(group_id, _now())
                for key in keys:
                    member = by_key[key][0]
                    pair_match = matches.get(frozenset((canonical.key, key))) if key != canonical.key else None
                    similarity = (
                        pair_match.similarity
                        if pair_match is not None
                        else max((match.similarity for match in edge_matches), default=0.0)
                    )
                    reason = pair_match.reason if pair_match is not None else "duplicate_group_canonical"
                    match_level = pair_match.level if pair_match is not None else group_level
                    self.connection.execute(
                        """
                        INSERT INTO listing_duplicates
                        (group_id, source, external_id, canonical_source, canonical_external_id,
                         match_level, reason, similarity_score, detected_at, is_canonical)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            group_id,
                            member.source,
                            member.external_id,
                            canonical.source,
                            canonical.external_id,
                            match_level,
                            reason,
                            similarity,
                            detected_at,
                            int(member.key == canonical.key),
                        ),
                    )
        return {
            "groups": len(groups),
            "confirmed_groups": confirmed_groups,
            "possible_groups": possible_groups,
            "members": sum(len(keys) for keys in groups),
        }

    def get_duplicate_record(self, listing: Listing) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM listing_duplicates WHERE source = ? AND external_id = ?",
            (listing.source, listing.external_id),
        ).fetchone()

    def duplicate_alternatives(self, listing: Listing) -> list[dict[str, str]]:
        record = self.get_duplicate_record(listing)
        if not record or not record["is_canonical"] or record["match_level"] != "confirmed":
            return []
        rows = self.connection.execute(
            """
            SELECT d.source, d.external_id, l.data_json
            FROM listing_duplicates d
            JOIN listings l ON l.source = d.source AND l.external_id = d.external_id
            WHERE d.group_id = ? AND d.is_canonical = 0 AND d.match_level = 'confirmed'
            ORDER BY d.source, d.external_id
            """,
            (record["group_id"],),
        ).fetchall()
        return [
            {
                "source": str(row["source"]),
                "external_id": str(row["external_id"]),
                "url": Listing.from_dict(json.loads(row["data_json"])).url,
            }
            for row in rows
        ]

    def refresh_duplicate_used_comparables(self, max_age_days: int) -> int:
        rows = self.connection.execute(
            """
            SELECT l.data_json
            FROM listings l
            JOIN listing_duplicates d ON d.source = l.source AND d.external_id = l.external_id
            """
        ).fetchall()
        updated = 0
        for row in rows:
            listing = Listing.from_dict(json.loads(row["data_json"]))
            analysis = self.get_analysis(listing.source, listing.external_id)
            if analysis is None:
                continue
            analysis.used_comparables = self.find_used_comparables(
                listing,
                max_age_days=max_age_days,
            )
            analysis.duplicate_alternatives = self.duplicate_alternatives(listing)
            self.save_analysis(listing, analysis)
            updated += 1
        return updated

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
        duplicate_rows = self.connection.execute(
            "SELECT source, external_id, group_id FROM listing_duplicates"
        ).fetchall()
        duplicate_groups = {
            f"{row['source']}:{row['external_id']}": str(row["group_id"])
            for row in duplicate_rows
        }
        listing_group = duplicate_groups.get(listing.key, "")
        listing_description_fingerprint = description_fingerprint(listing.description)
        listing_description_is_substantive = len(normalize_duplicate_text(listing.description)) >= 80
        listing_contact = contact_fingerprint(f"{listing.title}\n{listing.description}")
        rows = self.connection.execute(
            """
            SELECT source, external_id, data_json, first_seen_at, description_fingerprint
            FROM listings WHERE last_seen_at >= ?
            """,
            (cutoff.isoformat(),),
        ).fetchall()
        matches: list[UsedComparable] = []
        seen_urls: set[str] = set()
        for row in rows:
            candidate = Listing.from_dict(json.loads(row["data_json"]))
            if candidate.key == listing.key or candidate.url in seen_urls:
                continue
            candidate_group = duplicate_groups.get(candidate.key, "")
            if listing_group and candidate_group == listing_group:
                continue
            candidate_fingerprint = str(row["description_fingerprint"] or "")
            if (
                listing_description_is_substantive
                and listing_description_fingerprint
                and len(normalize_duplicate_text(candidate.description)) >= 80
                and candidate_fingerprint == listing_description_fingerprint
            ):
                continue
            listing_price = (
                listing.price_amount if listing.price_amount is not None else listing.price_czk
            )
            candidate_price = (
                candidate.price_amount
                if candidate.price_amount is not None
                else candidate.price_czk
            )
            candidate_contact = contact_fingerprint(f"{candidate.title}\n{candidate.description}")
            if (
                listing_contact
                and listing_contact == candidate_contact
                and listing_price == candidate_price
                and text_similarity(listing.description, candidate.description) >= 0.85
            ):
                continue
            price = candidate_price
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
