from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from deal_radar.scoring_config import ScoringConfig


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(slots=True)
class SearchProfile:
    name: str
    rss_url: str
    require_any_keywords: list[str] = field(default_factory=list)
    exclude_title_keywords: list[str] = field(default_factory=list)
    min_price_czk: int | None = None
    max_price_czk: int | None = None

    def validate(self) -> None:
        parsed = urlparse(self.rss_url)
        if parsed.scheme != "https" or not (
            parsed.hostname == "bazos.cz" or (parsed.hostname or "").endswith(".bazos.cz")
        ):
            raise ValueError(f"Profile {self.name!r} must use an HTTPS Bazoš RSS URL")


@dataclass(slots=True)
class CyklobazarProfile:
    name: str
    url: str
    enabled: bool = True
    location_label: str = ""
    require_any_keywords: list[str] = field(default_factory=list)
    exclude_title_keywords: list[str] = field(default_factory=list)
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    include_promoted: bool = False

    def validate(self) -> None:
        parsed = urlparse(self.url)
        hostname = parsed.hostname or ""
        if parsed.scheme != "https" or not (
            hostname == "cyklobazar.cz" or hostname.endswith(".cyklobazar.cz")
        ):
            raise ValueError(f"Profile {self.name!r} must use an HTTPS Cyklobazar URL")
        if not parsed.path.startswith("/kola"):
            raise ValueError(f"Profile {self.name!r} must point to a Cyklobazar bicycle listing page")


@dataclass(slots=True)
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""


@dataclass(slots=True)
class RetailConfig:
    enabled: bool = True
    sources: list[str] = field(default_factory=lambda: ["zbozi"])
    max_enrichments_per_run: int = 3
    min_comparables: int = 3
    identity_min_confidence: float = 0.7
    exact_match_threshold: float = 0.85
    ambiguous_match_threshold: float = 0.7
    success_cache_hours: int = 336
    insufficient_cache_hours: int = 48
    error_cache_hours: int = 1
    source_timeout_seconds: int = 15
    total_timeout_seconds: int = 45
    max_parallel_requests: int = 3
    max_queries_per_source: int = 2
    max_product_pages: int = 3
    max_offers_kept: int = 7
    max_telegram_sources: int = 5
    outlier_low_ratio: float = 0.55
    outlier_high_ratio: float = 1.8
    target_currency: str = "CZK"
    currency_rates_to_czk: dict[str, float] = field(default_factory=lambda: {"CZK": 1.0})
    codex_enabled: bool = False
    codex_path: str = "codex"
    codex_schema_path: str = "schemas/bike_match.schema.json"
    codex_timeout_seconds: int = 60
    codex_calls_per_hour: int = 3
    codex_calls_per_day: int = 10

    def validate(self) -> None:
        if self.min_comparables < 3:
            raise ValueError("retail.min_comparables must be at least 3")
        if not 0 <= self.ambiguous_match_threshold < self.exact_match_threshold <= 1:
            raise ValueError("retail match thresholds must satisfy 0 <= ambiguous < exact <= 1")
        if self.target_currency != "CZK":
            raise ValueError("MVP currently supports CZK as the target currency")
        if (
            self.max_parallel_requests < 1
            or self.max_product_pages < 0
            or self.source_timeout_seconds < 1
            or self.total_timeout_seconds < 1
        ):
            raise ValueError("retail request limits must be positive")
        unknown_sources = set(self.sources) - {"zbozi"}
        if unknown_sources:
            raise ValueError(f"Unknown retail price sources: {sorted(unknown_sources)}")


@dataclass(slots=True)
class AppConfig:
    database_path: str = "data/deal_radar.sqlite3"
    poll_interval_seconds: int = 420
    poll_jitter_seconds: int = 60
    source_backoff_max_seconds: int = 3600
    feedback_poll_interval_seconds: int = 10
    bootstrap_mode: str = "send_latest"
    max_initial_notifications: int = 1
    max_notifications_per_run: int = 10
    request_timeout_seconds: int = 30
    profiles: list[SearchProfile] = field(default_factory=list)
    cyklobazar_profiles: list[CyklobazarProfile] = field(default_factory=list)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    retail: RetailConfig = field(default_factory=RetailConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)

    def validate(self) -> None:
        if self.bootstrap_mode not in {"send_latest", "skip_existing", "send_all"}:
            raise ValueError("bootstrap_mode must be send_latest, skip_existing, or send_all")
        if not 180 <= self.poll_interval_seconds <= 86_400:
            raise ValueError("poll_interval_seconds must be between 180 and 86400")
        if not 2 <= self.feedback_poll_interval_seconds <= 300:
            raise ValueError("feedback_poll_interval_seconds must be between 2 and 300")
        if not self.profiles and not any(profile.enabled for profile in self.cyklobazar_profiles):
            raise ValueError("At least one marketplace search profile is required")
        for profile in self.profiles:
            profile.validate()
        for profile in self.cyklobazar_profiles:
            profile.validate()
        self.retail.validate()
        self.scoring.validate()


