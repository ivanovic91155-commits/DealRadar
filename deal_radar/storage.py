from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
from deal_radar.models import (
    AIAnalysis,
    DealCosts,
    DealEvaluation,
    Listing,
    ListingAnalysis,
    MarketComparable,
    MarketValuation,
    UsedComparable,
    UsedComparables,
    Valuation,
)


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
            CREATE TABLE IF NOT EXISTS pricing_comparables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                valuation_listing_source TEXT NOT NULL,
                valuation_listing_external_id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_listing_id TEXT NOT NULL,
                url TEXT NOT NULL,
                country TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price_original REAL,
                currency TEXT NOT NULL,
                price_czk INTEGER,
                brand TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                model_family TEXT NOT NULL DEFAULT '',
                generation TEXT NOT NULL DEFAULT '',
                trim TEXT NOT NULL DEFAULT '',
                year INTEGER,
                frame_size TEXT NOT NULL DEFAULT '',
                wheel_size TEXT NOT NULL DEFAULT '',
                bike_type TEXT NOT NULL DEFAULT '',
                is_ebike INTEGER,
                suspension_type TEXT NOT NULL DEFAULT '',
                frame_material TEXT NOT NULL DEFAULT '',
                fork_class TEXT NOT NULL DEFAULT '',
                drivetrain_class TEXT NOT NULL DEFAULT '',
                brake_class TEXT NOT NULL DEFAULT '',
                travel_mm INTEGER,
                match_type TEXT NOT NULL,
                match_score REAL NOT NULL,
                listing_fingerprint TEXT NOT NULL,
                image_fingerprint TEXT NOT NULL DEFAULT '',
                duplicate_group_id TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                image_url TEXT NOT NULL DEFAULT '',
                seller TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                weight REAL NOT NULL DEFAULT 0,
                exclusion_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (
                    valuation_listing_source,
                    valuation_listing_external_id,
                    source,
                    source_listing_id
                )
            );
            CREATE INDEX IF NOT EXISTS pricing_comparables_valuation_idx
            ON pricing_comparables(valuation_listing_source, valuation_listing_external_id);
            CREATE INDEX IF NOT EXISTS pricing_comparables_fingerprint_idx
            ON pricing_comparables(listing_fingerprint);
            CREATE TABLE IF NOT EXISTS market_valuations (
                listing_source TEXT NOT NULL,
                listing_external_id TEXT NOT NULL,
                market_price_czk INTEGER,
                quick_sale_price_czk INTEGER,
                price_low_czk INTEGER,
                price_high_czk INTEGER,
                confidence TEXT NOT NULL,
                valuation_method TEXT NOT NULL,
                status TEXT NOT NULL,
                comparables_total INTEGER NOT NULL,
                comparables_unique INTEGER NOT NULL,
                exact_comparables INTEGER NOT NULL,
                close_comparables INTEGER NOT NULL,
                family_comparables INTEGER NOT NULL,
                component_comparables INTEGER NOT NULL,
                cz_comparables INTEGER NOT NULL,
                foreign_comparables INTEGER NOT NULL,
                countries_used TEXT NOT NULL,
                sources_used TEXT NOT NULL,
                warnings TEXT NOT NULL,
                data_json TEXT NOT NULL,
                calculated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (listing_source, listing_external_id)
            );
            CREATE TABLE IF NOT EXISTS market_lookup_cache (
                cache_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                market TEXT NOT NULL,
                status TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exchange_rates (
                currency TEXT PRIMARY KEY,
                rate_to_czk REAL NOT NULL,
                valid_date TEXT NOT NULL,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_source_health (
                source TEXT PRIMARY KEY,
                consecutive_errors INTEGER NOT NULL DEFAULT 0,
                open_until TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS country_market_calibration (
                country TEXT NOT NULL,
                bike_type TEXT NOT NULL DEFAULT '',
                adjustment REAL NOT NULL,
                sample_size INTEGER NOT NULL,
                calculated_at TEXT NOT NULL,
                PRIMARY KEY (country, bike_type)
            );
            CREATE TABLE IF NOT EXISTS deal_cost_overrides (
                listing_source TEXT NOT NULL,
                listing_external_id TEXT NOT NULL,
                acquisition_costs_czk REAL NOT NULL DEFAULT 0,
                logistics_costs_czk REAL NOT NULL DEFAULT 0,
                platform_fees_czk REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (listing_source, listing_external_id)
            );
            CREATE TABLE IF NOT EXISTS deal_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_source TEXT NOT NULL,
                listing_external_id TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                calculated_at TEXT NOT NULL,
                purchase_price_czk REAL,
                acquisition_costs_czk REAL NOT NULL,
                logistics_costs_czk REAL NOT NULL,
                platform_fees_czk REAL NOT NULL,
                base_investment_czk REAL,
                risk_reserve_percent REAL NOT NULL,
                risk_reserve_czk REAL,
                total_investment_czk REAL,
                market_median_czk REAL,
                quick_sale_price_czk REAL,
                net_profit_czk REAL,
                roi_percent REAL,
                deal_score REAL NOT NULL,
                score_components_json TEXT NOT NULL,
                status TEXT NOT NULL,
                explanation TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                flags_json TEXT NOT NULL,
                liquidity_score INTEGER,
                liquidity_level TEXT NOT NULL,
                confidence_score INTEGER,
                confidence_level TEXT NOT NULL,
                condition TEXT NOT NULL,
                market_scope TEXT NOT NULL,
                market_valuation_ref TEXT NOT NULL,
                config_snapshot_json TEXT NOT NULL,
                data_json TEXT NOT NULL,
                UNIQUE (listing_source, listing_external_id, input_fingerprint)
            );
            CREATE INDEX IF NOT EXISTS deal_evaluations_listing_idx
            ON deal_evaluations(listing_source, listing_external_id, id DESC);
            CREATE INDEX IF NOT EXISTS deal_evaluations_status_score_idx
            ON deal_evaluations(status, deal_score DESC);
            CREATE TABLE IF NOT EXISTS ai_analysis_cache (
                content_hash TEXT NOT NULL,
                prompt_name TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                model_name TEXT NOT NULL,
                listing_source TEXT NOT NULL,
                listing_external_id TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (content_hash, prompt_name, prompt_version, schema_version, model_name)
            );
            CREATE TABLE IF NOT EXISTS ai_call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                listing_source TEXT NOT NULL,
                listing_external_id TEXT NOT NULL,
                analysis_type TEXT NOT NULL,
                model_name TEXT NOT NULL,
                prompt_name TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                latency_ms INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                estimated_input_cost_usd REAL NOT NULL,
                estimated_output_cost_usd REAL NOT NULL,
                estimated_total_cost_usd REAL NOT NULL,
                attempt_number INTEGER NOT NULL,
                used_fallback INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0,
                error_type TEXT NOT NULL DEFAULT '',
                error_message_safe TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ai_call_log_started_idx ON ai_call_log(started_at);
            CREATE INDEX IF NOT EXISTS ai_call_log_listing_idx
            ON ai_call_log(listing_source, listing_external_id, id DESC);
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

    def update_listing_description(self, listing: Listing) -> None:
        """Переписать data_json объявления после догрузки детальной страницы.

        Описание сохраняется, чтобы следующий цикл не платил за тот же HTTP-запрос
        и чтобы отпечаток содержимого для AI-кэша не прыгал между запусками.
        """

        with self.connection:
            self.connection.execute(
                """
                UPDATE listings
                SET data_json = ?, description_fingerprint = ?
                WHERE source = ? AND external_id = ?
                """,
                (
                    json.dumps(listing.to_dict(), ensure_ascii=False),
                    description_fingerprint(listing.description),
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

    def local_market_candidates(
        self,
        listing: Listing,
        *,
        max_age_days: int,
    ) -> list[MarketComparable]:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        rows = self.connection.execute(
            """
            SELECT l.data_json, l.first_seen_at, l.last_seen_at, d.group_id
            FROM listings l
            LEFT JOIN listing_duplicates d
              ON d.source = l.source AND d.external_id = l.external_id
            WHERE l.last_seen_at >= ?
            ORDER BY l.last_seen_at DESC
            """,
            (cutoff.isoformat(),),
        ).fetchall()
        result: list[MarketComparable] = []
        for row in rows:
            candidate = Listing.from_dict(json.loads(row["data_json"]))
            price = candidate.price_amount if candidate.price_amount is not None else candidate.price_czk
            if not price or price <= 0:
                continue
            identity = identify_listing(candidate)
            result.append(
                MarketComparable(
                    source=candidate.source,
                    source_listing_id=candidate.external_id,
                    url=candidate.url,
                    country="CZ",
                    title=candidate.title,
                    description=candidate.description,
                    price_original=float(price),
                    currency=candidate.currency or "CZK",
                    price_czk=candidate.price_czk,
                    brand=identity.brand,
                    model=identity.model,
                    model_family=identity.model_family,
                    generation=identity.generation,
                    trim=identity.trim,
                    year=identity.model_year,
                    frame_size=identity.frame_size,
                    wheel_size=identity.wheel_size,
                    bike_type=identity.bike_type,
                    is_ebike=identity.electric,
                    suspension_type=identity.suspension_type,
                    frame_material=identity.frame_material,
                    fork_class=identity.fork_class,
                    drivetrain_class=identity.drivetrain_class,
                    brake_class=identity.brake_class,
                    travel_mm=identity.travel_mm,
                    listing_fingerprint=description_fingerprint(candidate.description),
                    duplicate_group_id=str(row["group_id"] or ""),
                    published_at=candidate.published_at.isoformat() if candidate.published_at else "",
                    first_seen_at=str(row["first_seen_at"]),
                    last_seen_at=str(row["last_seen_at"]),
                    image_url=candidate.image_url or "",
                    location=candidate.location or "",
                )
            )
        return result

    def get_cached_market_valuation(self, listing: Listing) -> MarketValuation | None:
        row = self.connection.execute(
            """
            SELECT data_json, expires_at FROM market_valuations
            WHERE listing_source = ? AND listing_external_id = ?
            """,
            (listing.source, listing.external_id),
        ).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            return None
        valuation = MarketValuation.from_dict(json.loads(row["data_json"]))
        valuation.cache_used = True
        valuation.cache_hits += 1
        return valuation

    def get_latest_market_valuation(self, source: str, external_id: str) -> MarketValuation | None:
        row = self.connection.execute(
            """
            SELECT data_json FROM market_valuations
            WHERE listing_source = ? AND listing_external_id = ?
            """,
            (source, external_id),
        ).fetchone()
        return MarketValuation.from_dict(json.loads(row["data_json"])) if row else None

    def save_market_valuation(self, listing: Listing, valuation: MarketValuation) -> None:
        payload = json.dumps(valuation.to_dict(), ensure_ascii=False)
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO market_valuations (
                    listing_source, listing_external_id, market_price_czk,
                    quick_sale_price_czk, price_low_czk, price_high_czk,
                    confidence, valuation_method, status, comparables_total,
                    comparables_unique, exact_comparables, close_comparables,
                    family_comparables, component_comparables, cz_comparables,
                    foreign_comparables, countries_used, sources_used, warnings,
                    data_json, calculated_at, expires_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(listing_source, listing_external_id) DO UPDATE SET
                    market_price_czk = excluded.market_price_czk,
                    quick_sale_price_czk = excluded.quick_sale_price_czk,
                    price_low_czk = excluded.price_low_czk,
                    price_high_czk = excluded.price_high_czk,
                    confidence = excluded.confidence,
                    valuation_method = excluded.valuation_method,
                    status = excluded.status,
                    comparables_total = excluded.comparables_total,
                    comparables_unique = excluded.comparables_unique,
                    exact_comparables = excluded.exact_comparables,
                    close_comparables = excluded.close_comparables,
                    family_comparables = excluded.family_comparables,
                    component_comparables = excluded.component_comparables,
                    cz_comparables = excluded.cz_comparables,
                    foreign_comparables = excluded.foreign_comparables,
                    countries_used = excluded.countries_used,
                    sources_used = excluded.sources_used,
                    warnings = excluded.warnings,
                    data_json = excluded.data_json,
                    calculated_at = excluded.calculated_at,
                    expires_at = excluded.expires_at
                """,
                (
                    listing.source,
                    listing.external_id,
                    valuation.market_price_czk,
                    valuation.quick_sale_price_czk,
                    valuation.price_low_czk,
                    valuation.price_high_czk,
                    valuation.confidence,
                    valuation.valuation_method,
                    valuation.status,
                    valuation.comparables_total,
                    valuation.comparables_unique,
                    valuation.exact_comparables,
                    valuation.close_comparables,
                    valuation.family_comparables,
                    valuation.component_comparables,
                    valuation.cz_comparables,
                    valuation.foreign_comparables,
                    json.dumps(valuation.countries_used, ensure_ascii=False),
                    json.dumps(valuation.sources_used, ensure_ascii=False),
                    json.dumps(valuation.warnings, ensure_ascii=False),
                    payload,
                    valuation.calculated_at,
                    valuation.expires_at,
                ),
            )
            self.connection.execute(
                """
                DELETE FROM pricing_comparables
                WHERE valuation_listing_source = ? AND valuation_listing_external_id = ?
                """,
                (listing.source, listing.external_id),
            )
            for comparable in valuation.comparables + valuation.rejected_comparables:
                values = comparable.to_dict()
                values.update(
                    {
                        "valuation_listing_source": listing.source,
                        "valuation_listing_external_id": listing.external_id,
                        "is_ebike": None if comparable.is_ebike is None else int(comparable.is_ebike),
                        "is_active": int(comparable.is_active),
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO pricing_comparables (
                        valuation_listing_source, valuation_listing_external_id,
                        source, source_listing_id, url, country, title, description,
                        price_original, currency, price_czk, brand, model, model_family,
                        generation, trim, year, frame_size, wheel_size, bike_type,
                        is_ebike, suspension_type, frame_material, fork_class,
                        drivetrain_class, brake_class, travel_mm, match_type,
                        match_score, listing_fingerprint, image_fingerprint,
                        duplicate_group_id, published_at, first_seen_at, last_seen_at,
                        is_active, image_url, seller, location, weight,
                        exclusion_reason, created_at, updated_at
                    ) VALUES (
                        :valuation_listing_source, :valuation_listing_external_id,
                        :source, :source_listing_id, :url, :country, :title, :description,
                        :price_original, :currency, :price_czk, :brand, :model, :model_family,
                        :generation, :trim, :year, :frame_size, :wheel_size, :bike_type,
                        :is_ebike, :suspension_type, :frame_material, :fork_class,
                        :drivetrain_class, :brake_class, :travel_mm, :match_type,
                        :match_score, :listing_fingerprint, :image_fingerprint,
                        :duplicate_group_id, :published_at, :first_seen_at, :last_seen_at,
                        :is_active, :image_url, :seller, :location, :weight,
                        :exclusion_reason, :created_at, :updated_at
                    )
                    """,
                    values,
                )

    def get_deal_costs(self, listing: Listing) -> DealCosts:
        row = self.connection.execute(
            """
            SELECT acquisition_costs_czk, logistics_costs_czk, platform_fees_czk
            FROM deal_cost_overrides
            WHERE listing_source = ? AND listing_external_id = ?
            """,
            (listing.source, listing.external_id),
        ).fetchone()
        if row is None:
            return DealCosts()
        return DealCosts(
            acquisition_costs_czk=float(row["acquisition_costs_czk"]),
            logistics_costs_czk=float(row["logistics_costs_czk"]),
            platform_fees_czk=float(row["platform_fees_czk"]),
        )

    def set_deal_costs(self, listing: Listing, costs: DealCosts) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO deal_cost_overrides (
                    listing_source, listing_external_id, acquisition_costs_czk,
                    logistics_costs_czk, platform_fees_czk, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_source, listing_external_id) DO UPDATE SET
                    acquisition_costs_czk = excluded.acquisition_costs_czk,
                    logistics_costs_czk = excluded.logistics_costs_czk,
                    platform_fees_czk = excluded.platform_fees_czk,
                    updated_at = excluded.updated_at
                """,
                (
                    listing.source,
                    listing.external_id,
                    costs.acquisition_costs_czk,
                    costs.logistics_costs_czk,
                    costs.platform_fees_czk,
                    _now(),
                ),
            )

    def save_deal_evaluation(self, evaluation: DealEvaluation) -> bool:
        payload = json.dumps(evaluation.to_dict(), ensure_ascii=False, sort_keys=True)
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO deal_evaluations (
                    listing_source, listing_external_id, algorithm_version,
                    input_fingerprint, calculated_at, purchase_price_czk,
                    acquisition_costs_czk, logistics_costs_czk, platform_fees_czk,
                    base_investment_czk, risk_reserve_percent, risk_reserve_czk,
                    total_investment_czk, market_median_czk, quick_sale_price_czk,
                    net_profit_czk, roi_percent, deal_score, score_components_json,
                    status, explanation, reasons_json, flags_json, liquidity_score,
                    liquidity_level, confidence_score, confidence_level, condition,
                    market_scope, market_valuation_ref, config_snapshot_json, data_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    evaluation.listing_source,
                    evaluation.listing_external_id,
                    evaluation.algorithm_version,
                    evaluation.input_fingerprint,
                    evaluation.calculated_at,
                    evaluation.purchase_price_czk,
                    evaluation.acquisition_costs_czk,
                    evaluation.logistics_costs_czk,
                    evaluation.platform_fees_czk,
                    evaluation.base_investment_czk,
                    evaluation.risk_reserve_percent,
                    evaluation.risk_reserve_czk,
                    evaluation.total_investment_czk,
                    evaluation.market_median_czk,
                    evaluation.quick_sale_price_czk,
                    evaluation.net_profit_czk,
                    evaluation.roi_percent,
                    evaluation.deal_score,
                    json.dumps(evaluation.score_components, ensure_ascii=False, sort_keys=True),
                    evaluation.status,
                    evaluation.explanation,
                    json.dumps(evaluation.reasons, ensure_ascii=False),
                    json.dumps(evaluation.flags, ensure_ascii=False),
                    evaluation.liquidity_score,
                    evaluation.liquidity_level,
                    evaluation.confidence_score,
                    evaluation.confidence_level,
                    evaluation.condition,
                    evaluation.market_scope,
                    evaluation.market_valuation_ref,
                    json.dumps(evaluation.config_snapshot, ensure_ascii=False, sort_keys=True),
                    payload,
                ),
            )
        return bool(cursor.rowcount)

    def get_latest_deal_evaluation(
        self,
        source: str,
        external_id: str,
    ) -> DealEvaluation | None:
        row = self.connection.execute(
            """
            SELECT data_json FROM deal_evaluations
            WHERE listing_source = ? AND listing_external_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (source, external_id),
        ).fetchone()
        return DealEvaluation.from_dict(json.loads(row["data_json"])) if row else None

    def get_market_lookup_cache(self, cache_key: str) -> tuple[str, list[MarketComparable]] | None:
        row = self.connection.execute(
            "SELECT status, data_json, expires_at FROM market_lookup_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            return None
        items = [MarketComparable.from_dict(item) for item in json.loads(row["data_json"])]
        return str(row["status"]), items

    def save_market_lookup_cache(
        self,
        cache_key: str,
        source: str,
        market: str,
        status: str,
        items: list[MarketComparable],
        ttl_hours: int,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO market_lookup_cache
                (cache_key, source, market, status, data_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    status = excluded.status,
                    data_json = excluded.data_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    cache_key,
                    source,
                    market,
                    status,
                    json.dumps([item.to_dict() for item in items], ensure_ascii=False),
                    _now(),
                    (datetime.now(UTC) + timedelta(hours=max(1, ttl_hours))).isoformat(),
                ),
            )

    def market_source_available(self, source: str) -> bool:
        row = self.connection.execute(
            "SELECT open_until FROM market_source_health WHERE source = ?", (source,)
        ).fetchone()
        if not row or not row["open_until"]:
            return True
        return datetime.fromisoformat(row["open_until"]) <= datetime.now(UTC)

    def record_market_source_success(self, source: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO market_source_health
                (source, consecutive_errors, open_until, last_error, updated_at)
                VALUES (?, 0, '', '', ?)
                ON CONFLICT(source) DO UPDATE SET
                    consecutive_errors = 0, open_until = '', last_error = '', updated_at = excluded.updated_at
                """,
                (source, _now()),
            )

    def record_market_source_error(
        self,
        source: str,
        error: str,
        *,
        threshold: int,
        cooldown_minutes: int,
    ) -> int:
        row = self.connection.execute(
            "SELECT consecutive_errors FROM market_source_health WHERE source = ?", (source,)
        ).fetchone()
        count = int(row["consecutive_errors"] if row else 0) + 1
        open_until = (
            (datetime.now(UTC) + timedelta(minutes=cooldown_minutes)).isoformat()
            if count >= threshold
            else ""
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO market_source_health
                (source, consecutive_errors, open_until, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    consecutive_errors = excluded.consecutive_errors,
                    open_until = excluded.open_until,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (source, count, open_until, error[:300], _now()),
            )
        return count

    def load_exchange_rates(self) -> tuple[dict[str, float], str, str]:
        rows = self.connection.execute(
            "SELECT currency, rate_to_czk, valid_date, source, fetched_at FROM exchange_rates"
        ).fetchall()
        if not rows:
            return {}, "", ""
        return (
            {str(row["currency"]): float(row["rate_to_czk"]) for row in rows},
            str(rows[0]["valid_date"]),
            str(rows[0]["fetched_at"]),
        )

    def save_exchange_rates(
        self,
        rates: dict[str, float],
        valid_date: str,
        source: str,
        fetched_at: str,
    ) -> None:
        with self.connection:
            for currency, rate in rates.items():
                self.connection.execute(
                    """
                    INSERT INTO exchange_rates
                    (currency, rate_to_czk, valid_date, source, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(currency) DO UPDATE SET
                        rate_to_czk = excluded.rate_to_czk,
                        valid_date = excluded.valid_date,
                        source = excluded.source,
                        fetched_at = excluded.fetched_at
                    """,
                    (currency, rate, valid_date, source, fetched_at),
                )

    def recent_active_listings(self, limit: int = 50) -> list[Listing]:
        rows = self.connection.execute(
            """
            SELECT l.data_json
            FROM listings l
            LEFT JOIN listing_duplicates d
              ON d.source = l.source AND d.external_id = l.external_id
            WHERE l.last_seen_at >= ?
              AND (d.is_canonical IS NULL OR d.is_canonical = 1 OR d.match_level = 'possible')
            ORDER BY l.last_seen_at DESC, l.first_seen_at DESC
            LIMIT ?
            """,
            ((datetime.now(UTC) - timedelta(days=7)).isoformat(), max(1, limit)),
        ).fetchall()
        return [Listing.from_dict(json.loads(row["data_json"])) for row in rows]

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

    def get_cached_ai_analysis(
        self,
        *,
        content_hash: str,
        prompt_name: str,
        prompt_version: str,
        schema_version: str,
        model_name: str,
    ) -> AIAnalysis | None:
        """Кэш из раздела 17 ТЗ: не платить дважды за неизменившееся объявление."""

        row = self.connection.execute(
            """
            SELECT data_json, expires_at FROM ai_analysis_cache
            WHERE content_hash = ? AND prompt_name = ? AND prompt_version = ?
              AND schema_version = ? AND model_name = ?
            """,
            (content_hash, prompt_name, prompt_version, schema_version, model_name),
        ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            with self.connection:
                self.connection.execute(
                    """
                    DELETE FROM ai_analysis_cache
                    WHERE content_hash = ? AND prompt_name = ? AND prompt_version = ?
                      AND schema_version = ? AND model_name = ?
                    """,
                    (content_hash, prompt_name, prompt_version, schema_version, model_name),
                )
            return None
        return AIAnalysis.from_dict(json.loads(row["data_json"]))

    def cache_ai_analysis(self, listing: Listing, analysis: AIAnalysis, hours: int) -> None:
        expires_at = (datetime.now(UTC) + timedelta(hours=max(1, hours))).isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO ai_analysis_cache (
                    content_hash, prompt_name, prompt_version, schema_version, model_name,
                    listing_source, listing_external_id, data_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_hash, prompt_name, prompt_version, schema_version, model_name)
                DO UPDATE SET
                    listing_source = excluded.listing_source,
                    listing_external_id = excluded.listing_external_id,
                    data_json = excluded.data_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    analysis.content_hash,
                    analysis.prompt_name,
                    analysis.prompt_version,
                    analysis.schema_version,
                    analysis.model_name,
                    listing.source,
                    listing.external_id,
                    json.dumps(analysis.to_dict(), ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                    expires_at,
                ),
            )

    def log_ai_call(self, record: dict[str, Any]) -> None:
        """Журнал раздела 16 ТЗ. Ключ, заголовки и тело запроса сюда не попадают."""

        defaults: dict[str, Any] = {
            "request_id": "",
            "listing_source": "",
            "listing_external_id": "",
            "analysis_type": "listing-analysis",
            "model_name": "",
            "prompt_name": "",
            "prompt_version": "",
            "schema_version": "",
            "started_at": "",
            "finished_at": "",
            "latency_ms": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_input_cost_usd": 0.0,
            "estimated_output_cost_usd": 0.0,
            "estimated_total_cost_usd": 0.0,
            "attempt_number": 1,
            "used_fallback": 0,
            "success": 0,
            "error_type": "",
            "error_message_safe": "",
        }
        columns = tuple(defaults)
        placeholders = ", ".join("?" for _ in columns)
        with self.connection:
            self.connection.execute(
                f"INSERT INTO ai_call_log ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(record.get(column, defaults[column]) for column in columns),
            )

    def ai_spend_usd_since(self, moment: datetime) -> float:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(estimated_total_cost_usd), 0) AS spent FROM ai_call_log WHERE started_at >= ?",
            (moment.astimezone(UTC).isoformat(),),
        ).fetchone()
        return float(row["spent"] or 0.0)

    def ai_call_count_since(self, moment: datetime) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS calls FROM ai_call_log WHERE started_at >= ?",
            (moment.astimezone(UTC).isoformat(),),
        ).fetchone()
        return int(row["calls"] or 0)
