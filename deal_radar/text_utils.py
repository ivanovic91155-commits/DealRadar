from __future__ import annotations

import re
import unicodedata


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    plain = plain.replace("’", "'").replace("–", "-").replace("-", " ")
    plain = re.sub(r"(?<=\d),(?=\d)", ".", plain)
    return " ".join(re.sub(r"[^a-z0-9+./\"']+", " ", plain).split())