def load_config(path: str | Path = "config.json") -> AppConfig:
    load_dotenv()
    config_path = Path(path)
    if not config_path.exists() and config_path.name == "config.json":
        config_path = Path("config.example.json")
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    telegram_raw = raw.get("telegram", {})
    retail_raw = raw.get("retail", {})
    codex_env = os.getenv("DEAL_RADAR_CODEX_ENABLED", "").strip().casefold()
    config = AppConfig(
        database_path=raw.get("database_path", "data/deal_radar.sqlite3"),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 420)),
        poll_jitter_seconds=int(raw.get("poll_jitter_seconds", 60)),
        source_backoff_max_seconds=int(raw.get("source_backoff_max_seconds", 3600)),
        feedback_poll_interval_seconds=int(raw.get("feedback_poll_interval_seconds", 10)),
        bootstrap_mode=raw.get("bootstrap_mode", "send_latest"),
        max_initial_notifications=int(raw.get("max_initial_notifications", 1)),
        max_notifications_per_run=int(raw.get("max_notifications_per_run", 10)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 30)),
        profiles=[SearchProfile(**profile) for profile in raw.get("profiles", [])],
        cyklobazar_profiles=[
            CyklobazarProfile(**profile) for profile in raw.get("cyklobazar_profiles", [])
        ],
        telegram=TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", telegram_raw.get("bot_token", "")),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", str(telegram_raw.get("chat_id", ""))),
        ),
        retail=RetailConfig(
            enabled=bool(retail_raw.get("enabled", True)),
            sources=[str(item) for item in retail_raw.get("sources", ["zbozi"])],
            max_enrichments_per_run=int(retail_raw.get("max_enrichments_per_run", 3)),
            min_comparables=int(retail_raw.get("min_comparables", 3)),
            identity_min_confidence=float(retail_raw.get("identity_min_confidence", 0.7)),
            exact_match_threshold=float(retail_raw.get("exact_match_threshold", 0.85)),
            ambiguous_match_threshold=float(retail_raw.get("ambiguous_match_threshold", 0.7)),
            success_cache_hours=int(retail_raw.get("success_cache_hours", 336)),
            insufficient_cache_hours=int(retail_raw.get("insufficient_cache_hours", 48)),
            error_cache_hours=int(retail_raw.get("error_cache_hours", 1)),
            source_timeout_seconds=int(retail_raw.get("source_timeout_seconds", 15)),
            total_timeout_seconds=int(retail_raw.get("total_timeout_seconds", 45)),
            max_parallel_requests=int(retail_raw.get("max_parallel_requests", 3)),
            max_queries_per_source=int(retail_raw.get("max_queries_per_source", 2)),
            max_product_pages=int(retail_raw.get("max_product_pages", 3)),
            max_offers_kept=int(retail_raw.get("max_offers_kept", 7)),
            max_telegram_sources=int(retail_raw.get("max_telegram_sources", 5)),
            outlier_low_ratio=float(retail_raw.get("outlier_low_ratio", 0.55)),
            outlier_high_ratio=float(retail_raw.get("outlier_high_ratio", 1.8)),
            target_currency=str(retail_raw.get("target_currency", "CZK")).upper(),
            currency_rates_to_czk={
                str(key).upper(): float(value)
                for key, value in retail_raw.get("currency_rates_to_czk", {"CZK": 1.0}).items()
            },
            codex_enabled=(codex_env in {"1", "true", "yes", "on"}) if codex_env else bool(retail_raw.get("codex_enabled", False)),
            codex_path=os.getenv("CODEX_PATH", str(retail_raw.get("codex_path", "codex"))),
            codex_schema_path=str(retail_raw.get("codex_schema_path", "schemas/bike_match.schema.json")),
            codex_timeout_seconds=int(retail_raw.get("codex_timeout_seconds", 60)),
            codex_calls_per_hour=int(retail_raw.get("codex_calls_per_hour", 3)),
            codex_calls_per_day=int(retail_raw.get("codex_calls_per_day", 10)),
        ),
        scoring=ScoringConfig.from_dict(raw.get("scoring")),
    )
    config.validate()
    return config
