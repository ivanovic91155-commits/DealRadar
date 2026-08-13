from __future__ import annotations

import re

from deal_radar import bike_catalog
from deal_radar.config import IdentityConfig
from deal_radar.models import BikeIdentity, Listing
from deal_radar.text_utils import normalize_text

__all__ = [
    "BRANDS",
    "build_search_queries",
    "configure_identity",
    "hard_filter_reason",
    "has_accessory_terms",
    "has_unavailable_terms",
    "has_used_terms",
    "identify_bike",
    "identify_listing",
    "model_family",
    "normalize_text",
]


BRANDS = {
    "4ever": "4EVER",
    "author": "Author",
    "banshee": "Banshee",
    "bergamont": "Bergamont",
    "bianchi": "Bianchi",
    "bmc": "BMC",
    "brompton": "Brompton",
    "cannondale": "Cannondale",
    "canyon": "Canyon",
    "cervelo": "Cervélo",
    "commencal": "Commencal",
    "core": "Core",
    "corratec": "Corratec",
    "cube": "Cube",
    "dartmoor": "Dartmoor",
    "decathlon": "Decathlon",
    "devinci": "Devinci",
    "electra": "Electra",
    "eltreco": "Eltreco",
    "felt": "Felt",
    "focus": "Focus",
    "fuji": "Fuji",
    "ghost": "Ghost",
    "giant": "Giant",
    "gt": "GT",
    "haibike": "Haibike",
    "head": "Head",
    "kellys": "Kellys",
    "kona": "Kona",
    "kross": "Kross",
    "ktm": "KTM",
    "lapierre": "Lapierre",
    "liv": "Liv",
    "marin": "Marin",
    "merida": "Merida",
    "mondraker": "Mondraker",
    "moustache": "Moustache",
    "norco": "Norco",
    "orbea": "Orbea",
    "pivot": "Pivot",
    "polygon": "Polygon",
    "propain": "Propain",
    "rock machine": "Rock Machine",
    "rockrider": "Rockrider",
    "rose": "Rose",
    "santa cruz": "Santa Cruz",
    "scott": "Scott",
    "specialized": "Specialized",
    "superior": "Superior",
    "trek": "Trek",
    "whyte": "Whyte",
    "wilier": "Wilier",
    "woom": "Woom",
    "yt": "YT",
}

