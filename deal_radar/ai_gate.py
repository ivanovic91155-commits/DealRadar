"""AI Opportunity Gate и Telegram Notification Gate.

Level 1 давно возвращает ``hidden_opportunity``, ``seller_urgency``,
``listing_quality``, ``identity_confidence`` и риск-флаги, но решение «показать
объявление человеку» их не читало: в Telegram уходил любой ``MANUAL_REVIEW``,
которому хватило свободного слота. Этот модуль превращает уже сохранённый разбор
в два независимых числа и ничего не спрашивает у модели заново.

Числа отвечают на разные вопросы, поэтому и считаются по-разному:

``analysis_priority_score``
    Стоит ли тратить на объявление скрейпинг и рыночную оценку. Живёт до
    Market Price Engine, читает только AI-сигналы.

``notification_priority_score``
    Стоит ли показывать объявление человеку. Живёт после оценки сделки; деньги,
    ликвидность и уверенность берутся готовыми из :class:`DealEvaluation`, а
    новыми здесь являются только AI-сигналы, которых у ``deal_score`` нет.

Главный принцип обоих: ``listing_quality=LOW`` само по себе почти ничего не
стоит. Плохо оформленное объявление — это не плохой велосипед, а чаще всего
как раз то, мимо чего прошли остальные покупатели. Наказывается не небрежность
продавца, а отсутствие информации вместе с отсутствием причины.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from deal_radar.config import RISK_SEVERITIES, AIGateConfig, TelegramGateConfig
from deal_radar.models import AIAnalysis, ListingAnalysis


# Действия AI Gate. Ни одно из них не удаляет объявление: ворота управляют
# очередью анализа, а не составом базы.
ACTION_DEEP_ANALYSIS = "DEEP_ANALYSIS"
ACTION_STANDARD = "STANDARD"
ACTION_BLOCKED = "BLOCKED"

# Действия Telegram Gate.
SEND = "SEND"
SKIP = "SKIP"
BLOCK = "BLOCK"

SIGNAL_HIDDEN_OPPORTUNITY = "hidden_opportunity"
SIGNAL_URGENCY_HIGH = "seller_urgency_high"
SIGNAL_PRICE_ANOMALY = "price_anomaly"

# Типы объявлений, которые AI считает «не велосипедом». COMPLETE_BICYCLE и OTHER
# сюда не входят: OTHER слишком размыт, чтобы глушить по нему, а спорные случаи
# должны дойти до человека.
NON_BIKE_LISTING_TYPES = frozenset(
    {"PARTS", "ACCESSORY", "FRAME_ONLY", "SERVICE_OR_RENTAL", "WANTED"}
)


def _severity_rank(severity: str) -> int:
    return RISK_SEVERITIES.index(severity) if severity in RISK_SEVERITIES else 0


@dataclass(slots=True)
class AISignals:
    """Плоский срез разбора Level 1 — ровно те поля, которые читают ворота."""

    available: bool = False
    hidden_opportunity: bool = False
    seller_urgency: str = "UNKNOWN"
    listing_quality: str = "UNKNOWN"
    identity_confidence: float = 0.0
    facts: int = 0
    findings: list[tuple[str, str]] = field(default_factory=list)
    risk_penalty: int = 0

    @property
    def max_severity(self) -> str:
        if not self.findings:
            return ""
        return max((severity for _flag, severity in self.findings), key=_severity_rank)

    @property
    def blocking_flags(self) -> list[str]:
        return [flag for flag, severity in self.findings if severity == "blocking"]


def _risk_findings(ai: AIAnalysis, config: AIGateConfig) -> list[tuple[str, str]]:
    """Риски объявления как пары «флаг → тяжесть».

    Новых флагов для модели здесь не появляется: берутся значения enum из
    ``schema.json``, булевы поля блока ``risk`` и производные признаки из блоков
    ``condition`` и ``classification``.
    """

    findings: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(flag: str) -> None:
        if flag in seen:
            return
        seen.add(flag)
        findings.append((flag, config.risk_severity.get(flag, "minor")))

    classification = ai.classification
    if (
        config.block_non_bicycle
        and classification is not None
        and not classification.is_bicycle
        and classification.listing_type in NON_BIKE_LISTING_TYPES
    ):
        # Второй замок к подавлению не-велосипедов в сервисе. Тот срабатывает
        # только при уверенности выше порога и убирает объявление совсем; этот
        # работает при любой уверенности и всего лишь не пускает карточку в
        # Telegram. Шлем, который модель опознала на 0.6, — ровно этот случай.
        add("NOT_A_BICYCLE")

    risk = ai.risk
    if risk is not None:
        for flag in risk.risk_flags:
            add(flag)
        if risk.possible_scam:
            add("SCAM_RISK")
        if risk.possible_stolen_bike:
            add("STOLEN_RISK")
        if risk.suspicious_price:
            add("SUSPICIOUS_PRICE")

    condition = ai.condition
    if condition is not None:
        if condition.claimed_condition == "DAMAGED":
            add("DAMAGED_CONDITION")
        if condition.missing_parts:
            # У электровелосипеда отсутствующая деталь — это чаще всего батарея
            # или зарядка, то есть половина стоимости, а не мелочь.
            electric = ai.identity is not None and bool(ai.identity.is_electric)
            add("EBIKE_MISSING_PARTS" if electric else "MISSING_PARTS")
    return findings


def _count_facts(ai: AIAnalysis) -> int:
    """Сколько полезных фактов вытащил AI из текста и фото."""

    identity = ai.identity
    specifications = ai.specifications
    condition = ai.condition
    values: list[object] = []
    if identity is not None:
        values.extend([identity.brand, identity.model, identity.model_year, identity.bike_type])
    if specifications is not None:
        values.extend(
            [
                specifications.frame_size_normalized or specifications.frame_size_raw,
                specifications.wheel_size_inches,
                specifications.groupset,
                specifications.frame_material,
            ]
        )
    if condition is not None:
        values.append(condition.claimed_condition not in {"", "UNKNOWN"})
    return sum(1 for value in values if value)


def read_signals(
    analysis: ListingAnalysis,
    config: AIGateConfig,
    *,
    live: bool,
) -> AISignals:
    """Достать AI-сигналы из анализа объявления.

    ``live=False`` (AI выключен или в тени) означает пустой набор: ворота
    продолжают работать на детерминированных данных сделки. Выключенный
    ``ai_opportunity_gate`` при этом гасит только приоритет анализа: сигналы
    остаются доступны воротам Telegram, потому что это отдельный слой со своим
    выключателем.
    """

    ai = analysis.ai_analysis
    if not live or ai is None or ai.status != "AI_OK":
        return AISignals()
    opportunity = ai.opportunity
    identity = ai.identity
    findings = _risk_findings(ai, config)
    penalty = sum(config.risk_penalty.get(severity, 0) for _flag, severity in findings)
    return AISignals(
        available=True,
        hidden_opportunity=bool(opportunity.hidden_opportunity) if opportunity else False,
        seller_urgency=(opportunity.seller_urgency if opportunity else "UNKNOWN") or "UNKNOWN",
        listing_quality=(opportunity.listing_quality if opportunity else "UNKNOWN") or "UNKNOWN",
        identity_confidence=identity.identity_confidence if identity else 0.0,
        facts=_count_facts(ai),
        findings=findings,
        risk_penalty=min(penalty, config.risk_penalty_max),
    )


@dataclass(slots=True)
class GateDecision:
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    action: str = ACTION_STANDARD


def _reason(reasons: list[str], name: str, points: int) -> None:
    if points:
        reasons.append(f"{name}:{points:+d}")


def analysis_priority(signals: AISignals, config: AIGateConfig) -> GateDecision:
    """Насколько важно потратить ресурсы на дальнейшую проверку объявления."""

    if not config.enabled:
        return GateDecision(score=0, reasons=[], action=ACTION_STANDARD)

    reasons: list[str] = []
    score = config.base_score
    _reason(reasons, "base", config.base_score)

    if not signals.available:
        # Разбора нет (AI выключен, в тени, не дошёл до объявления или упал).
        # Объявление остаётся на нейтральной середине, а не уезжает в конец
        # очереди: отсутствие анализа — не улика против объявления.
        reasons.append("ai_unavailable:+0")
        return GateDecision(
            score=max(0, min(100, score)),
            reasons=reasons,
            action=ACTION_DEEP_ANALYSIS if score >= config.analysis_min_score else ACTION_STANDARD,
        )

    if signals.hidden_opportunity:
        score += config.hidden_opportunity_bonus
        _reason(reasons, "hidden_opportunity", config.hidden_opportunity_bonus)

    urgency_bonus = config.seller_urgency_bonus.get(signals.seller_urgency, 0)
    score += urgency_bonus
    _reason(reasons, f"seller_urgency_{signals.seller_urgency.casefold()}", urgency_bonus)

    quality_bonus = config.listing_quality_bonus.get(signals.listing_quality, 0)
    score += quality_bonus
    _reason(reasons, f"listing_quality_{signals.listing_quality.casefold()}", quality_bonus)

    if signals.identity_confidence >= config.identity_high_threshold:
        score += config.identity_high_bonus
        _reason(reasons, "identity_high", config.identity_high_bonus)
    elif signals.identity_confidence >= config.identity_medium_threshold:
        score += config.identity_medium_bonus
        _reason(reasons, "identity_medium", config.identity_medium_bonus)
    else:
        # Неизвестная модель — это цена проверки, а не приговор: штраф
        # намеренно меньше любого бонуса за возможность.
        score -= config.unknown_identity_penalty
        _reason(reasons, "identity_unknown", -config.unknown_identity_penalty)

    if signals.facts >= config.valuable_information_min_facts:
        score += config.valuable_information_bonus
        _reason(reasons, "valuable_information", config.valuable_information_bonus)

    if signals.risk_penalty:
        score -= signals.risk_penalty
        _reason(reasons, f"risk_{signals.max_severity}", -signals.risk_penalty)

    score = max(0, min(100, score))
    if signals.blocking_flags:
        action = ACTION_BLOCKED
    elif score >= config.analysis_min_score:
        action = ACTION_DEEP_ANALYSIS
    else:
        action = ACTION_STANDARD
    return GateDecision(score=score, reasons=reasons, action=action)


@dataclass(slots=True)
class NotificationDecision:
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    strong_signals: list[str] = field(default_factory=list)
    action: str = SEND
    reason: str = ""


def _price_anomaly(analysis: ListingAnalysis, config: TelegramGateConfig) -> bool:
    """Аномальное ценовое преимущество по уже посчитанным числам сделки."""

    deal = analysis.deal_evaluation
    if deal is None:
        return False
    if "purchase_price_outlier" in deal.flags:
        return True
    median = deal.market_median_czk
    purchase = deal.purchase_price_czk
    if not median or not purchase or median <= 0:
        return False
    return (median - purchase) / median * 100 >= config.price_anomaly_min_discount_percent


def notification_priority(
    analysis: ListingAnalysis,
    signals: AISignals,
    config: TelegramGateConfig,
) -> NotificationDecision:
    """Насколько это объявление важно показать пользователю."""

    deal = analysis.deal_evaluation
    if deal is None:
        return NotificationDecision(score=0, reasons=["deal_evaluation_missing:+0"])

    reasons: list[str] = []
    strong: list[str] = []
    base = config.status_base_score.get(deal.status, 0)
    score = float(base)
    _reason(reasons, f"status_{deal.status.casefold()}", base)

    # Деньги, ликвидность и уверенность не пересчитываются: берём готовые числа
    # этапа 2.2 и переводим их в баллы внимания.
    profit = deal.net_profit_czk
    if profit is not None and profit > 0:
        points = min(
            config.profit_signal_max,
            profit / config.profit_signal_target_czk * config.profit_signal_max,
        )
        score += points
        _reason(reasons, "expected_profit", round(points))
    roi = deal.roi_percent
    if roi is not None and roi > 0:
        points = min(
            config.roi_signal_max,
            roi / config.roi_signal_target_percent * config.roi_signal_max,
        )
        score += points
        _reason(reasons, "roi", round(points))
    if deal.liquidity_score:
        points = deal.liquidity_score / 100 * config.liquidity_signal_max
        score += points
        _reason(reasons, "liquidity", round(points))
    if deal.confidence_score:
        points = deal.confidence_score / 100 * config.confidence_signal_max
        score += points
        _reason(reasons, "confidence", round(points))

    if signals.hidden_opportunity:
        score += config.hidden_opportunity_bonus
        _reason(reasons, "hidden_opportunity", config.hidden_opportunity_bonus)
        strong.append(SIGNAL_HIDDEN_OPPORTUNITY)
    if signals.seller_urgency == "HIGH":
        score += config.urgency_high_bonus
        _reason(reasons, "seller_urgency_high", config.urgency_high_bonus)
        strong.append(SIGNAL_URGENCY_HIGH)
    elif signals.seller_urgency == "MEDIUM":
        score += config.urgency_medium_bonus
        _reason(reasons, "seller_urgency_medium", config.urgency_medium_bonus)
    if _price_anomaly(analysis, config):
        score += config.price_anomaly_bonus
        _reason(reasons, "price_anomaly", config.price_anomaly_bonus)
        strong.append(SIGNAL_PRICE_ANOMALY)

    penalty = min(signals.risk_penalty, config.risk_penalty_max)
    if penalty:
        score -= penalty
        _reason(reasons, f"risk_{signals.max_severity}", -penalty)

    return NotificationDecision(
        score=int(max(0, min(100, round(score)))),
        reasons=reasons,
        strong_signals=[name for name in strong if name in config.strong_signals],
    )


def telegram_decision(
    analysis: ListingAnalysis,
    signals: AISignals,
    config: TelegramGateConfig,
) -> NotificationDecision:
    """Отправлять ли карточку и почему именно так."""

    decision = notification_priority(analysis, signals, config)
    deal = analysis.deal_evaluation
    if not config.enabled or deal is None:
        decision.action = SEND
        decision.reason = "gate_disabled" if not config.enabled else "deal_evaluation_missing"
        return decision

    if config.block_on_blocking_risk and signals.blocking_flags:
        decision.action = BLOCK
        decision.reason = f"blocking_risk_{signals.blocking_flags[0].casefold()}"
        return decision

    status = deal.status
    if status == "HOT":
        # Качественный HOT остаётся гарантированным: единственное, что его
        # останавливает, — блокирующий риск выше.
        if decision.score >= config.hot_min_score:
            decision.action, decision.reason = SEND, "status_hot"
        else:
            decision.action, decision.reason = SKIP, "score_below_hot_threshold"
    elif status == "INTERESTING":
        if decision.score >= config.interesting_min_score:
            decision.action, decision.reason = SEND, "notification_score"
        else:
            decision.action, decision.reason = SKIP, "score_below_interesting_threshold"
    elif status == "MANUAL_REVIEW":
        if config.manual_review_require_strong_signal and decision.strong_signals:
            decision.action = SEND
            decision.reason = decision.strong_signals[0]
        elif decision.score >= config.manual_review_min_score:
            decision.action, decision.reason = SEND, "notification_score"
        else:
            decision.action = SKIP
            decision.reason = (
                "no_strong_signal"
                if config.manual_review_require_strong_signal
                else "score_below_manual_review_threshold"
            )
    else:
        # LOW_PRIORITY и REJECT ворота не трогают: их судьбу и так решают флаги
        # telegram_send_* этапа 2.2, и переопределять явную настройку владельца
        # этот слой не должен.
        decision.action, decision.reason = SEND, "status_send_policy"
    return decision
