from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


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


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None or not value.strip() else float(value)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None or not value.strip() else int(value)


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


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
class FacebookMarketplaceProfile:
    """Один поиск по Facebook Marketplace через ScrapeCreators.

    Координаты вместо URL: у Marketplace нет ни RSS, ни стабильной страницы
    выдачи, поиск задаётся точкой и радиусом.
    """

    name: str
    query: str
    lat: float
    lng: float
    radius_km: int = 60
    enabled: bool = True
    # Валюта, которую ожидаем от площадки в этой локации. Ответ ScrapeCreators
    # отдаёт цену строкой вида "12 000 Kč", символ бывает неоднозначным, поэтому
    # у профиля есть ожидание — но оно только запасной вариант, а не подмена.
    expected_currency: str = "CZK"
    min_price: int | None = None
    max_price: int | None = None
    condition: str = ""  # new | used_like_new | used_good | used_fair
    require_any_keywords: list[str] = field(default_factory=list)
    exclude_title_keywords: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.name or not self.query:
            raise ValueError("facebook_marketplace profile needs a name and a query")
        if not -90 <= self.lat <= 90 or not -180 <= self.lng <= 180:
            raise ValueError(f"Profile {self.name!r} has coordinates outside the world")
        if not 1 <= self.radius_km <= 500:
            raise ValueError(f"Profile {self.name!r} must use a radius between 1 and 500 km")
        if len(self.expected_currency) != 3:
            raise ValueError(f"Profile {self.name!r} needs a three-letter currency code")
        allowed_conditions = {"", "new", "used_like_new", "used_good", "used_fair"}
        if self.condition not in allowed_conditions:
            raise ValueError(f"Profile {self.name!r} has an unknown condition {self.condition!r}")
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError(f"Profile {self.name!r} has an inverted price range")


@dataclass(slots=True)
class FacebookMarketplaceConfig:
    """Источник Facebook Marketplace поверх ScrapeCreators API.

    Каждый поиск стоит кредит, поэтому расход ограничен со всех сторон: один
    запрос на профиль за цикл, страницы только при ``has_next_page``, детальные
    карточки по умолчанию выключены.
    """

    enabled: bool = False
    api_key: str = ""  # только из окружения, в JSON не хранится
    base_url: str = "https://api.scrapecreators.com/v1/facebook/marketplace"
    timeout_seconds: int = 30
    max_retries: int = 2
    max_pages_per_profile: int = 1
    results_per_page: int = 24
    # Поиск отдаёт только заголовок, цену и одно фото. Страница объявления даёт
    # описание, все фотографии, настоящий код валюты и дату публикации — то
    # есть всё, на чём AI вообще может опознать модель. Каждая такая карточка
    # стоит ещё один credit, поэтому включается отдельно и с потолком за цикл.
    detail_enabled: bool = False
    max_details_per_run: int = 0
    # Потолок за сутки UTC поверх потолка за цикл: объявление обогащается один
    # раз в жизни, но в активный день новых карточек может быть много, а запас
    # credits конечен. Счётчик переживает перезапуск (лежит в metadata).
    max_details_per_day: int = 40
    # Сколько фотографий уходит в AI. Больше кадров — лучше опознание рамы и
    # навесного, но и дороже вызов модели.
    max_photos_per_listing: int = 5
    date_listed: str = "last_24_hours"
    # Общий цикл проекта короче интервала (по умолчанию 420 секунд), а каждый
    # поиск здесь стоит денег: без собственного интервала один профиль сжёг бы
    # все 6100 credits за месяц. Источник опрашивается не чаще этого значения —
    # 1200 секунд (20 минут) даёт 2160 credits в месяц на профиль.
    min_interval_seconds: int = 1200
    profiles: list[FacebookMarketplaceProfile] = field(default_factory=list)

    def validate(self) -> None:
        if urlparse(self.base_url).scheme != "https":
            raise ValueError("facebook_marketplace.base_url must use HTTPS")
        if not 5 <= self.timeout_seconds <= 300:
            raise ValueError("facebook_marketplace.timeout_seconds must be between 5 and 300")
        if not 0 <= self.max_retries <= 5:
            raise ValueError("facebook_marketplace.max_retries must be between 0 and 5")
        if not 1 <= self.max_pages_per_profile <= 10:
            raise ValueError("facebook_marketplace.max_pages_per_profile must be between 1 and 10")
        if not 1 <= self.results_per_page <= 100:
            raise ValueError("facebook_marketplace.results_per_page must be between 1 and 100")
        if self.max_details_per_run < 0:
            raise ValueError("facebook_marketplace.max_details_per_run must not be negative")
        if not 1 <= self.max_photos_per_listing <= 10:
            raise ValueError("facebook_marketplace.max_photos_per_listing must be between 1 and 10")
        if self.detail_enabled and self.max_details_per_run <= 0:
            raise ValueError(
                "facebook_marketplace.detail_enabled needs a positive max_details_per_run"
            )
        if self.max_details_per_day < 0:
            raise ValueError("facebook_marketplace.max_details_per_day must not be negative")
        if self.min_interval_seconds < 0:
            raise ValueError("facebook_marketplace.min_interval_seconds must not be negative")
        allowed_dates = {"all", "1", "7", "30", "last_24_hours", "last_7_days", "last_30_days"}
        if self.date_listed not in allowed_dates:
            raise ValueError(f"facebook_marketplace.date_listed must be one of {sorted(allowed_dates)}")
        for profile in self.profiles:
            profile.validate()

    @property
    def active_profiles(self) -> list[FacebookMarketplaceProfile]:
        return [profile for profile in self.profiles if profile.enabled]

    def credits_per_month(self, polls_per_day: int | None = None) -> int:
        """Верхняя граница расхода за месяц (раздел «Экономия credits»).

        Считается по собственному интервалу источника, а не по общему циклу:
        именно он и определяет, сколько запросов реально уходит.
        """

        if polls_per_day is None:
            polls_per_day = (
                86_400 // self.min_interval_seconds if self.min_interval_seconds > 0 else 86_400
            )
        return len(self.active_profiles) * polls_per_day * 30 * self.max_pages_per_profile


@dataclass(slots=True)
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""


@dataclass(slots=True)
class IdentityConfig:
    """Параметры распознавания бренда и модели велосипеда (этап 1)."""

    catalog_enabled: bool = True
    catalog_path: str = ""  # пусто -> файл, поставляемый вместе с пакетом
    fuzzy_match_cutoff: float = 0.90
    brand_score: float = 0.40
    confirmed_model_score: float = 0.40
    candidate_model_score: float = 0.20
    unconfirmed_confidence_cap: float = 0.65

    def validate(self) -> None:
        scores = (
            self.brand_score,
            self.confirmed_model_score,
            self.candidate_model_score,
            self.unconfirmed_confidence_cap,
        )
        if any(not 0 <= value <= 1 for value in scores):
            raise ValueError("identity scores must be between 0 and 1")
        if self.candidate_model_score > self.confirmed_model_score:
            raise ValueError(
                "identity.candidate_model_score must not exceed identity.confirmed_model_score"
            )
        if self.brand_score + self.candidate_model_score > self.unconfirmed_confidence_cap:
            raise ValueError(
                "identity.unconfirmed_confidence_cap must cover brand_score + candidate_model_score"
            )
        if not 0.5 <= self.fuzzy_match_cutoff <= 1:
            raise ValueError("identity.fuzzy_match_cutoff must be between 0.5 and 1")