GENERATION_RE = re.compile(r"\b(?:gen(?:eration)?|generace)\s*[-.]?\s*(\d{1,2})\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(20[0-3]\d)\b")
WHEEL_RE = re.compile(
    r"(?<!\d)(?:(12|14|16|18|20|24|26|27[.,]5|27|28|29)\s*(?:\"|''|palc(?:u|ove|ova)?|inch)"
    r"|(26|27[.,]5|27|28|29))(?!\d)",
    re.IGNORECASE,
)
FRAME_RE = re.compile(
    r"\b(?:vel(?:ikost)?\.?|size|ram(?:u)?|frame|rahmen(?:gro(?:sse|ße))?|gro(?:sse|ße)|gr\.?|maat|framemaat)\s*[:.-]?\s*(xxs|xs|s|m/?l|m|l|xl|xxl|\d{2}(?:[.,]\d)?)\b",
    re.IGNORECASE,
)
# "velikost kol(a) 26" по-чешски = размер КОЛЁС, а не рамы. Ловим такие
# случаи, чтобы не занести колёсный размер в поле рамы.
FRAME_WHEEL_CONFUSION_RE = re.compile(
    r"\b(?:vel(?:ikost)?\.?|size|maat)\s*(?:kol[ao]?|wheels?|wielen)\b",
    re.IGNORECASE,
)
# Обратная путаница: 'vel. 16"' и 'velikost ramu 16"' — это РАМА, а не колёса.
# Без этой проверки первое же число с кавычкой объявляется колёсным размером,
# и женский Merida с рамой 16" получает "колёса 16"" вместо реальных 26".
FRAME_MARKER_BEFORE_RE = re.compile(
    r"(?:vel(?:ikost)?\.?|size|ram(?:u|ec)?|frame(?:maat)?|rahmen(?:gro(?:sse|ße))?"
    r"|gro(?:sse|ße)|gr\.?|maat)\s*(?:kola?|ramu|frame)?\s*[:.-]?\s*$",
    re.IGNORECASE,
)
# Явный колёсный контекст: "na 26 kolech", "26 kola", "kola 26", "26 wheels".
WHEEL_MARKER_AFTER_RE = re.compile(r"^\s*(?:kol[aeo]?(?:ch|y)?|palc|wheels?|wielen)\b", re.IGNORECASE)
# "kol" без гласной на конце — это "velikost kol 26", тоже колёса. Проверяется
# раньше рамочного маркера, иначе "velikost" перетянет одеяло на себя.
WHEEL_MARKER_BEFORE_RE = re.compile(r"\b(?:kol[ao]?|kolech|wheels?|wielen)\s*(?:o\s*)?$", re.IGNORECASE)
# Числовые размеры рамы валидны только как ростовка: дюймы 13-23 или см 38-62.
# Всё, что похоже на колёса (24/26/27/28/29), рамой быть не может.
WHEEL_LIKE_SIZES = {"24", "26", "27", "28", "29"}
PRICE_SUFFIX_RE = re.compile(r"\s*:\s*\d[\d\s.\u00a0]*\s*$")
TRAVEL_RE = re.compile(r"\b(80|90|100|110|120|130|140|150|160|170|180|190|200)\s*mm\b", re.IGNORECASE)

