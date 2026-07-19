"""Backward-compatible imports for the retail pricing subsystem.

The MVP no longer calls OpenAI API. New code should import from
``deal_radar.pricing`` and ``deal_radar.price_sources`` directly.
"""

from __future__ import annotations

import hashlib

from deal_radar.bike_identity import identify_listing
from deal_radar.models import Listing
from deal_radar.pricing import NewBikePriceService, calculate_median, discount_percent


def listing_fingerprint(listing: Listing) -> str:
    identity = identify_listing(listing)
    if identity.brand and identity.model:
        return identity.normalized_key
    normalized = " ".join(listing.title.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


__all__ = ["NewBikePriceService", "calculate_median", "discount_percent", "listing_fingerprint"]