@dataclass(slots=True)
class AIConfig:
    """AI Analysis Level 1 поверх OpenAI Responses API.

    Цены за 1M токенов — публичный прайс-лист на 2026-08-08. Они меняются,
    поэтому вынесены в конфиг и переопределяются переменными окружения.
    """

    enabled: bool = False  # kill switch: AI_ANALYSIS_ENABLED
    shadow_mode: bool = True
    can_affect_deal_status: bool = False
    api_key: str = ""  # только из окружения, в JSON не хранится
    api_base_url: str = "https://api.openai.com/v1"
    model_primary: str = "gpt-5.6-luna"
    model_fallback: str = "gpt-5.6-terra"
    fallback_enabled: bool = True
    max_retries: int = 2
    timeout_seconds: int = 30
    max_parallel_requests: int = 4
    max_calls_per_cycle: int = 0  # 0 -> без лимита, бюджет ограничивают доллары
    max_title_chars: int = 300
    max_description_chars: int = 4000
    cache_ttl_hours: int = 720
    # Объявления с договорной ценой по умолчанию доходят до AI: раздел 4 ТЗ
    # прямо требует не отсекать короткие объявления вроде «Prodám Scott, spěchá».
    skip_listings_without_price: bool = False
    daily_budget_usd: float = 5.0
    warn_at_percent: float = 80.0
    stop_at_budget: bool = True
    primary_input_usd_per_1m: float = 0.20
    primary_cached_input_usd_per_1m: float = 0.02
    primary_output_usd_per_1m: float = 1.20
    fallback_input_usd_per_1m: float = 2.00
    fallback_cached_input_usd_per_1m: float = 0.20
    fallback_output_usd_per_1m: float = 12.00
    prompt_name: str = "listing-analysis"
    prompt_version: str = "v1.3.0"
    prompts_path: str = ""  # пусто -> каталог, поставляемый вместе с пакетом

    # --- Зрение: первое фото объявления уходит в Level 1 --------------------
    # По фото модель надёжнее отличает настоящий велосипед от аксессуара
    # (детское кресло, велотуфли, спиннинг-тренажёр) и опознаёт тип, когда текст
    # пустой («Prodám kolo»). Модель зрения по умолчанию — основная (Luna);
    # пусто здесь означает model_primary. detail="low" держит расход умеренным.
    vision_enabled: bool = True
    vision_model: str = ""
    vision_detail: str = "low"  # low | high | auto
    # Сколько кадров уходит в один вызов. По обложке видно, велосипед это или
    # шлем; чтобы назвать модель, нужны рама, привод и навесное с разных
    # ракурсов. Площадки, отдающие одно фото, ничего не теряют.
    vision_max_images: int = 5
    # Классификации разрешено убирать карточку, только когда она уверенно
    # решила, что объявление — не велосипед. Порог намеренно высокий: пустой
    # результат лучше, чем спрятанный настоящий велосипед. Само подавление
    # включается лишь при can_affect_deal_status=true и shadow_mode=false.
    min_reject_confidence: float = 0.75

    # --- Level 2: AI-оценка цены перепродажи -------------------------------
    # Отдельный kill switch: извлечение фактов и оценка денег включаются
    # независимо, потому что риск у них разный.
    price_estimate_enabled: bool = False
    # Оценка цены — задача на рассуждение, поэтому модель сильнее, чем на
    # извлечении фактов. Расход держит кэш по идентичности велосипеда.
    price_model: str = "gpt-5.6-terra"
    price_input_usd_per_1m: float = 2.00
    price_cached_input_usd_per_1m: float = 0.20
    price_output_usd_per_1m: float = 12.00
    price_prompt_name: str = "price-estimate"
    price_prompt_version: str = "v1.0.0"
    # Цена б/у велосипеда меняется медленно, а кэш ключуется по модели и
    # состоянию, а не по объявлению — поэтому TTL длинный.
    price_cache_ttl_hours: int = 720
    # Запускать оценку, когда реальных аналогов нет вовсе либо их слишком мало.
    price_min_comparables: int = 3
    price_on_low_confidence: bool = True
    # Главный предохранитель: догадка модели не должна становиться сигналом
    # «ехать покупать». HOT требует подтверждённых аналогов с площадок.
    price_allow_hot: bool = False
    # Границы вменяемости ответа: всё за их пределами означает, что модель
    # неправильно поняла объявление, и такая оценка отбрасывается.
    price_floor_czk: int = 500
    price_ceiling_czk: int = 500_000
    price_max_ratio_to_asking: float = 8.0
    price_min_ratio_to_asking: float = 0.15
    price_max_range_ratio: float = 2.5

    def validate(self) -> None:
        if urlparse(self.api_base_url).scheme != "https":
            raise ValueError("ai.api_base_url must use HTTPS")
        if not self.model_primary:
            raise ValueError("ai.model_primary must not be empty")
        if self.fallback_enabled and not self.model_fallback:
            raise ValueError("ai.model_fallback must not be empty when fallback is enabled")
        if not 0 <= self.max_retries <= 5:
            raise ValueError("ai.max_retries must be between 0 and 5")
        if not 5 <= self.timeout_seconds <= 300:
            raise ValueError("ai.timeout_seconds must be between 5 and 300")
        if not 1 <= self.max_parallel_requests <= 16:
            raise ValueError("ai.max_parallel_requests must be between 1 and 16")
        if self.max_calls_per_cycle < 0:
            raise ValueError("ai.max_calls_per_cycle must not be negative")
        if self.max_title_chars < 50 or self.max_description_chars < 200:
            raise ValueError("ai text limits are too small to analyse a listing")
        if self.vision_detail not in {"low", "high", "auto"}:
            raise ValueError("ai.vision_detail must be one of low, high, auto")
        if not 1 <= self.vision_max_images <= 10:
            raise ValueError("ai.vision_max_images must be between 1 and 10")
        if not 0 < self.min_reject_confidence <= 1:
            raise ValueError("ai.min_reject_confidence must be between 0 and 1")
        if self.cache_ttl_hours < 0:
            raise ValueError("ai.cache_ttl_hours must not be negative")
        if self.daily_budget_usd < 0:
            raise ValueError("ai.daily_budget_usd must not be negative")
        if not 0 < self.warn_at_percent <= 100:
            raise ValueError("ai.warn_at_percent must be between 0 and 100")
        prices = (
            self.primary_input_usd_per_1m,
            self.primary_cached_input_usd_per_1m,
            self.primary_output_usd_per_1m,
            self.fallback_input_usd_per_1m,
            self.fallback_cached_input_usd_per_1m,
            self.fallback_output_usd_per_1m,
            self.price_input_usd_per_1m,
            self.price_cached_input_usd_per_1m,
            self.price_output_usd_per_1m,
        )
        if any(price < 0 for price in prices):
            raise ValueError("ai token prices must not be negative")
        if self.price_estimate_enabled and not self.price_model:
            raise ValueError("ai.price_model must not be empty when price estimates are enabled")
        if self.price_cache_ttl_hours < 0:
            raise ValueError("ai.price_cache_ttl_hours must not be negative")
        if self.price_min_comparables < 0:
            raise ValueError("ai.price_min_comparables must not be negative")
        if not 0 < self.price_floor_czk < self.price_ceiling_czk:
            raise ValueError("ai.price_floor_czk must be positive and below ai.price_ceiling_czk")
        if not 0 < self.price_min_ratio_to_asking < 1 < self.price_max_ratio_to_asking:
            raise ValueError(
                "ai.price_min_ratio_to_asking must be below 1 and ai.price_max_ratio_to_asking above 1"
            )
        if self.price_max_range_ratio < 1:
            raise ValueError("ai.price_max_range_ratio must not be below 1")


@dataclass(slots=True)
class RetailConfig:
    enabled: bool = True
    sources: list[str] = field(default_factory=lambda: ["zbozi"])
    max_enrichments_per_run: int = 3
    min_comparables: int = 3
    min_offer_match_score: float = 0.78
    two_source_max_spread_ratio: float = 0.35
    three_source_max_spread_ratio: float = 0.25
    low_confidence_max_spread_ratio: float = 0.50
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
    lookup_delay_seconds: float = 0.25
    max_consecutive_source_errors: int = 3

    def validate(self) -> None:
        if self.min_comparables < 1:
            raise ValueError("retail.min_comparables must be positive")
        if not 0 < self.min_offer_match_score <= 1:
            raise ValueError("retail.min_offer_match_score must be between 0 and 1")
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


def _default_brand_market_mapping() -> dict[str, list[str]]:
    germany = ["Cube", "Canyon", "Rose", "Focus", "Haibike", "Bulls", "Ghost", "Corratec", "Stevens"]
    netherlands = ["Gazelle", "Batavus", "Koga", "Cortina", "Sparta"]
    mapping = {brand: ["DE"] for brand in germany}
    mapping.update({brand: ["NL"] for brand in netherlands})
    mapping.update({brand: ["PL", "EU"] for brand in ["Kross"]})
    mapping.update({brand: ["CZ", "SK"] for brand in ["Author", "Rock Machine"]})
    mapping.update({brand: ["ES", "EU"] for brand in ["Orbea"]})
    mapping.update({brand: ["IT", "EU"] for brand in ["Bianchi", "Pinarello", "Colnago"]})
    mapping.update(
        {
            brand: ["DE", "EU"]
            for brand in ["Trek", "Specialized", "Giant", "Merida", "Scott", "Cannondale"]
        }
    )
    return mapping


def _default_market_weights() -> dict[str, float]:
    return {
        "exact_cz": 1.00,
        "close_cz": 0.85,
        "model_family_cz": 0.60,
        "component_class_cz": 0.35,
        "exact_foreign": 0.70,
        "close_foreign": 0.55,
        "model_family_foreign": 0.35,
        "component_class_foreign": 0.20,
    }