GENERIC_WORDS = {
    "bike", "bicycle", "bicykl", "cyklo", "e", "elektrokolo", "ebike", "e-bike", "horsky", "horske",
    "jizdni", "kolo", "kola", "mtb", "detske", "detsky", "divci", "damske", "panske", "prodám",
    "alu", "hlinikovy", "prodam", "prodej", "model", "novy", "nove", "zanovni", "v", "vel", "velikost",
    "kol", "kolech", "palec", "palcu", "palce", "palcove", "palcova", "rada", "modelova",
    "ram", "ramu", "frame", "size", "zaruce", "rahmen", "rahmengrosse", "grosse", "gr",
    "maat", "framemaat", "framematen", "divers", "kleuren", "en", "herren", "damen",
    "mountain", "road", "gravel", "city", "hardtail",
    "xxs", "xs", "s", "m", "m/l", "l", "xl", "xxl",
}
COLOR_WORDS = {
    "black", "blue", "bronze", "brown", "bila", "bile", "bily", "cerna", "cerne", "cerny", "cervena",
    "cervene", "cerveny", "clear", "crystal", "dark", "fade", "gloss", "green", "grey", "keswick",
    "flamingo", "lithium", "lotus", "magic", "matte", "mint", "modra", "modre", "modry", "orange",
    "pennyflake", "pink", "purple", "red", "silver", "splatter", "white", "yellow", "zelena", "zelene",
    "zeleny",
}
TRIM_WORDS = {
    "alloy", "axs", "carbon", "comp", "elite", "evo", "expert", "gx", "pro", "race", "sl", "slr",
    "sport", "sx", "team", "ultimate",
}
COMPONENT_WORDS = {"deore", "fox", "rockshox", "shimano", "sram", "suntour", "xt", "xtr"}
# Продавцы описывают состояние и условия продажи прямо в заголовке. Такие слова
# не могут быть частью названия модели — иначе "Cube kolo po servisu" даёт
# модель "Po Servisu". Многословные обороты вырезаются до токенизации.
NOISE_PHRASES = (
    "ve skvelem stavu", "v skvelem stavu", "ve vybornem stavu", "v vybornem stavu",
    "v dobrem stavu", "ve velmi dobrem stavu", "po servisu", "po kompletnim servisu",
    "po celkovem servisu", "jako nove", "jako novy", "top stav", "super stav",
    "pekny stav", "dobry stav", "malo jete", "malo pouzivane", "po synovi", "po dceri",
    "za odvoz", "cena dohodou", "k odberu", "sada kol", "nutny servis", "spatny stav",
    "prodam kolo", "prodam jizdni kolo",
)
NOISE_WORDS = {
    "levne", "levny", "rychle", "specha", "spechá", "stav", "stavu", "servis", "servisu",
    "servisovane", "skvely", "skvelem", "vyborny", "vybornem", "perfektni", "perfektnim",
    "zachovaly", "zachovale", "funkcni", "nefunkcni", "koupeno", "sleva", "dohodou",
    "dohoda", "ihned", "odvoz", "odber", "moznost", "zaslani", "posilam", "dovezu",
    "original", "doklad", "faktura", "zaruka", "zaruce", "nutno", "nutny", "vhodne",
    "vhodny", "krasne", "krasny", "temer", "plne", "nevim", "neznam", "znacka", "dnes",
    "stehovani", "nevyuzite", "potrebuji", "spech",
}
ACCESSORY_WORDS = {
    "baterie", "battery", "charger", "fork", "frame only", "helmet", "nabijecka", "plášť", "plast",
    "pneumatika", "ram kola", "sedlo", "vidlice", "bearing", "bearings", "headset", "lozisko",
    "lozisek", "sada kol", "vypletena kola", "wheelset",
    # Велосипедное «kolo» в заголовке протаскивает и вещи, которые велосипедом не
    # являются: детское кресло (sedačka), велотуфли (tretry), спиннинг-тренажёр и
    # прочие домашние станки. Слово в заголовке здесь — сам предмет, а не аксессуар
    # к продаваемому велосипеду, поэтому отсекаем до платного вызова AI.
    "sedacka", "tretry", "spinning", "trenazer", "rotoped",
}
# Продажа рамы отдельно: "prodám rám Trek", "rámec bez vidlice". Одиночное слово
# "ram" в ACCESSORY_WORDS занести нельзя — оно встречается в размере рамы.
# "X na kolo" — почти всегда аксессуар *для* велосипеда (sedačka na kolo,
# tretry na kolo, držák na kolo): сам велосипед в заголовке зовётся "kolo",
# а не "na kolo".
ACCESSORY_PHRASES_RE = re.compile(
    r"\b(?:prodam|prodej|pouze|jen|samotny|nabizim)\s+ram(?:ec|u)?\b"
    r"|\bram(?:ec|u)?\s+(?:bez|z)\b"
    r"|\bna\s+kola?\b"
)
USED_WORDS = {"bazar", "pouzite", "pouzity", "refurbished", "repasovane", "repasovany", "used"}
UNAVAILABLE_WORDS = {"neni skladem", "out of stock", "unavailable", "vyprodano"}


NOISE_PHRASES_RE = re.compile(
    "|".join(rf"(?<![a-z0-9]){re.escape(normalize_text(phrase))}(?![a-z0-9])" for phrase in NOISE_PHRASES)
)
MODEL_STOP_TOKENS = frozenset(GENERIC_WORDS | COLOR_WORDS | COMPONENT_WORDS | TRIM_WORDS | NOISE_WORDS)

_IDENTITY_CONFIG = IdentityConfig()


def configure_identity(config: IdentityConfig) -> None:
    """Применить настройки идентификации из config.json (вызывается сервисом)."""
    global _IDENTITY_CONFIG
    _IDENTITY_CONFIG = config
    bike_catalog.clear_cache()


