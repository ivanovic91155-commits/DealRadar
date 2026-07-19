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
    r"\b(?:vel(?:ikost)?\.?|size|ram(?:u)?|frame)\s*[:.-]?\s*(xxs|xs|s|m/?l|m|l|xl|xxl|\d{2}(?:[.,]\d)?)\b",
    re.IGNORECASE,
)
PRICE_SUFFIX_RE = re.compile(r"\s*:\s*\d[\d\s.\u00a0]*\s*$")

GENERIC_WORDS = {
    "bike", "bicycle", "bicykl", "cyklo", "e", "elektrokolo", "ebike", "e-bike", "horsky", "horske",
    "jizdni", "kolo", "kola", "mtb", "detske", "detsky", "divci", "damske", "panske", "prodám",
    "alu", "hlinikovy", "prodam", "prodej", "model", "novy", "nove", "zanovni", "v", "vel", "velikost",
    "ram", "ramu", "frame", "size", "zaruce",
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
        ("mountain", ("horske", "horsky", "mtb", "trail", "enduro", "downhill")),
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


def has_used_terms(text: str) -> bool:
    normalized = normalize_text(text)
    return any(word in normalized for word in USED_WORDS)


def has_unavailable_terms(text: str) -> bool:
    normalized = normalize_text(text)
    return any(word in normalized for word in UNAVAILABLE_WORDS)