@dataclass(slots=True)
class MarketPricingConfig:
    enabled: bool = False
    sources: list[str] = field(
        default_factory=lambda: ["bazos_cz", "kleinanzeigen_de", "marktplaats_nl", "buycycle_eu"]
    )
    quick_sale_discount: float = 0.15
    max_foreign_markets_per_model: int = 2
    minimum_unique_comparables_before_fallback: int = 3
    foreign_market_disagreement_threshold: float = 0.30
    max_comparable_age_days: int = 180
    max_results_per_source: int = 25
    max_queries_per_source: int = 1
    source_timeout_seconds: int = 20
    total_timeout_seconds: int = 70
    circuit_breaker_errors: int = 3
    circuit_breaker_cooldown_minutes: int = 30
    expensive_rounding_threshold_czk: int = 50000
    cheap_rounding_step_czk: int = 100
    expensive_rounding_step_czk: int = 500
    exchange_rate_url: str = (
        "https://www.cnb.cz/en/financial-markets/foreign-exchange-market/"
        "central-bank-exchange-rate-fixing/central-bank-exchange-rate-fixing/daily.txt"
    )
    country_market_adjustment: dict[str, float] = field(
        default_factory=lambda: {
            "CZ": 1.0,
            "DE": 1.0,
            "NL": 1.0,
            "SK": 1.0,
            "PL": 1.0,
            "AT": 1.0,
            "ES": 1.0,
            "IT": 1.0,
            "EU": 1.0,
        }
    )
    brand_market_mapping: dict[str, list[str]] = field(default_factory=_default_brand_market_mapping)
    category_market_mapping: dict[str, list[str]] = field(
        default_factory=lambda: {"city": ["NL", "DE"], "mountain": ["DE", "NL"], "road": ["DE", "EU"], "gravel": ["DE", "EU"]}
    )
    weights: dict[str, float] = field(default_factory=_default_market_weights)
    cache_ttl_hours: dict[str, int] = field(
        default_factory=lambda: {
            "high_confidence": 168,
            "low_confidence": 72,
            "not_found": 48,
            "source_error": 1,
            "exchange_rate": 24,
        }
    )
    depreciation_by_age: dict[str, float] = field(
        default_factory=lambda: {"0": 0.80, "1": 0.70, "2": 0.62, "3": 0.55, "4": 0.48, "5": 0.42, "6+": 0.35}
    )
    auto_calibration_enabled: bool = True
    auto_calibration_minimum_model_pairs: int = 10

    def validate(self) -> None:
        if not 0 < self.quick_sale_discount < 1:
            raise ValueError("market_pricing.quick_sale_discount must be between 0 and 1")
        if self.max_foreign_markets_per_model not in {1, 2}:
            raise ValueError("market_pricing.max_foreign_markets_per_model must be 1 or 2")
        if not 0 < self.foreign_market_disagreement_threshold <= 1:
            raise ValueError("market_pricing disagreement threshold must be between 0 and 1")
        allowed_sources = {"bazos_cz", "kleinanzeigen_de", "marktplaats_nl", "buycycle_eu"}
        unknown = set(self.sources) - allowed_sources
        if unknown:
            raise ValueError(f"Unknown market pricing sources: {sorted(unknown)}")
        if any(value <= 0 for value in self.weights.values()):
            raise ValueError("market_pricing weights must be positive")


def _default_priority_weights() -> dict[str, int]:
    return {
        "very_fresh": 15,
        "brand_recognized": 5,
        "model_recognized": 10,
        "numeric_price": 5,
        "description": 5,
        "year_known": 3,
        "size_known": 3,
        "location_match": 2,
        "cached_new_price": 10,
        "new_discount_15": 5,
        "new_discount_30": 12,
        "new_discount_45": 20,
        "new_discount_60": 25,
        "used_discount_15": 7,
        "used_discount_30": 12,
        "negotiable": 3,
        "urgent_sale": 5,
    }


@dataclass(slots=True)
class PriorityConfig:
    urgent_min_score: int = 80
    interesting_min_score: int = 60
    manual_review_min_score: int = 35
    low_priority_min_score: int = 1
    max_telegram_cards: int = 10
    manual_review_reserved_slots: int = 2
    individual_notification_max_age_hours: int = 6
    used_comparable_max_age_days: int = 30
    very_fresh_max_hours: int = 6
    description_min_chars: int = 80
    suspicious_discount_percent: int = 80
    lookup_all_max_count: int = 10
    lookup_mid_max_count: int = 30
    lookup_high_max_count: int = 70
    lookup_mid_limit: int = 10
    lookup_high_limit: int = 15
    lookup_max_limit: int = 20
    lookup_large_fraction: float = 0.20
    duplicate_confirmed_similarity: float = 0.95
    duplicate_possible_similarity: float = 0.85
    duplicate_canonical_seen_tie_seconds: int = 300
    weights: dict[str, int] = field(default_factory=_default_priority_weights)

    def validate(self) -> None:
        if not 0 <= self.low_priority_min_score <= self.manual_review_min_score <= self.interesting_min_score <= self.urgent_min_score <= 100:
            raise ValueError("priority score thresholds must be ordered between 0 and 100")
        if self.max_telegram_cards < 1:
            raise ValueError("priority.max_telegram_cards must be positive")
        if not 0 <= self.manual_review_reserved_slots <= self.max_telegram_cards:
            raise ValueError("priority.manual_review_reserved_slots is out of range")
        if self.used_comparable_max_age_days < 1 or self.individual_notification_max_age_hours < 1:
            raise ValueError("priority age limits must be positive")
        if not 0 < self.lookup_large_fraction <= 1 or self.lookup_max_limit < self.lookup_high_limit:
            raise ValueError("priority dynamic lookup budget settings are invalid")
        if not 0 < self.duplicate_possible_similarity < self.duplicate_confirmed_similarity <= 1:
            raise ValueError("priority duplicate similarity thresholds are invalid")


def _default_deal_score_weights() -> dict[str, float]:
    return {
        "profit": 25.0,
        "roi": 25.0,
        "liquidity": 20.0,
        "confidence": 15.0,
        "condition": 10.0,
        "risk": 5.0,
    }


