"""ChannelAdapter abstract base class — common interface for all channels.

`fetch_orders` and `verify_webhook` are the read side; `push_inventory` is the
writeback.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

ChannelName = Literal["rakuten", "shopify", "amazon", "wholesale"]
NormalizedStatus = Literal[
    "pending",  # order received, not yet processed
    "confirmed",  # confirmed by channel
    "shipped",
    "delivered",
    "cancelled",
    "returned",
]


class NormalizedOrderLine(BaseModel):
    """Channel-agnostic order line item."""

    line_id: str
    channel_sku: str
    #: Rakuten SKU管理番号 (`variantId`). `channel_sku` is the page
    #: (商品管理番号), and on this shop a page sells several colours and sizes,
    #: so the page alone does not say what was bought. None for channels whose
    #: `channel_sku` is already the sellable unit (Shopify, Amazon).
    channel_variant_key: str | None = None
    channel_product_id: str | None = None
    product_name: str | None = None  # channel's own product/item name (for alerts)
    quantity: int = Field(gt=0)
    unit_price: Decimal
    currency: str = "JPY"
    fulfillment_type: str | None = None  # FBA / MFN / self / None


class NormalizedOrder(BaseModel):
    """Channel-agnostic order representation.

    Each ChannelAdapter is responsible for converting channel-specific
    payloads into this shape so downstream services stay channel-neutral.
    """

    channel: ChannelName
    channel_order_id: str
    marketplace_id: str | None = None
    status: NormalizedStatus
    ordered_at: datetime
    items: list[NormalizedOrderLine]
    raw_payload: dict[str, Any] | None = None


class ChannelAdapter(ABC):
    """Abstract interface every channel must implement."""

    channel: ChannelName

    @abstractmethod
    async def fetch_orders(
        self,
        since: datetime,
        until: datetime | None = None,
    ) -> list[NormalizedOrder]:
        """Fetch orders updated within the given window."""

    @abstractmethod
    async def push_inventory(
        self,
        sku: str,
        quantity: int,
    ) -> dict[str, Any] | None:
        """Push inventory level to the channel.

        Return the channel's response as a dict when there is one, so the caller
        can persist it in `sync_attempts.response_payload`, or `None` when there
        is not. `None` is a normal success, not a degraded one: Rakuten answers a
        successful 在庫API 2.0 upsert with 204 No Content and an empty body, so
        there is nothing to record beyond the attempt itself. Shopify returns the
        mutation result.

        An implementation may take additional KEYWORD-ONLY arguments where the
        channel needs more than a SKU to identify what it is setting — Rakuten
        requires `variant_key` (SKU管理番号), because its `sku` is a page that
        carries several sellable SKUs.
        """

    @abstractmethod
    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify webhook authenticity (HMAC, etc.).

        Channels without webhooks may return True unconditionally.
        """
