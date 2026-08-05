from __future__ import annotations

import re
import unicodedata

from deal_radar.models import BikeIdentity, Listing


BRANDS = {
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
# Числовые размеры рамы валидны только как ростовка: дюймы 13-23 или см 38-62.
# Всё, что похоже на колёса (24/26/27/28/29), рамой быть не может.
WHEEL_LIKE_SIZES = {"24", "26", "27", "28", "29"}
PRICE_SUFFIX_RE = re.compile(r"\s*:\s*\d[\d\s.\u00a0]*\s*$")
TRAVEL_RE = re.compile(r"\b(80|90|100|110|120|130|140|150|160|170|180|190|200)\s*mm\b", re.IGNORECASE)

GENERIC_WORDS = {
    "bike", "bicycle", "bicykl", "cyklo", "e", "elektrokolo", "ebike", "e-bike", "horsky", "horske",
    "jizdni", "kolo", "kola", "mtb", "detske", "detsky", "divci", "damske", "panske", "prodám",
    "alu", "hlinikovy", "prodam", "prodej", "model", "novy", "nove", "zanovni", "v", "vel", "velikost",
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
ACCESSORY_WORDS = {
    "baterie", "battery", "charger", "fork", "frame only", "helmet", "nabijecka", "plášť", "plast",
    "pneumatika", "ram kola", "sedlo", "vidlice", "bearing", "bearings", "headset", "lozisko",
    "lozisek",
}
USED_WORDS = {"bazar", "pouzite", "pouzity", "refurbished", "repasovane", "repasovany", "used"}
UNAVAILABLE_WORDS = {"neni skladem", "out of stock", "unavailable", "vyprodano"}


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    plain = plain.replace("’", "'").replace("–", "-").replace("-", " ")
    plain = re.sub(r"(?<=\d),(?=\d)", ".", plain)
    return " ".join(re.sub(r"[^a-z0-9+./\"']+", " ", plain).split())


def _attribute_values(text: str) -> tuple[str, int | None, str, str]:
    generation_match = GENERATION_RE.search(text)
    year_match = YEAR_RE.search(text)
    wheel_match = WHEEL_RE.search(text)
    frame_match = FRAME_RE.search(text)
    generation = f"Gen {generation_match.group(1)}" if generation_match else ""
    year = int(year_match.group(1)) if year_match else None
    wheel_value = (wheel_match.group(1) or wheel_match.group(2)) if wheel_match else ""
    wheel = wheel_value.replace(",", ".") if wheel_value else ""
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


def _extract_model(normalized_title: str, brand_span: tuple[int, int] | None) -> tuple[str, str]:
    if not brand_span:
        return "", ""
    tail = normalized_title[brand_span[1] :]
    tail = GENERATION_RE.sub(" ", tail)
    tail = YEAR_RE.sub(" ", tail)
    tail = WHEEL_RE.sub(" ", tail)
    tail = FRAME_RE.sub(" ", tail)
    tokens = re.findall(r"[a-z0-9+.-]+", tail)
    model_tokens: list[str] = []
    trim_tokens: list[str] = []
    for token in tokens:
        token = token.strip(".-")
        if not token or token in GENERIC_WORDS:
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
    brand, brand_span = _find_brand(normalized_title)
    model, trim = _extract_model(normalized_title, brand_span)
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

    if any(word in normalized_all for word in ("elektrokolo", "e bike", " ebike", "electric bike", "motor bosch")):
        electric: bool | None = True
    else:
        electric = False if brand and model else None

    audience = ""
    if any(word in normalized_all for word in ("detske", "detsky", "divci", "junior", "kids")):
        audience = "kids"
    elif any(word in normalized_all for word in ("damske", "women", "wmn")):
        audience = "women"
    elif any(word in normalized_all for word in ("panske", "men's")):
        audience = "men"

    confidence = 0.0
    confidence += 0.45 if brand else 0.0
    confidence += 0.35 if model else 0.0
    confidence += 0.07 if generation else 0.0
    confidence += 0.06 if year else 0.0
    confidence += 0.04 if wheel else 0.0
    confidence += 0.03 if bike_type else 0.0
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