@dataclass(slots=True)
class DealScoringConfig:
    enabled: bool = False
    algorithm_version: str = "2.2.0"
    risk_reserve_percent: float = 10.0
    hot_min_profit_czk: float = 4000.0
    hot_min_roi_percent: float = 20.0
    interesting_min_roi_percent: float = 15.0
    high_roi_percent: float = 100.0
    hot_min_liquidity_score: int = 50
    hot_min_confidence_score: int = 50
    manual_review_on_low_confidence: bool = True
    telegram_send_hot: bool = True
    telegram_send_interesting: bool = True
    telegram_send_manual_review: bool = True
    telegram_send_low_priority: bool = False
    telegram_send_reject: bool = False
    liquidity_high_min_score: int = 70
    liquidity_medium_min_score: int = 50
    liquidity_exact_points: int = 12
    liquidity_close_points: int = 8
    liquidity_family_points: int = 4
    liquidity_component_points: int = 2
    liquidity_source_points: int = 5
    liquidity_source_count_cap: int = 4
    liquidity_czech_bonus: int = 10
    estimated_days_high: int = 21
    estimated_days_medium: int = 45
    estimated_days_low: int = 75
    confidence_scores: dict[str, int] = field(
        default_factory=lambda: {"high": 85, "medium": 65, "low": 35}
    )
    minimum_identity_confidence: float = 0.55
    # Оценка цены, полученная от AI, не подтверждена аналогами с площадок.
    # Такая сделка может дойти до INTERESTING, но не до HOT: HOT читается как
    # «ехать покупать», и выдавать его на догадке модели нельзя.
    allow_hot_on_ai_price: bool = False
    require_confirmed_model: bool = True
    price_outlier_discount_percent: float = 80.0
    profit_score_target_czk: float = 8000.0
    roi_score_target_percent: float = 100.0
    risk_penalty_per_flag: float = 20.0
    score_weights: dict[str, float] = field(default_factory=_default_deal_score_weights)
    condition_scores: dict[str, float] = field(
        default_factory=lambda: {
            "excellent": 100.0,
            "good": 85.0,
            "unknown": 50.0,
            "service_required": 20.0,
            "problematic": 10.0,
            "contradictory": 10.0,
        }
    )
    positive_condition_terms: list[str] = field(
        default_factory=lambda: [
            "bez investic",
            "bez dalsich investic",
            "bez nutnosti servisu",
            "po servisu",
            "nove servisovano",
            "pravidelny servis",
            "servis neni potreba",
            "pripraveno k jizde",
            "pripravene k jizde",
            "pripravene na okamzite vyjeti",
            "ihned k pouziti",
            "staci sednout a jet",
            "jen nasednout a jet",
            "plne funkcni",
            "vse 100 funkcni",
            "vse funguje na 100",
            "pravidelne servisovane",
            "vse zavcasu servisovano",
            "dobrem stavu",
            "peknem stavu",
            "idealnim stavu",
            "perfektnim stavu",
            "vybornem stavu",
            "bezvadny stav",
            "jako nove",
            "zachovale",
            "udrzovane",
            "top stav",
            "perfektni stav",
            "velmi dobry stav",
            "vyborny stav",
            "ready to ride",
            "fully serviced",
        ]
    )
    completed_service_terms: list[str] = field(
        default_factory=lambda: [
            "po servisu",
            "nove servisovano",
            "bez nutnosti servisu",
            "servis neni potreba",
            "delal jsem servis",
            "servis byl proveden",
            "proveden servis",
            "po kompletnim servisu",
            "fully serviced",
        ]
    )
    service_required_terms: list[str] = field(
        default_factory=lambda: [
            "nutny servis",
            "potrebuje servis",
            "pred servisem",
            "servis vidlice",
            "servis tlumice",
            "vymena retezu",
            "nutna vymena",
            "needs service",
            "requires service",
            "service required",
        ]
    )
    problem_condition_terms: list[str] = field(
        default_factory=lambda: [
            "na opravu",
            "nepojizdne",
            "poskozene",
            "praskly ram",
            "nefunkcni",
            "repair needed",
            "not working",
            "damaged frame",
        ]
    )
    critical_valuation_statuses: list[str] = field(
        default_factory=lambda: [
            "insufficient_comparables",
            "ambiguous_model",
            "currency_error",
            "source_unavailable",
            "duplicate_only_results",
            "no_matching_comparables",
        ]
    )
    critical_warning_terms: list[str] = field(
        default_factory=lambda: [
            "критичес",
            "неоднознач",
            "несовместим",
            "critical",
            "ambiguous",
            "mismatch",
        ]
    )
    manual_review_risk_terms: list[str] = field(
        default_factory=lambda: [
            "модель не подтверждена",
            "цена подозрительно низкая",
            "числовая цена продавца отсутствует",
            "требуется ручная проверка",
            "model is ambiguous",
            "suspiciously low price",
            "manual review required",
        ]
    )

    def validate(self) -> None:
        if not 0 <= self.risk_reserve_percent <= 100:
            raise ValueError("deal_scoring.risk_reserve_percent must be between 0 and 100")
        if not 0 <= self.interesting_min_roi_percent <= self.hot_min_roi_percent:
            raise ValueError("deal_scoring ROI thresholds are invalid")
        if self.high_roi_percent < self.hot_min_roi_percent:
            raise ValueError("deal_scoring.high_roi_percent must not be below HOT ROI")
        if not 0 <= self.hot_min_liquidity_score <= 100:
            raise ValueError("deal_scoring.hot_min_liquidity_score must be between 0 and 100")
        if not 0 <= self.hot_min_confidence_score <= 100:
            raise ValueError("deal_scoring.hot_min_confidence_score must be between 0 and 100")
        if not 0 <= self.liquidity_medium_min_score <= self.liquidity_high_min_score <= 100:
            raise ValueError("deal_scoring liquidity thresholds are invalid")
        if self.profit_score_target_czk <= 0 or self.roi_score_target_percent <= 0:
            raise ValueError("deal_scoring component score targets must be positive")
        if not self.score_weights or any(value < 0 for value in self.score_weights.values()):
            raise ValueError("deal_scoring score weights must be non-negative")
        if sum(self.score_weights.values()) <= 0:
            raise ValueError("deal_scoring score weights must have a positive sum")
        if any(not 0 <= value <= 100 for value in self.confidence_scores.values()):
            raise ValueError("deal_scoring confidence scores must be between 0 and 100")
        if any(not 0 <= value <= 100 for value in self.condition_scores.values()):
            raise ValueError("deal_scoring condition scores must be between 0 and 100")


def _default_risk_severity() -> dict[str, str]:
    """Тяжесть риск-флагов Level 1. Новых флагов не вводим: слева — значения
    enum из ``schema.json`` и булевы поля блока ``risk``; ``*_MISSING_PARTS`` и
    ``DAMAGED_CONDITION`` собираются из существующего блока ``condition``."""

    return {
        "NOT_A_BICYCLE": "blocking",
        "PRICE_TOO_LOW": "minor",
        "SUSPICIOUS_PRICE": "minor",
        "NO_PHOTOS": "minor",
        "VAGUE_DESCRIPTION": "minor",
        "MISSING_PARTS": "minor",
        "NO_DOCUMENTS": "medium",
        "SHIPPING_ONLY": "medium",
        "SELLER_AVOIDS_DETAILS": "medium",
        "CONTRADICTORY_INFO": "medium",
        "DAMAGED_CONDITION": "medium",
        "EBIKE_MISSING_PARTS": "medium",
        "ADVANCE_PAYMENT_REQUESTED": "severe",
        "EXTERNAL_INSTRUCTIONS": "severe",
        "SCAM_RISK": "blocking",
        "STOLEN_RISK": "blocking",
    }


RISK_SEVERITIES = ("info", "minor", "medium", "severe", "blocking")


@dataclass(slots=True)
class AIGateConfig:
    """AI Opportunity Gate: насколько объявление достойно дальнейшей проверки.

    Слой детерминированный и нового запроса к модели не делает — он читает уже
    сохранённый разбор Level 1. Главный принцип: ``listing_quality=LOW`` само по
    себе почти ничего не стоит, потому что плохо оформленные объявления и есть
    самая интересная зона для перепродажи. Наказывается не небрежность
    продавца, а отсутствие информации вместе с отсутствием причины.
    """

    enabled: bool = True
    # Нейтральная середина: с неё начинают и объявления без AI-разбора, поэтому
    # отсутствие анализа не отбрасывает объявление в конец очереди.
    base_score: int = 25
    analysis_min_score: int = 30
    hidden_opportunity_bonus: int = 30
    seller_urgency_bonus: dict[str, int] = field(
        default_factory=lambda: {"HIGH": 20, "MEDIUM": 8, "LOW": 0, "UNKNOWN": 0}
    )
    listing_quality_bonus: dict[str, int] = field(
        default_factory=lambda: {"HIGH": 5, "MEDIUM": 3, "LOW": 0, "UNKNOWN": 0}
    )
    identity_high_threshold: float = 0.80
    identity_medium_threshold: float = 0.55
    identity_high_bonus: int = 10
    identity_medium_bonus: int = 5
    unknown_identity_penalty: int = 5
    # Бонус за факты, которых не дал детерминированный разбор: модель, год, тип,
    # размер рамы и колёс, трансмиссия, материал, состояние.
    valuable_information_bonus: int = 5
    valuable_information_min_facts: int = 3
    risk_penalty: dict[str, int] = field(
        default_factory=lambda: {
            "info": 0,
            "minor": 5,
            "medium": 15,
            "severe": 40,
            "blocking": 40,
        }
    )
    risk_penalty_max: int = 40
    risk_severity: dict[str, str] = field(default_factory=_default_risk_severity)
    # Вердикт «это не велосипед» с уверенностью ниже порога подавления всё равно
    # не должен доходить до Telegram: карточка остаётся в базе, но не уходит.
    block_non_bicycle: bool = True

    def validate(self) -> None:
        if not 0 <= self.base_score <= 100:
            raise ValueError("ai_opportunity_gate.base_score must be between 0 and 100")
        if not 0 <= self.analysis_min_score <= 100:
            raise ValueError("ai_opportunity_gate.analysis_min_score must be between 0 and 100")
        if not 0 <= self.identity_medium_threshold <= self.identity_high_threshold <= 1:
            raise ValueError("ai_opportunity_gate identity thresholds must satisfy 0 <= medium <= high <= 1")
        if self.risk_penalty_max < 0:
            raise ValueError("ai_opportunity_gate.risk_penalty_max must not be negative")
        if any(value < 0 for value in self.risk_penalty.values()):
            raise ValueError("ai_opportunity_gate risk penalties must not be negative")
        unknown_severities = set(self.risk_penalty) - set(RISK_SEVERITIES)
        unknown_severities |= set(self.risk_severity.values()) - set(RISK_SEVERITIES)
        if unknown_severities:
            raise ValueError(f"Unknown risk severities: {sorted(unknown_severities)}")