def _wheel_size(text: str) -> str:
    """Размер колёс с оглядкой на контекст вокруг числа.

    Наивное «первое совпадение» ломается на обычном чешском объявлении:
    в 'Dámské horské kolo MERIDA (vel. 16") ... jezdí na klasických 26" kolech'
    первым идёт размер РАМЫ. Поэтому кандидаты с рамочным маркером слева
    отбрасываются, а кандидат с явным колёсным контекстом побеждает даже если
    стоит позже.
    """

    fallback = ""
    for match in WHEEL_RE.finditer(text):
        value = (match.group(1) or match.group(2) or "").replace(",", ".")
        if not value:
            continue
        before = text[max(0, match.start() - 24) : match.start()]
        after = text[match.end() : match.end() + 12]
        if WHEEL_MARKER_AFTER_RE.match(after) or WHEEL_MARKER_BEFORE_RE.search(before):
            return value
        if FRAME_MARKER_BEFORE_RE.search(before):
            continue
        if not fallback:
            fallback = value
    return fallback


def _attribute_values(text: str) -> tuple[str, int | None, str, str]:
    generation_match = GENERATION_RE.search(text)
    year_match = YEAR_RE.search(text)
    frame_match = FRAME_RE.search(text)
    generation = f"Gen {generation_match.group(1)}" if generation_match else ""
    year = int(year_match.group(1)) if year_match else None
    wheel = _wheel_size(text)
    frame = frame_match.group(1).upper() if frame_match else ""
    # Защита от путаницы колёс и рамы:
    # 1) числовой "размер рамы", совпадающий с колёсным (26/28/29) — это колёса
    # 2) "velikost kol 26" — это размер колёс, рама не указана
    if frame and frame.split(".")[0].split(",")[0] in WHEEL_LIKE_SIZES:
        frame = ""
    if frame and FRAME_WHEEL_CONFUSION_RE.search(text):
        # если совпадение рамы стоит там же, где "velikost kol" — сбрасываем
        confusion = FRAME_WHEEL_CONFUSION_RE.search(text)
        if frame_match and confusion and abs(frame_match.start() - confusion.start()) < 12:
            frame = ""
    return generation, year, wheel, frame


def _find_brand(normalized_title: str) -> tuple[str, tuple[int, int] | None]:
    for key in sorted(BRANDS, key=len, reverse=True):
        match = re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", normalized_title)
        if match:
            return BRANDS[key], match.span()
    return "", None


def _model_tail(normalized_title: str, brand_span: tuple[int, int] | None) -> str:
    """Хвост заголовка после бренда, очищенный от атрибутов и мусорных фраз."""
    if not brand_span:
        return ""
    tail = normalized_title[brand_span[1] :]
    tail = GENERATION_RE.sub(" ", tail)
    tail = YEAR_RE.sub(" ", tail)
    tail = WHEEL_RE.sub(" ", tail)
    tail = FRAME_RE.sub(" ", tail)
    tail = NOISE_PHRASES_RE.sub(" ", tail)
    return " ".join(tail.split())


def _extract_trim(tail: str) -> str:
    trim_tokens = [token for token in tail.split() if token in TRIM_WORDS]
    return " ".join(trim_tokens).upper()


def _extract_model(tail: str) -> tuple[str, str]:
    """Запасной путь: кандидат из свободных слов после бренда.

    Результат никогда не считается подтверждённой моделью — он лишь помогает
    поиску аналогов и дедупликации.
    """
    tokens = re.findall(r"[a-z0-9+.-]+", tail)
    model_tokens: list[str] = []
    trim_tokens: list[str] = []
    for token in tokens:
        token = token.strip(".-")
        if not token or token in GENERIC_WORDS or token in NOISE_WORDS:
            continue
        if token in COLOR_WORDS and model_tokens:
            break
        if token in COMPONENT_WORDS and model_tokens:
            break
        if token in TRIM_WORDS and model_tokens:
            trim_tokens.append(token)
            continue
        if len(model_tokens) >= 5:
            break
        model_tokens.append(token)
    model = " ".join(model_tokens).title()
    trim = " ".join(trim_tokens).upper()
    return model, trim


