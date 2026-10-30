"""One place to build a channel adapter from settings.

Two CLIs had already copied this factory verbatim — they differed only in the
word used in the error message — and the Shopify stock audit needed a third.
docs/23 §1-12 is explicit that shared code lands once; an unowned helper copied
a third time is how the copies start drifting.

`purpose` only shapes the error text, but it is worth keeping: "credentials are
missing, cannot push" and "cannot audit stock" send an operator to different
places, and the whole reason the copies existed was that one message.
"""

from __future__ import annotations

from app.adapters import RakutenAdapter, ShopifyAdapter
from app.config import get_settings


def build_shopify_adapter(*, purpose: str) -> ShopifyAdapter:
    settings = get_settings()
    if not settings.shopify_shop_domain or not settings.shopify_access_token:
        raise RuntimeError(f"Shopify credentials are missing from settings; cannot {purpose}.")
    return ShopifyAdapter(
        shop_domain=settings.shopify_shop_domain,
        access_token=settings.shopify_access_token,
        webhook_secret=settings.shopify_webhook_secret,
        api_version=settings.shopify_api_version,
        location_id=settings.shopify_location_id,
    )


def build_rakuten_adapter(*, purpose: str) -> RakutenAdapter:
    """The RMS credential pair, or a message naming what cannot be done without it.

    Both halves or neither: RMS authenticates `base64(serviceSecret:licenseKey)`
    as a unit, so a half-configured environment produces a 401 that looks exactly
    like a wrong key. Refusing here says which it is.

    The adapter strips whitespace from both values itself. That is not redundant
    with `run_cli.ps1` doing the same — a Secret Manager value copied from a
    Windows source carries a trailing CR, which corrupts the base64 token and
    returned ES04-01 on every call until it was found.
    """
    settings = get_settings()
    if not settings.rakuten_service_secret or not settings.rakuten_license_key:
        raise RuntimeError(f"Rakuten RMS credentials are missing from settings; cannot {purpose}.")
    return RakutenAdapter(
        service_secret=settings.rakuten_service_secret,
        license_key=settings.rakuten_license_key,
        shop_url=settings.rakuten_shop_url or None,
    )


__all__ = ["build_rakuten_adapter", "build_shopify_adapter"]