@dataclass(slots=True)
class TelegramGateConfig:
    """Telegram Notification Gate: насколько объявление достойно внимания человека.

    Второй, независимый от analysis priority score. Деньги, ликвидность и
    уверенность берутся готовыми из оценки сделки — новых финансовых формул
    здесь нет; добавляются только сигналы AI, которых у ``deal_score`` нет.
    """

    enabled: bool = True
    hot_min_score: int = 0
    interesting_min_score: int = 40
    manual_review_min_score: int = 60
    # Спасательный круг для MANUAL_REVIEW: сильный сигнал отправляет карточку
    # даже при невысоком score. Выключение оставляет только порог по score.
    manual_review_require_strong_signal: bool = True
    strong_signals: list[str] = field(
        default_factory=lambda: ["hidden_opportunity", "seller_urgency_high", "price_anomaly"]
    )
    block_on_blocking_risk: bool = True
    status_base_score: dict[str, int] = field(
        default_factory=lambda: {
            "HOT": 55,
            "INTERESTING": 40,
            "MANUAL_REVIEW": 20,
            "LOW_PRIORITY": 5,
            "REJECT": 0,
        }
    )
    profit_signal_max: int = 20
    profit_signal_target_czk: float = 8000.0
    roi_signal_max: int = 15
    roi_signal_target_percent: float = 60.0
    liquidity_signal_max: int = 10
    confidence_signal_max: int = 10
    hidden_opportunity_bonus: int = 20
    urgency_high_bonus: int = 15
    urgency_medium_bonus: int = 6
    price_anomaly_bonus: int = 15
    price_anomaly_min_discount_percent: float = 35.0
    risk_penalty_max: int = 40

    def validate(self) -> None:
        thresholds = (
            self.hot_min_score,
            self.interesting_min_score,
            self.manual_review_min_score,
        )
        if any(not 0 <= value <= 100 for value in thresholds):
            raise ValueError("telegram_notification_gate thresholds must be between 0 and 100")
        if self.profit_signal_target_czk <= 0 or self.roi_signal_target_percent <= 0:
            raise ValueError("telegram_notification_gate signal targets must be positive")
        if not 0 <= self.price_anomaly_min_discount_percent <= 100:
            raise ValueError(
                "telegram_notification_gate.price_anomaly_min_discount_percent must be between 0 and 100"
            )
        known_signals = {"hidden_opportunity", "seller_urgency_high", "price_anomaly"}
        unknown = set(self.strong_signals) - known_signals
        if unknown:
            raise ValueError(f"Unknown telegram_notification_gate strong signals: {sorted(unknown)}")


@dataclass(slots=True)
class AppConfig:
    database_path: str = "data/deal_radar.sqlite3"
    poll_interval_seconds: int = 600
    poll_jitter_seconds: int = 60
    source_backoff_max_seconds: int = 3600
    feedback_poll_interval_seconds: int = 10
    bootstrap_mode: str = "send_latest"
    max_initial_notifications: int = 1
    max_notifications_per_run: int = 10
    request_timeout_seconds: int = 30
    # Cyklobazar отдаёт в списке только обрывок описания; характеристики лежат
    # на странице объявления. Потолок запросов за цикл — защита от бана.
    cyklobazar_detail_budget: int = 20
    profiles: list[SearchProfile] = field(default_factory=list)
    cyklobazar_profiles: list[CyklobazarProfile] = field(default_factory=list)
    facebook_marketplace: FacebookMarketplaceConfig = field(
        default_factory=FacebookMarketplaceConfig
    )
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    retail: RetailConfig = field(default_factory=RetailConfig)
    market_pricing: MarketPricingConfig = field(default_factory=MarketPricingConfig)
    priority: PriorityConfig = field(default_factory=PriorityConfig)
    deal_scoring: DealScoringConfig = field(default_factory=DealScoringConfig)
    ai_gate: AIGateConfig = field(default_factory=AIGateConfig)
    telegram_gate: TelegramGateConfig = field(default_factory=TelegramGateConfig)

    @property
    def ai_signals_live(self) -> bool:
        """Читать ли сигналы AI в воротах.

        Shadow-режим означает ровно то, что обещает README: результат только
        сохраняется. Пока слой в тени, ворота работают на детерминированных
        данных сделки, а поля Level 1 не влияют ни на очередь анализа, ни на
        отправку.
        """

        return self.ai.enabled and not self.ai.shadow_mode

    def validate(self) -> None:
        if self.bootstrap_mode not in {"send_latest", "skip_existing", "send_all"}:
            raise ValueError("bootstrap_mode must be send_latest, skip_existing, or send_all")
        if not 60 <= self.poll_interval_seconds <= 86_400:
            raise ValueError("poll_interval_seconds must be between 60 and 86400")
        if not 2 <= self.feedback_poll_interval_seconds <= 300:
            raise ValueError("feedback_poll_interval_seconds must be between 2 and 300")
        if not 0 <= self.cyklobazar_detail_budget <= 100:
            raise ValueError("cyklobazar_detail_budget must be between 0 and 100")
        has_facebook = self.facebook_marketplace.enabled and bool(
            self.facebook_marketplace.active_profiles
        )
        if (
            not self.profiles
            and not any(profile.enabled for profile in self.cyklobazar_profiles)
            and not has_facebook
        ):
            raise ValueError("At least one marketplace search profile is required")
        for profile in self.profiles:
            profile.validate()
        for profile in self.cyklobazar_profiles:
            profile.validate()
        self.facebook_marketplace.validate()
        self.identity.validate()
        self.ai.validate()
        self.retail.validate()
        self.market_pricing.validate()
        self.priority.validate()
        self.deal_scoring.validate()
        self.ai_gate.validate()
        self.telegram_gate.validate()