def model_family(model: str) -> str:
    tokens = normalize_text(model).split()
    while len(tokens) > 1 and re.fullmatch(r"\d+(?:[.+-]\d+)*|gen\d+", tokens[-1]):
        tokens.pop()
    return " ".join(token.title() for token in tokens)


def _component_attributes(normalized: str) -> tuple[str, str, str, str, str, int | None]:
    words = set(normalized.split())
    if any(term in normalized for term in ("full suspension", "celoodpruz", "fully", "dvojodpruz")):
        suspension = "full_suspension"
    elif any(term in words for term in ("hardtail", "pevnak")):
        suspension = "hardtail"
    else:
        suspension = ""

    if any(term in words for term in ("carbon", "karbon", "karbonovy")):
        frame_material = "carbon"
    elif any(term in words for term in ("aluminium", "aluminum", "hlinik", "alu")):
        frame_material = "aluminium"
    elif any(term in words for term in ("steel", "ocel", "ocelovy")):
        frame_material = "steel"
    else:
        frame_material = ""

    fork_class = ""
    for candidate, terms in (
        ("premium", ("fox 36", "fox 38", "lyrik", "zeb", "factory")),
        ("mid", ("fox 34", "reba", "sid", "pike", "revelation", "judy")),
        ("entry", ("suntour", "xc30", "xc32", "xcr", "rst")),
    ):
        if any(term in normalized for term in terms):
            fork_class = candidate
            break

    drivetrain_class = ""
    for candidate, terms in (
        ("premium", ("xtr", "xx1", "x01", "dura ace", "red axs")),
        ("upper", ("deore xt", "ultegra", "gx eagle", "force axs")),
        ("mid", ("deore", "slx", "nx eagle", "105", "rival")),
        ("entry", ("altus", "acera", "alivio", "tourney", "sx eagle")),
    ):
        if any(term in normalized for term in terms):
            drivetrain_class = candidate
            break

    if any(term in normalized for term in ("hydraulic", "hydraulicke", "hydraulicke brzdy")):
        brake_class = "hydraulic_disc"
    elif any(term in normalized for term in ("mechanical disc", "mechanicke kotouc")):
        brake_class = "mechanical_disc"
    elif any(term in normalized for term in ("rim brake", "v brake", "rafkove")):
        brake_class = "rim"
    else:
        brake_class = ""
    travel_match = TRAVEL_RE.search(normalized)
    travel = int(travel_match.group(1)) if travel_match else None
    return suspension, frame_material, fork_class, drivetrain_class, brake_class, travel


