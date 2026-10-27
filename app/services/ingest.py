"""OrderIngestService — channel-agnostic order ingestion pipeline.

End-to-end behavior:

1. Idempotently upserts the order on (channel, channel_order_id).
2. For each line item, resolves the master_sku_id via ChannelSkuMapping.
   - If mapped: persists the OrderItem with master_sku_id set and emits
     `order_consumed` (or no event for cancelled orders).
   - If unmapped: persists the OrderItem with master_sku_id NULL, registers
     a MappingAlert, and leaves the order in `pending_mapping` status.
3. Cancellation transitions emit `cancellation_returned` events for items
   that were previously consumed, using the same source identifiers — the
   UNIQUE constraint on inventory_events guarantees no double compensation.

The service does NOT commit; the caller controls transaction boundaries so
ingestion of a single channel order is all-or-nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import NormalizedOrder, NormalizedOrderLine
from app.models import (
    MappingAlert,
    MappingAlertStatusEnum,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services.inventory import EventSource, InventoryService
from app.services.mapping import resolve_master_sku_id


@dataclass(frozen=True, slots=True)
class IngestResult:
    order: Order
    created: bool
    pending_mapping_count: int
    consumed_count: int
    cancelled_count: int
    #: Mapped lines whose master is 在庫管理対象外 (gift boxes, coupons,
    #: made-to-order). No inventory event is written for them, on the consume
    #: path or the cancellation path. Reported separately from
    #: `pending_mapping_count` because "deliberately not stocked" and "we don't
    #: know what this is" are opposite situations: one needs no action, the other
    #: needs an operator. Collapsing them is how the Phase 1-B ingest logs made a
    #: healthy run look like a backlog.
    unmanaged_skipped: int = 0


_CANCEL_STATUSES = {"cancelled", "returned"}


class OrderIngestService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._inventory = InventoryService(session)

    async def ingest(self, payload: NormalizedOrder) -> IngestResult:
        existing = await self._find_order(payload.channel, payload.channel_order_id)
        if existing is None:
            return await self._ingest_new(payload)
        return await self._ingest_update(existing, payload)

    # ---------- new orders ----------

    async def _ingest_new(self, payload: NormalizedOrder) -> IngestResult:
        is_cancelled = payload.status in _CANCEL_STATUSES
        order = Order(
            channel=payload.channel,
            channel_order_id=payload.channel_order_id,
            marketplace_id=payload.marketplace_id,
            status=payload.status,
            ordered_at=payload.ordered_at,
            raw_payload=payload.raw_payload,
        )
        self._session.add(order)
        await self._session.flush()

        pending = 0
        consumed = 0
        unmanaged = 0
        for line in payload.items:
            mapped_id = await self._lookup_mapping(payload.channel, line, payload.marketplace_id)
            item = OrderItem(
                order_id=order.id,
                line_id=line.line_id,
                channel_sku=line.channel_sku,
                master_sku_id=mapped_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                currency=line.currency,
                fulfillment_type=line.fulfillment_type,
            )
            self._session.add(item)
            await self._session.flush()

            if mapped_id is None:
                await self._record_alert(payload, line)
                pending += 1
                continue
            if is_cancelled:
                # Cancelled-on-arrival: never consumed, nothing to compensate.
                continue
            source = EventSource(
                channel=payload.channel,
                order_id=payload.channel_order_id,
                line_id=line.line_id,
            )
            # A bundle/shared-stock line fans out to its components; a normal SKU
            # consumes itself; a 在庫管理対象外 master writes nothing at all.
            # `consume_order_line` owns all three — see its docstring for what
            # went wrong when each caller decided for itself.
            application = await self._inventory.consume_order_line(
                master_sku_id=mapped_id,
                quantity=line.quantity,
                source=source,
                occurred_at=payload.ordered_at,
            )
            if application.unmanaged:
                unmanaged += 1
                continue
            consumed += 1

        if pending and not is_cancelled:
            order.status = OrderStatusEnum.PENDING_MAPPING

        return IngestResult(
            order=order,
            created=True,
            pending_mapping_count=pending,
            consumed_count=consumed,
            cancelled_count=0,
            unmanaged_skipped=unmanaged,
        )

    # ---------- existing orders ----------

    async def _ingest_update(self, order: Order, payload: NormalizedOrder) -> IngestResult:
        cancelled = 0
        unmanaged = 0
        prev_cancelled = order.status in _CANCEL_STATUSES
        now_cancelled = payload.status in _CANCEL_STATUSES

        if payload.raw_payload is not None:
            order.raw_payload = payload.raw_payload

        if now_cancelled and not prev_cancelled:
            # Stamp the compensation at CANCELLATION time, not the original order
            # time. Backdating writes an event at T-60 for a 60-day-old order,
            # which silently corrupts every daily aggregate from that date
            # forward — a forward running sum never revisits it, and no fixed
            # rebuild window can see it.
            cancelled, unmanaged = await self._compensate_lines(order, datetime.now(UTC))

        order.status = payload.status

        return IngestResult(
            order=order,
            created=False,
            pending_mapping_count=0,
            consumed_count=0,
            cancelled_count=cancelled,
            unmanaged_skipped=unmanaged,
        )

    async def _compensate_lines(self, order: Order, occurred_at: datetime) -> tuple[int, int]:
        """Returns (compensated_lines, unmanaged_lines_skipped)."""
        result = await self._session.execute(
            select(OrderItem).where(
                OrderItem.order_id == order.id,
                OrderItem.master_sku_id.is_not(None),
            ),
        )
        compensated = 0
        unmanaged = 0
        for item in result.scalars().all():
            assert item.master_sku_id is not None  # narrowed by WHERE
            source = EventSource(
                channel=order.channel,
                order_id=order.channel_order_id,
                line_id=item.line_id,
            )
            # Mirrors the consume fan-out by construction — same expansion, same
            # 在庫管理対象外 stop. The OrderItem holds the parent id.
            application = await self._inventory.return_order_line(
                master_sku_id=item.master_sku_id,
                quantity=item.quantity,
                source=source,
                occurred_at=occurred_at,
            )
            if application.unmanaged:
                unmanaged += 1
                continue
            if application.applied:
                compensated += 1
        return compensated, unmanaged

    # ---------- helpers ----------

    async def _find_order(self, channel: str, channel_order_id: str) -> Order | None:
        result = await self._session.execute(
            select(Order).where(
                Order.channel == channel,
                Order.channel_order_id == channel_order_id,
            ),
        )
        return result.scalar_one_or_none()

    async def _lookup_mapping(
        self,
        channel: str,
        line: NormalizedOrderLine,
        marketplace_id: str | None,
    ) -> int | None:
        # The line's own variant decides the master. While this passed None,
        # every Rakuten order resolved at page level, so a page whose SKUs had
        # been split into per-colour/size masters kept consuming the superseded
        # page master instead: measured 2026-10-27 as 40 order_consumed events
        # over 30 days against 14 masters the stocktake deliberately excludes,
        # which means nothing would ever correct them. Stays None for channels
        # whose channel_sku is already the sellable unit, and the resolver then
        # matches the page-level row exactly as it did before.
        return await resolve_master_sku_id(
            self._session,
            channel=channel,
            channel_sku=line.channel_sku,
            marketplace_id=marketplace_id,
            variant_key=line.channel_variant_key,
        )

    async def _record_alert(
        self,
        payload: NormalizedOrder,
        line: NormalizedOrderLine,
    ) -> None:
        """Upsert the alert; UNIQUE on (channel, channel_sku, marketplace_id)."""
        alert = MappingAlert(
            channel=payload.channel,
            channel_sku=line.channel_sku,
            channel_product_id=line.channel_product_id,
            product_name=line.product_name,
            marketplace_id=payload.marketplace_id,
            status=MappingAlertStatusEnum.OPEN,
            first_seen_at=datetime.now(UTC),
        )
        try:
            async with self._session.begin_nested():
                self._session.add(alert)
                await self._session.flush()
        except IntegrityError:
            # Already alerted — increment occurrence_count.
            existing = await self._session.execute(
                select(MappingAlert).where(
                    MappingAlert.channel == payload.channel,
                    MappingAlert.channel_sku == line.channel_sku,
                    MappingAlert.marketplace_id.is_(payload.marketplace_id),
                ),
            )
            row = existing.scalar_one()
            row.occurrence_count += 1
            # Backfill identifying info if an earlier alert lacked it.
            if not row.product_name and line.product_name:
                row.product_name = line.product_name
            if not row.channel_product_id and line.channel_product_id:
                row.channel_product_id = line.channel_product_id
            if row.status == MappingAlertStatusEnum.RESOLVED:
                # A previously-resolved alert came back unmapped — reopen it.
                row.status = MappingAlertStatusEnum.OPEN