def load_config(path: str | Path = "config.json") -> AppConfig:
    load_dotenv()
    config_path = Path(path)
    if not config_path.exists() and config_path.name == "config.json":
        config_path = Path("config.example.json")
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    telegram_raw = raw.get("telegram", {})
    identity_raw = raw.get("identity", {})
    ai_raw = raw.get("ai", {})
    retail_raw = raw.get("retail", {})
    market_raw = raw.get("market_pricing", {})
    priority_raw = raw.get("priority", {})
    deal_raw = raw.get("deal_scoring", {})
    facebook_raw = raw.get("facebook_marketplace", {})
    ai_gate_raw = raw.get("ai_opportunity_gate", {})
    telegram_gate_raw = raw.get("telegram_notification_gate", {})
    codex_env = os.getenv("DEAL_RADAR_CODEX_ENABLED", "").strip().casefold()
    config = AppConfig(
        database_path=raw.get("database_path", "data/deal_radar.sqlite3"),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 600)),
        poll_jitter_seconds=int(raw.get("poll_jitter_seconds", 60)),
        source_backoff_max_seconds=int(raw.get("source_backoff_max_seconds", 3600)),
        feedback_poll_interval_seconds=int(raw.get("feedback_poll_interval_seconds", 10)),
        bootstrap_mode=raw.get("bootstrap_mode", "send_latest"),
        max_initial_notifications=int(raw.get("max_initial_notifications", 1)),
        max_notifications_per_run=int(raw.get("max_notifications_per_run", 10)),
        cyklobazar_detail_budget=_env_int(
            "CYKLOBAZAR_DETAIL_BUDGET", int(raw.get("cyklobazar_detail_budget", 20))
        ),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 30)),
        profiles=[SearchProfile(**profile) for profile in raw.get("profiles", [])],
        cyklobazar_profiles=[
            CyklobazarProfile(**profile) for profile in raw.get("cyklobazar_profiles", [])
        ],
        facebook_marketplace=FacebookMarketplaceConfig(
            enabled=_env_bool(
                "FACEBOOK_MARKETPLACE_ENABLED", bool(facebook_raw.get("enabled", False))
            ),
            # Ключ живёт только в окружении: ни в JSON, ни в git, ни в логах.
            api_key=os.getenv("SCRAPECREATORS_API_KEY", "").strip(),
            base_url=str(
                facebook_raw.get("base_url", FacebookMarketplaceConfig().base_url)
            ),
            timeout_seconds=_env_int(
                "FACEBOOK_MARKETPLACE_TIMEOUT_SECONDS", int(facebook_raw.get("timeout_seconds", 30))
            ),
            max_retries=_env_int(
                "FACEBOOK_MARKETPLACE_MAX_RETRIES", int(facebook_raw.get("max_retries", 2))
            ),
            max_pages_per_profile=_env_int(
                "FACEBOOK_MARKETPLACE_MAX_PAGES_PER_PROFILE",
                int(facebook_raw.get("max_pages_per_profile", 1)),
            ),
            results_per_page=_env_int(
                "FACEBOOK_MARKETPLACE_RESULTS_PER_PAGE",
                int(facebook_raw.get("results_per_page", 24)),
            ),
            detail_enabled=_env_bool(
                "FACEBOOK_MARKETPLACE_DETAIL_ENABLED",
                bool(facebook_raw.get("detail_enabled", False)),
            ),
            max_details_per_run=_env_int(
                "FACEBOOK_MARKETPLACE_MAX_DETAILS_PER_RUN",
                int(facebook_raw.get("max_details_per_run", 0)),
            ),
            max_details_per_day=_env_int(
                "FACEBOOK_MARKETPLACE_MAX_DETAILS_PER_DAY",
                int(facebook_raw.get("max_details_per_day", 40)),
            ),
            max_photos_per_listing=_env_int(
                "FACEBOOK_MARKETPLACE_MAX_PHOTOS",
                int(facebook_raw.get("max_photos_per_listing", 5)),
            ),
            date_listed=str(facebook_raw.get("date_listed", "last_24_hours")),
            min_interval_seconds=_env_int(
                "FACEBOOK_MARKETPLACE_MIN_INTERVAL_SECONDS",
                int(facebook_raw.get("min_interval_seconds", 1200)),
            ),
            profiles=[
                FacebookMarketplaceProfile(**profile)
                for profile in facebook_raw.get("profiles", [])
            ],
        ),
        telegram=TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", telegram_raw.get("bot_token", "")),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", str(telegram_raw.get("chat_id", ""))),
        ),
        identity=IdentityConfig(
            catalog_enabled=bool(identity_raw.get("catalog_enabled", True)),
            catalog_path=str(identity_raw.get("catalog_path", "")),
            fuzzy_match_cutoff=float(identity_raw.get("fuzzy_match_cutoff", 0.90)),
            brand_score=float(identity_raw.get("brand_score", 0.40)),
            confirmed_model_score=float(identity_raw.get("confirmed_model_score", 0.40)),
            candidate_model_score=float(identity_raw.get("candidate_model_score", 0.20)),
            unconfirmed_confidence_cap=float(identity_raw.get("unconfirmed_confidence_cap", 0.65)),
        ),
        ai=AIConfig(
            enabled=_env_bool("AI_ANALYSIS_ENABLED", bool(ai_raw.get("enabled", False))),
            shadow_mode=_env_bool("AI_SHADOW_MODE", bool(ai_raw.get("shadow_mode", True))),
            can_affect_deal_status=_env_bool(
                "AI_CAN_AFFECT_DEAL_STATUS", bool(ai_raw.get("can_affect_deal_status", False))
            ),
            # Ключ читается только из окружения и никогда не хранится в JSON.
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            api_base_url=_env_str(
                "OPENAI_API_BASE_URL", str(ai_raw.get("api_base_url", "https://api.openai.com/v1"))
            ),
            model_primary=_env_str(
                "OPENAI_MODEL_PRIMARY", str(ai_raw.get("model_primary", "gpt-5.6-luna"))
            ),
            model_fallback=_env_str(
                "OPENAI_MODEL_FALLBACK", str(ai_raw.get("model_fallback", "gpt-5.6-terra"))
            ),
            fallback_enabled=_env_bool(
                "OPENAI_FALLBACK_ENABLED", bool(ai_raw.get("fallback_enabled", True))
            ),
            max_retries=_env_int("OPENAI_MAX_RETRIES", int(ai_raw.get("max_retries", 2))),
            timeout_seconds=_env_int(
                "OPENAI_TIMEOUT_SECONDS", int(ai_raw.get("timeout_seconds", 30))
            ),
            max_parallel_requests=_env_int(
                "AI_MAX_PARALLEL_REQUESTS", int(ai_raw.get("max_parallel_requests", 4))
            ),
            max_calls_per_cycle=_env_int(
                "AI_DEEP_ANALYSIS_LIMIT", int(ai_raw.get("max_calls_per_cycle", 0))
            ),
            max_title_chars=int(ai_raw.get("max_title_chars", 300)),
            max_description_chars=int(ai_raw.get("max_description_chars", 4000)),
            cache_ttl_hours=_env_int("AI_CACHE_TTL_HOURS", int(ai_raw.get("cache_ttl_hours", 720))),
            skip_listings_without_price=bool(ai_raw.get("skip_listings_without_price", False)),
            daily_budget_usd=_env_float(
                "OPENAI_DAILY_BUDGET_USD", float(ai_raw.get("daily_budget_usd", 5.0))
            ),
            warn_at_percent=_env_float(
                "OPENAI_WARN_AT_PERCENT", float(ai_raw.get("warn_at_percent", 80.0))
            ),
            stop_at_budget=_env_bool(
                "OPENAI_STOP_AT_BUDGET", bool(ai_raw.get("stop_at_budget", True))
            ),
            primary_input_usd_per_1m=_env_float(
                "OPENAI_PRIMARY_INPUT_USD_PER_1M",
                float(ai_raw.get("primary_input_usd_per_1m", 0.20)),
            ),
            primary_cached_input_usd_per_1m=_env_float(
                "OPENAI_PRIMARY_CACHED_INPUT_USD_PER_1M",
                float(ai_raw.get("primary_cached_input_usd_per_1m", 0.02)),
            ),
            primary_output_usd_per_1m=_env_float(
                "OPENAI_PRIMARY_OUTPUT_USD_PER_1M",
                float(ai_raw.get("primary_output_usd_per_1m", 1.20)),
            ),
            fallback_input_usd_per_1m=_env_float(
                "OPENAI_FALLBACK_INPUT_USD_PER_1M",
                float(ai_raw.get("fallback_input_usd_per_1m", 2.00)),
            ),
            fallback_cached_input_usd_per_1m=_env_float(
                "OPENAI_FALLBACK_CACHED_INPUT_USD_PER_1M",
                float(ai_raw.get("fallback_cached_input_usd_per_1m", 0.20)),
            ),
            fallback_output_usd_per_1m=_env_float(
                "OPENAI_FALLBACK_OUTPUT_USD_PER_1M",
                float(ai_raw.get("fallback_output_usd_per_1m", 12.00)),
            ),
            prompt_name=str(ai_raw.get("prompt_name", "listing-analysis")),
            prompt_version=str(ai_raw.get("prompt_version", "v1.3.0")),
            prompts_path=str(ai_raw.get("prompts_path", "")),
            vision_enabled=_env_bool("AI_VISION_ENABLED", bool(ai_raw.get("vision_enabled", True))),
            vision_model=_env_str("OPENAI_VISION_MODEL", str(ai_raw.get("vision_model", ""))),
            vision_detail=_env_str("OPENAI_VISION_DETAIL", str(ai_raw.get("vision_detail", "low"))),
            vision_max_images=_env_int(
                "OPENAI_VISION_MAX_IMAGES", int(ai_raw.get("vision_max_images", 5))
            ),
            min_reject_confidence=_env_float(
                "AI_MIN_REJECT_CONFIDENCE", float(ai_raw.get("min_reject_confidence", 0.75))
            ),
            price_estimate_enabled=_env_bool(
                "AI_PRICE_ESTIMATE_ENABLED", bool(ai_raw.get("price_estimate_enabled", False))
            ),
            price_model=_env_str("OPENAI_PRICE_MODEL", str(ai_raw.get("price_model", "gpt-5.6-terra"))),
            price_input_usd_per_1m=_env_float(
                "OPENAI_PRICE_INPUT_USD_PER_1M", float(ai_raw.get("price_input_usd_per_1m", 2.00))
            ),
            price_cached_input_usd_per_1m=_env_float(
                "OPENAI_PRICE_CACHED_INPUT_USD_PER_1M",
                float(ai_raw.get("price_cached_input_usd_per_1m", 0.20)),
            ),
            price_output_usd_per_1m=_env_float(
                "OPENAI_PRICE_OUTPUT_USD_PER_1M", float(ai_raw.get("price_output_usd_per_1m", 12.00))
            ),
            price_prompt_name=str(ai_raw.get("price_prompt_name", "price-estimate")),
            price_prompt_version=str(ai_raw.get("price_prompt_version", "v1.0.0")),
            price_cache_ttl_hours=_env_int(
                "AI_PRICE_CACHE_TTL_HOURS", int(ai_raw.get("price_cache_ttl_hours", 720))
            ),
            price_min_comparables=int(ai_raw.get("price_min_comparables", 3)),
            price_on_low_confidence=bool(ai_raw.get("price_on_low_confidence", True)),
            price_allow_hot=_env_bool(
                "AI_PRICE_ALLOW_HOT", bool(ai_raw.get("price_allow_hot", False))
            ),
            price_floor_czk=int(ai_raw.get("price_floor_czk", 500)),
            price_ceiling_czk=int(ai_raw.get("price_ceiling_czk", 500_000)),
            price_max_ratio_to_asking=float(ai_raw.get("price_max_ratio_to_asking", 8.0)),
            price_min_ratio_to_asking=float(ai_raw.get("price_min_ratio_to_asking", 0.15)),
            price_max_range_ratio=float(ai_raw.get("price_max_range_ratio", 2.5)),
        ),
        retail=RetailConfig(
            enabled=bool(retail_raw.get("enabled", True)),
            sources=[str(item) for item in retail_raw.get("sources", ["zbozi"])],
            max_enrichments_per_run=int(retail_raw.get("max_enrichments_per_run", 3)),
            min_comparables=int(retail_raw.get("min_comparables", 3)),
            min_offer_match_score=float(retail_raw.get("min_offer_match_score", 0.78)),
            two_source_max_spread_ratio=float(retail_raw.get("two_source_max_spread_ratio", 0.35)),
            three_source_max_spread_ratio=float(retail_raw.get("three_source_max_spread_ratio", 0.25)),
            low_confidence_max_spread_ratio=float(retail_raw.get("low_confidence_max_spread_ratio", 0.50)),
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
            lookup_delay_seconds=float(retail_raw.get("lookup_delay_seconds", 0.25)),
            max_consecutive_source_errors=int(retail_raw.get("max_consecutive_source_errors", 3)),
        ),
        market_pricing=MarketPricingConfig(
            enabled=bool(market_raw.get("enabled", True)),
            sources=[str(item) for item in market_raw.get("sources", ["bazos_cz", "kleinanzeigen_de", "marktplaats_nl", "buycycle_eu"])],
            quick_sale_discount=(
                _env_float(
                    "QUICK_SALE_DISCOUNT_PERCENT",
                    float(market_raw.get("quick_sale_discount", 0.15)) * 100,
                )
                / 100
            ),
            max_foreign_markets_per_model=int(market_raw.get("max_foreign_markets_per_model", 2)),
            minimum_unique_comparables_before_fallback=int(market_raw.get("minimum_unique_comparables_before_fallback", 3)),
            foreign_market_disagreement_threshold=float(market_raw.get("foreign_market_disagreement_threshold", 0.30)),
            max_comparable_age_days=int(market_raw.get("max_comparable_age_days", 180)),
            max_results_per_source=int(market_raw.get("max_results_per_source", 25)),
            max_queries_per_source=int(market_raw.get("max_queries_per_source", 1)),
            source_timeout_seconds=int(market_raw.get("source_timeout_seconds", 20)),
            total_timeout_seconds=int(market_raw.get("total_timeout_seconds", 70)),
            circuit_breaker_errors=int(market_raw.get("circuit_breaker_errors", 3)),
            circuit_breaker_cooldown_minutes=int(market_raw.get("circuit_breaker_cooldown_minutes", 30)),
            expensive_rounding_threshold_czk=int(market_raw.get("expensive_rounding_threshold_czk", 50000)),
            cheap_rounding_step_czk=int(market_raw.get("cheap_rounding_step_czk", 100)),
            expensive_rounding_step_czk=int(market_raw.get("expensive_rounding_step_czk", 500)),
            exchange_rate_url=str(market_raw.get("exchange_rate_url", MarketPricingConfig().exchange_rate_url)),
            country_market_adjustment={
                **MarketPricingConfig().country_market_adjustment,
                **{str(key).upper(): float(value) for key, value in market_raw.get("country_market_adjustment", {}).items()},
            },
            brand_market_mapping={
                **_default_brand_market_mapping(),
                **{str(key): [str(item).upper() for item in value] for key, value in market_raw.get("brand_market_mapping", {}).items()},
            },
            category_market_mapping={
                **MarketPricingConfig().category_market_mapping,
                **{str(key): [str(item).upper() for item in value] for key, value in market_raw.get("category_market_mapping", {}).items()},
            },
            weights={
                **_default_market_weights(),
                **{str(key): float(value) for key, value in market_raw.get("weights", {}).items()},
            },
            cache_ttl_hours={
                **MarketPricingConfig().cache_ttl_hours,
                **{str(key): int(value) for key, value in market_raw.get("cache_ttl_hours", {}).items()},
            },
            depreciation_by_age={
                **MarketPricingConfig().depreciation_by_age,
                **{str(key): float(value) for key, value in market_raw.get("depreciation_by_age", {}).items()},
            },
            auto_calibration_enabled=bool(market_raw.get("auto_calibration", {}).get("enabled", True)),
            auto_calibration_minimum_model_pairs=int(market_raw.get("auto_calibration", {}).get("minimum_model_pairs", 10)),
        ),
        priority=PriorityConfig(
            urgent_min_score=int(priority_raw.get("urgent_min_score", 80)),
            interesting_min_score=int(priority_raw.get("interesting_min_score", 60)),
            manual_review_min_score=int(priority_raw.get("manual_review_min_score", 35)),
            low_priority_min_score=int(priority_raw.get("low_priority_min_score", 1)),
            max_telegram_cards=int(priority_raw.get("max_telegram_cards", raw.get("max_notifications_per_run", 10))),
            manual_review_reserved_slots=int(priority_raw.get("manual_review_reserved_slots", 2)),
            individual_notification_max_age_hours=int(priority_raw.get("individual_notification_max_age_hours", 6)),
            used_comparable_max_age_days=int(priority_raw.get("used_comparable_max_age_days", 30)),
            very_fresh_max_hours=int(priority_raw.get("very_fresh_max_hours", 6)),
            description_min_chars=int(priority_raw.get("description_min_chars", 80)),
            suspicious_discount_percent=int(priority_raw.get("suspicious_discount_percent", 80)),
            lookup_all_max_count=int(priority_raw.get("lookup_all_max_count", 10)),
            lookup_mid_max_count=int(priority_raw.get("lookup_mid_max_count", 30)),
            lookup_high_max_count=int(priority_raw.get("lookup_high_max_count", 70)),
            lookup_mid_limit=int(priority_raw.get("lookup_mid_limit", 10)),
            lookup_high_limit=int(priority_raw.get("lookup_high_limit", 15)),
            lookup_max_limit=int(priority_raw.get("lookup_max_limit", 20)),
            lookup_large_fraction=float(priority_raw.get("lookup_large_fraction", 0.20)),
            duplicate_confirmed_similarity=float(priority_raw.get("duplicate_confirmed_similarity", 0.95)),
            duplicate_possible_similarity=float(priority_raw.get("duplicate_possible_similarity", 0.85)),
            duplicate_canonical_seen_tie_seconds=int(priority_raw.get("duplicate_canonical_seen_tie_seconds", 300)),
            weights={
                **_default_priority_weights(),
                **{str(key): int(value) for key, value in priority_raw.get("weights", {}).items()},
            },
        ),
        deal_scoring=DealScoringConfig(
            enabled=_env_bool("DEAL_SCORING_ENABLED", bool(deal_raw.get("enabled", True))),
            algorithm_version=str(deal_raw.get("algorithm_version", "2.2.0")),
            risk_reserve_percent=_env_float(
                "RISK_RESERVE_PERCENT",
                float(deal_raw.get("risk_reserve_percent", 10)),
            ),
            hot_min_profit_czk=_env_float(
                "HOT_MIN_PROFIT_CZK",
                float(deal_raw.get("hot_min_profit_czk", 4000)),
            ),
            hot_min_roi_percent=_env_float(
                "HOT_MIN_ROI_PERCENT",
                float(deal_raw.get("hot_min_roi_percent", 20)),
            ),
            interesting_min_roi_percent=_env_float(
                "INTERESTING_MIN_ROI_PERCENT",
                float(deal_raw.get("interesting_min_roi_percent", 15)),
            ),
            high_roi_percent=_env_float(
                "HIGH_ROI_PERCENT",
                float(deal_raw.get("high_roi_percent", 100)),
            ),
            hot_min_liquidity_score=int(
                _env_float(
                    "HOT_MIN_LIQUIDITY_SCORE",
                    float(deal_raw.get("hot_min_liquidity_score", 50)),
                )
            ),
            hot_min_confidence_score=int(
                _env_float(
                    "HOT_MIN_CONFIDENCE_SCORE",
                    float(deal_raw.get("hot_min_confidence_score", 50)),
                )
            ),
            manual_review_on_low_confidence=_env_bool(
                "MANUAL_REVIEW_MAX_LOW_CONFIDENCE",
                bool(deal_raw.get("manual_review_on_low_confidence", True)),
            ),
            telegram_send_hot=_env_bool(
                "TELEGRAM_SEND_HOT",
                bool(deal_raw.get("telegram_send_hot", True)),
            ),
            telegram_send_interesting=_env_bool(
                "TELEGRAM_SEND_INTERESTING",
                bool(deal_raw.get("telegram_send_interesting", True)),
            ),
            telegram_send_manual_review=_env_bool(
                "TELEGRAM_SEND_MANUAL_REVIEW",
                bool(deal_raw.get("telegram_send_manual_review", True)),
            ),
            telegram_send_low_priority=_env_bool(
                "TELEGRAM_SEND_LOW_PRIORITY",
                bool(deal_raw.get("telegram_send_low_priority", False)),
            ),
            telegram_send_reject=_env_bool(
                "TELEGRAM_SEND_REJECT",
                bool(deal_raw.get("telegram_send_reject", False)),
            ),
            liquidity_high_min_score=int(deal_raw.get("liquidity_high_min_score", 70)),
            liquidity_medium_min_score=int(deal_raw.get("liquidity_medium_min_score", 50)),
            liquidity_exact_points=int(deal_raw.get("liquidity_exact_points", 12)),
            liquidity_close_points=int(deal_raw.get("liquidity_close_points", 8)),
            liquidity_family_points=int(deal_raw.get("liquidity_family_points", 4)),
            liquidity_component_points=int(deal_raw.get("liquidity_component_points", 2)),
            liquidity_source_points=int(deal_raw.get("liquidity_source_points", 5)),
            liquidity_source_count_cap=int(deal_raw.get("liquidity_source_count_cap", 4)),
            liquidity_czech_bonus=int(deal_raw.get("liquidity_czech_bonus", 10)),
            estimated_days_high=int(deal_raw.get("estimated_days_high", 21)),
            estimated_days_medium=int(deal_raw.get("estimated_days_medium", 45)),
            estimated_days_low=int(deal_raw.get("estimated_days_low", 75)),
            confidence_scores={
                **DealScoringConfig().confidence_scores,
                **{str(key): int(value) for key, value in deal_raw.get("confidence_scores", {}).items()},
            },
            minimum_identity_confidence=float(deal_raw.get("minimum_identity_confidence", 0.55)),
            require_confirmed_model=bool(deal_raw.get("require_confirmed_model", True)),
            allow_hot_on_ai_price=_env_bool(
                "AI_PRICE_ALLOW_HOT", bool(deal_raw.get("allow_hot_on_ai_price", False))
            ),
            price_outlier_discount_percent=float(
                deal_raw.get("price_outlier_discount_percent", 80)
            ),
            profit_score_target_czk=float(deal_raw.get("profit_score_target_czk", 8000)),
            roi_score_target_percent=float(deal_raw.get("roi_score_target_percent", 100)),
            risk_penalty_per_flag=float(deal_raw.get("risk_penalty_per_flag", 20)),
            score_weights={
                **_default_deal_score_weights(),
                **{str(key): float(value) for key, value in deal_raw.get("score_weights", {}).items()},
            },
            condition_scores={
                **DealScoringConfig().condition_scores,
                **{str(key): float(value) for key, value in deal_raw.get("condition_scores", {}).items()},
            },
            positive_condition_terms=[
                str(value)
                for value in deal_raw.get(
                    "positive_condition_terms",
                    DealScoringConfig().positive_condition_terms,
                )
            ],
            completed_service_terms=[
                str(value)
                for value in deal_raw.get(
                    "completed_service_terms",
                    DealScoringConfig().completed_service_terms,
                )
            ],
            service_required_terms=[
                str(value)
                for value in deal_raw.get(
                    "service_required_terms",
                    DealScoringConfig().service_required_terms,
                )
            ],
            problem_condition_terms=[
                str(value)
                for value in deal_raw.get(
                    "problem_condition_terms",
                    DealScoringConfig().problem_condition_terms,
                )
            ],
            critical_valuation_statuses=[
                str(value)
                for value in deal_raw.get(
                    "critical_valuation_statuses",
                    DealScoringConfig().critical_valuation_statuses,
                )
            ],
            critical_warning_terms=[
                str(value)
                for value in deal_raw.get(
                    "critical_warning_terms",
                    DealScoringConfig().critical_warning_terms,
                )
            ],
            manual_review_risk_terms=[
                str(value)
                for value in deal_raw.get(
                    "manual_review_risk_terms",
                    DealScoringConfig().manual_review_risk_terms,
                )
            ],
        ),
        ai_gate=AIGateConfig(
            enabled=_env_bool("AI_OPPORTUNITY_GATE_ENABLED", bool(ai_gate_raw.get("enabled", True))),
            base_score=int(ai_gate_raw.get("base_score", 25)),
            analysis_min_score=_env_int(
                "AI_GATE_ANALYSIS_MIN_SCORE", int(ai_gate_raw.get("analysis_min_score", 30))
            ),
            hidden_opportunity_bonus=_env_int(
                "AI_GATE_HIDDEN_OPPORTUNITY_BONUS",
                int(ai_gate_raw.get("hidden_opportunity_bonus", 30)),
            ),
            seller_urgency_bonus={
                **AIGateConfig().seller_urgency_bonus,
                **{str(key).upper(): int(value) for key, value in ai_gate_raw.get("seller_urgency_bonus", {}).items()},
            },
            listing_quality_bonus={
                **AIGateConfig().listing_quality_bonus,
                **{str(key).upper(): int(value) for key, value in ai_gate_raw.get("listing_quality_bonus", {}).items()},
            },
            identity_high_threshold=float(ai_gate_raw.get("identity_high_threshold", 0.80)),
            identity_medium_threshold=float(ai_gate_raw.get("identity_medium_threshold", 0.55)),
            identity_high_bonus=int(ai_gate_raw.get("identity_high_bonus", 10)),
            identity_medium_bonus=int(ai_gate_raw.get("identity_medium_bonus", 5)),
            unknown_identity_penalty=int(ai_gate_raw.get("unknown_identity_penalty", 5)),
            valuable_information_bonus=int(ai_gate_raw.get("valuable_information_bonus", 5)),
            valuable_information_min_facts=int(ai_gate_raw.get("valuable_information_min_facts", 3)),
            risk_penalty={
                **AIGateConfig().risk_penalty,
                **{str(key): int(value) for key, value in ai_gate_raw.get("risk_penalty", {}).items()},
            },
            risk_penalty_max=int(ai_gate_raw.get("risk_penalty_max", 40)),
            risk_severity={
                **_default_risk_severity(),
                **{str(key).upper(): str(value) for key, value in ai_gate_raw.get("risk_severity", {}).items()},
            },
            block_non_bicycle=bool(ai_gate_raw.get("block_non_bicycle", True)),
        ),
        telegram_gate=TelegramGateConfig(
            enabled=_env_bool(
                "TELEGRAM_NOTIFICATION_GATE_ENABLED", bool(telegram_gate_raw.get("enabled", True))
            ),
            hot_min_score=int(telegram_gate_raw.get("hot_min_score", 0)),
            interesting_min_score=_env_int(
                "TELEGRAM_INTERESTING_MIN_SCORE",
                int(telegram_gate_raw.get("interesting_min_score", 40)),
            ),
            manual_review_min_score=_env_int(
                "TELEGRAM_MANUAL_REVIEW_MIN_SCORE",
                int(telegram_gate_raw.get("manual_review_min_score", 60)),
            ),
            manual_review_require_strong_signal=_env_bool(
                "TELEGRAM_MANUAL_REVIEW_REQUIRE_STRONG_SIGNAL",
                bool(telegram_gate_raw.get("manual_review_require_strong_signal", True)),
            ),
            strong_signals=[
                str(value)
                for value in telegram_gate_raw.get(
                    "strong_signals", TelegramGateConfig().strong_signals
                )
            ],
            block_on_blocking_risk=bool(telegram_gate_raw.get("block_on_blocking_risk", True)),
            status_base_score={
                **TelegramGateConfig().status_base_score,
                **{str(key).upper(): int(value) for key, value in telegram_gate_raw.get("status_base_score", {}).items()},
            },
            profit_signal_max=int(telegram_gate_raw.get("profit_signal_max", 20)),
            profit_signal_target_czk=float(telegram_gate_raw.get("profit_signal_target_czk", 8000)),
            roi_signal_max=int(telegram_gate_raw.get("roi_signal_max", 15)),
            roi_signal_target_percent=float(telegram_gate_raw.get("roi_signal_target_percent", 60)),
            liquidity_signal_max=int(telegram_gate_raw.get("liquidity_signal_max", 10)),
            confidence_signal_max=int(telegram_gate_raw.get("confidence_signal_max", 10)),
            hidden_opportunity_bonus=int(telegram_gate_raw.get("hidden_opportunity_bonus", 20)),
            urgency_high_bonus=int(telegram_gate_raw.get("urgency_high_bonus", 15)),
            urgency_medium_bonus=int(telegram_gate_raw.get("urgency_medium_bonus", 6)),
            price_anomaly_bonus=int(telegram_gate_raw.get("price_anomaly_bonus", 15)),
            price_anomaly_min_discount_percent=float(
                telegram_gate_raw.get("price_anomaly_min_discount_percent", 35)
            ),
            risk_penalty_max=int(telegram_gate_raw.get("risk_penalty_max", 40)),
        ),
    )
    config.validate()
    return config