def identify_bike(title: str, description: str = "") -> BikeIdentity:
    clean_title = PRICE_SUFFIX_RE.sub("", title).strip()
    normalized_title = normalize_text(clean_title)
    normalized_all = normalize_text(f"{clean_title} {description[:2500]}")
    config = _IDENTITY_CONFIG
    brand, brand_span = _find_brand(normalized_title)
    tail = _model_tail(normalized_title, brand_span)
    catalog = bike_catalog.load_catalog(config.catalog_path) if config.catalog_enabled else None
    catalog_match = (
        bike_catalog.match_model(
            brand,
            tail,
            normalized_title,
            normalized_all,
            catalog=catalog,
            fuzzy_cutoff=config.fuzzy_match_cutoff,
            stop_tokens=MODEL_STOP_TOKENS,
        )
        if brand and catalog
        else None
    )
    if catalog_match:
        model = catalog_match.model
        trim = _extract_trim(tail)
        model_confirmed = True
        model_source = catalog_match.source
    else:
        model, trim = _extract_model(tail)
        model_confirmed = False
        model_source = "tail" if model else ""
    generation, year, wheel, frame = _attribute_values(normalized_all)

    bike_type = ""
    type_patterns = [
        ("gravel", ("gravel",)),
        ("road", ("silnicni", "road")),
        ("city", ("mestske", "city", "trekking")),
        ("bmx", ("bmx",)),
        ("mountain", ("horske", "horsky", "mtb", "mountain", "trail", "enduro", "downhill")),
    ]
    for candidate, words in type_patterns:
        if any(word in normalized_all.split() for word in words):
            bike_type = candidate
            break

    motor_terms = bike_catalog.motor_keywords(catalog) if catalog else ()
    electric_terms = ("elektrokolo", "e bike", " ebike", "electric bike", "motor bosch")
    if any(word in normalized_all for word in electric_terms) or any(
        term in normalized_all for term in motor_terms
    ):
        electric: bool | None = True
    elif catalog_match and catalog_match.electric:
        electric = True
    else:
        electric = False if brand and model else None

    audience = ""
    if any(word in normalized_all for word in ("detske", "detsky", "divci", "junior", "kids")):
        audience = "kids"
    elif any(word in normalized_all for word in ("damske", "women", "wmn")):
        audience = "women"
    elif any(word in normalized_all for word in ("panske", "men's")):
        audience = "men"
    if not audience and catalog_match and catalog_match.audience:
        audience = catalog_match.audience

    # Уверенность отражает подтверждения, а не заполненность полей: выдуманная
    # модель не должна получать тот же вес, что и найденная в каталоге.
    confidence = 0.0
    confidence += config.brand_score if brand else 0.0
    if model_confirmed:
        confidence += config.confirmed_model_score
    elif model:
        confidence += config.candidate_model_score
    confidence += 0.07 if generation else 0.0
    confidence += 0.06 if year else 0.0
    confidence += 0.04 if wheel else 0.0
    confidence += 0.03 if bike_type else 0.0
    if model and not model_confirmed:
        confidence = min(confidence, config.unconfirmed_confidence_cap)
    confidence = min(confidence, 1.0)
    suspension, frame_material, fork_class, drivetrain_class, brake_class, travel = (
        _component_attributes(normalized_all)
    )
    return BikeIdentity(
        brand=brand,
        model=model,
        generation=generation,
        model_year=year,
        trim=trim,
        wheel_size=wheel,
        frame_size=frame,
        bike_type=bike_type,
        electric=electric,
        audience=audience,
        model_family=model_family(model),
        suspension_type=suspension,
        frame_material=frame_material,
        fork_class=fork_class,
        drivetrain_class=drivetrain_class,
        brake_class=brake_class,
        travel_mm=travel,
        confidence=round(confidence, 2),
        model_confirmed=model_confirmed,
        model_source=model_source,
    )


def identify_listing(listing: Listing) -> BikeIdentity:
    return identify_bike(listing.title, listing.description)


def build_search_queries(identity: BikeIdentity) -> list[str]:
    variants = [
        [identity.brand, identity.model, identity.generation, str(identity.model_year or ""), identity.wheel_size],
        [identity.brand, identity.model, identity.generation, str(identity.model_year or "")],
        [identity.brand, identity.model, identity.wheel_size],
    ]
    queries: list[str] = []
    for parts in variants:
        query = " ".join(part for part in parts if part).strip()
        if query and query not in queries:
            queries.append(query)
    return queries


def has_accessory_terms(text: str) -> bool:
    normalized = normalize_text(text)
    if ACCESSORY_PHRASES_RE.search(normalized):
        return True
    return any(normalize_text(word) in normalized for word in ACCESSORY_WORDS)


def hard_filter_reason(
    listing: Listing,
    identity: BikeIdentity | None = None,
) -> str:
    """Return the existing exclusion-policy reason for a listing, if any."""
    if has_accessory_terms(listing.title):
        return "hard_filter_accessory_or_part"
    resolved_identity = identity or identify_listing(listing)
    if resolved_identity.audience == "kids":
        return "hard_filter_kids_bike"
    return ""


def has_used_terms(text: str) -> bool:
    normalized = normalize_text(text)
    return any(word in normalized for word in USED_WORDS)


def has_unavailable_terms(text: str) -> bool:
    normalized = normalize_text(text)
    return any(word in normalized for word in UNAVAILABLE_WORDS)
