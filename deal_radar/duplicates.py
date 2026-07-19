from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from deal_radar.bike_identity import identify_listing, normalize_text
from deal_radar.models import Listing


PLATFORM_BOILERPLATE = (
    "otevrit inzerat",
    "zobrazit telefon",
    "odpovedet na inzerat",
    "sdilet inzerat",
    "topovat inzerat",
)
CONTACT_RE = re.compile(
    r"(?:\+?\d[\d\s().-]{7,}\d|[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class DuplicateMatch:
    level: str = "none"
    similarity: float = 0.0
    title_similarity: float = 0.0
    reason: str = ""


def normalize_duplicate_text(value: str) -> str:
    normalized = normalize_text(value).replace("...", " ")
    for phrase in PLATFORM_BOILERPLATE:
        normalized = normalized.replace(phrase, " ")
    return " ".join(normalized.split())


def description_fingerprint(value: str) -> str:
    normalized = normalize_duplicate_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def contact_fingerprint(value: str) -> str:
    contacts: list[str] = []
    for match in CONTACT_RE.findall(value):
        if "@" in match:
            contacts.append(normalize_duplicate_text(match))
            continue
        digits = re.sub(r"\D", "", match)
        # Czech phone numbers have at least nine digits. Requiring that many
        # prevents model years and wheel sizes (for example, "3 29 2025")
        # from becoming a false contact match.
        if 9 <= len(digits) <= 15:
            contacts.append(digits)
    joined = "|".join(sorted(item for item in contacts if item))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest() if joined else ""


def text_similarity(first: str, second: str, *, allow_short_overlap: bool = False) -> float:
    left = normalize_duplicate_text(first)
    right = normalize_duplicate_text(second)
    if not left or not right:
        return 0.0
    sequence = SequenceMatcher(None, left, right).ratio()
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    if not allow_short_overlap and (
        min(len(left), len(right)) < 80 or min(len(left_tokens), len(right_tokens)) < 12
    ):
        return round(sequence, 4)
    return round(max(sequence, overlap), 4)


def _compatible(value: object, candidate: object) -> bool:
    if value in {None, ""} or candidate in {None, ""}:
        return True
    if isinstance(value, str) and isinstance(candidate, str):
        return normalize_text(value) == normalize_text(candidate)
    return value == candidate


def compare_listings(
    listing: Listing,
    candidate: Listing,
    *,
    confirmed_threshold: float = 0.95,
    possible_threshold: float = 0.85,
) -> DuplicateMatch:
    if listing.source == candidate.source:
        return DuplicateMatch(reason="same_source")
    listing_price = listing.price_amount if listing.price_amount is not None else listing.price_czk
    candidate_price = candidate.price_amount if candidate.price_amount is not None else candidate.price_czk
    if not listing_price or listing_price != candidate_price:
        return DuplicateMatch(reason="price_mismatch")

    identity = identify_listing(listing)
    candidate_identity = identify_listing(candidate)
    if not identity.brand or not identity.model:
        return DuplicateMatch(reason="identity_missing")
    if normalize_text(identity.brand) != normalize_text(candidate_identity.brand):
        return DuplicateMatch(reason="brand_mismatch")
    if normalize_text(identity.model) != normalize_text(candidate_identity.model):
        return DuplicateMatch(reason="model_mismatch")
    for field_name in (
        "generation",
        "model_year",
        "wheel_size",
        "frame_size",
        "trim",
        "bike_type",
        "electric",
        "audience",
    ):
        if not _compatible(getattr(identity, field_name), getattr(candidate_identity, field_name)):
            return DuplicateMatch(reason=f"{field_name}_mismatch")

    description_score = text_similarity(listing.description, candidate.description)
    title_score = text_similarity(listing.title, candidate.title, allow_short_overlap=True)
    first_contact = contact_fingerprint(f"{listing.title}\n{listing.description}")
    second_contact = contact_fingerprint(f"{candidate.title}\n{candidate.description}")
    same_contact = bool(first_contact and first_contact == second_contact)

    if description_score >= confirmed_threshold and (title_score >= 0.70 or same_contact):
        return DuplicateMatch(
            "confirmed",
            description_score,
            title_score,
            "same_price_identity_and_description",
        )
    if description_score >= possible_threshold and title_score >= 0.60:
        return DuplicateMatch(
            "possible",
            description_score,
            title_score,
            "same_price_identity_and_similar_text",
        )
    return DuplicateMatch(
        "none",
        description_score,
        title_score,
        "text_similarity_below_threshold",
    )


def duplicate_group_id(keys: list[str]) -> str:
    payload = "|".join(sorted(keys))
    return "dup-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
