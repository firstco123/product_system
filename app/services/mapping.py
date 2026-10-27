"""MappingService — resolve channel SKUs and replay pending orders.

When an order arrives whose `channel_sku` has no active mapping, ingestion
leaves the order in `pending_mapping` state and records a `MappingAlert`.
Once the operator resolves the alert by creating a `ChannelSkuMapping`, this
service backfills `master_sku_id` on the parked order items and applies them to
stock — preserving idempotency end-to-end.

Two rules make the replay match first ingestion, which it did not before:

* It applies each line through `InventoryService.consume_order_line`, so a
  共有在庫 parent reaches its pool and a 在庫管理対象外 master writes nothing.
* It confirms an order only when nothing of it is unmapped. One alert is one
  channel SKU, and an order can be waiting on several.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.logging import get_logger
from app.models import (
    ChannelSkuMapping,
    MappingAlert,
    MappingAlertStatusEnum,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services.exceptions import AmbiguousChannelSkuError, MappingNotFoundError
from app.services.inventory import EventSource, InventoryService
from app.services.timeframe import to_jst_date

log = get_logger(__name__)

#: Same set `OrderIngestService` uses; a cancelled line was never consumed.
_CANCEL_STATUSES = {"cancelled", "returned"}


def same_marketplace(
    column: InstrumentedAttribute[str | None],
    marketplace_id: str | None,
) -> ColumnElement[bool]:
    """`column` equals `marketplace_id`, with NULL treated as a value.

    `column.is_(x)` is valid SQL only for NULL and booleans. Given anything else
    it emits `marketplace_id IS $1`, which Postgres rejects outright as a syntax
    error — so this is not a subtle mismatch, it is a query that cannot run.

    All four places that compared a marketplace were written that way, and none
    of them could fail: Rakuten and Shopify both leave `marketplace_id` unset,
    so every call passed None and every call got `IS NULL`. Wholesale is the
    first channel with a real one (発注者コード 0393, docs/29 §5-1), which is
    what made four latent bugs fire at once. Amazon would have done the same.

    A conditional rather than `IS NOT DISTINCT FROM`, which is also correct but
    which Postgres will not serve from a btree index. These predicates run on
    the hourly order poll and once per product code per wholesale upload.
    """
    if marketplace_id is None:
        return column.is_(None)
    return column == marketplace_id


async def resolve_master_sku_id(
    session: AsyncSession,
    *,
    channel: str,
    channel_sku: str,
    marketplace_id: str | None = None,
    variant_key: str | None = None,
) -> int | None:
    """The one definition of "which master does this channel line belong to".

    Two steps, because a Rakuten page keeps its page-level mapping row while its
    SKUs are mapped one at a time (see `ChannelSkuMapping`):

    1. the row whose `channel_variant_key` is exactly `variant_key`, NULL included;
    2. only when a `variant_key` was supplied and step 1 found nothing, the
       page-level row — so a page that is not yet mapped per variant still
       resolves, and no flag day is needed.

    Pinning `channel_variant_key` is the point. A query that leaves it out
    matches the page-level row AND every variant row of that page, so the
    `scalar_one_or_none()` below would raise MultipleResultsFound the moment the
    first variant row is inserted — on the hourly order poll, which
    `internal_jobs._run_job` turns into an HTTP 500 and a critical Slack alert.

    Each step is covered by `uq_channel_sku_mapping`, so each returns at most one
    row. `scalar_one_or_none()` is kept deliberately over `.first()`: if that
    constraint is ever lost, this must fail loudly rather than silently pick one
    of several variants.
    """
    for step, key_condition in enumerate(_variant_key_steps(variant_key)):
        result = await session.execute(
            select(ChannelSkuMapping.master_sku_id).where(
                ChannelSkuMapping.channel == channel,
                ChannelSkuMapping.channel_sku == channel_sku,
                same_marketplace(ChannelSkuMapping.marketplace_id, marketplace_id),
                key_condition,
                ChannelSkuMapping.is_active.is_(True),
            ),
        )
        found = result.scalar_one_or_none()
        if found is not None:
            if step and variant_key is not None:
                # The line named a variant, but only the page-level row matched:
                # the (page, variant) pair is unmapped, so consumption lands on
                # whatever that page points at. Logged because the silent
                # version of this is what let the superseded page masters keep
                # draining — a key that fails to match reads exactly like a key
                # that was never supplied.
                log.info(
                    "mapping.page_level_fallback",
                    channel=channel,
                    channel_sku=channel_sku,
                    variant_key=variant_key,
                    master_sku_id=found,
                )
            return found
    return None


@dataclass(frozen=True, slots=True)
class ChannelTarget:
    """One place on a channel where a master's stock has to be written.

    `channel_sku` alone is not an address on every channel. On Shopify it is the
    sellable unit and `variant_key` is None for ever; on Rakuten it is the PAGE
    (商品管理番号) and `variant_key` the SKU管理番号 inside it, and the pair is
    what the 在庫API requires.
    """

    channel_sku: str
    variant_key: str | None = None

    @property
    def label(self) -> str:
        """For logs and audit rows, where "which SKU was that?" has to be
        answerable from one string."""
        return f"{self.channel_sku}/{self.variant_key}" if self.variant_key else self.channel_sku


async def channel_targets(
    session: AsyncSession,
    *,
    master_sku_id: int,
    channel: str,
) -> list[ChannelTarget]:
    """EVERY active mapping this master has on this channel.

    The inverse of `resolve_master_sku_id`, and deliberately a list. One master
    is sold from several Rakuten pages — 558 of 609 masters held more than one,
    measured 2026-10-27 — so "the channel SKU for this master" is a question with
    no single answer, and code that asked it that way wrote one page's stock and
    left the rest stale.

    `bundle_push` asked exactly that, with `.scalars().first()` and no ORDER BY:
    Postgres was free to return a different page on every run, so which of a
    master's pages got the correct number was arbitrary and changed between
    pushes. Ordering is fixed here so a repeated push is a repeat, and logs from
    two runs can be compared.
    """
    result = await session.execute(
        select(ChannelSkuMapping.channel_sku, ChannelSkuMapping.channel_variant_key)
        .where(
            ChannelSkuMapping.master_sku_id == master_sku_id,
            ChannelSkuMapping.channel == channel,
            ChannelSkuMapping.is_active.is_(True),
        )
        .order_by(ChannelSkuMapping.channel_sku, ChannelSkuMapping.channel_variant_key)
    )
    return [ChannelTarget(channel_sku=sku, variant_key=key) for sku, key in result.tuples().all()]


def _variant_key_steps(variant_key: str | None) -> list[Any]:
    """The variant-key predicate per step, most specific first."""
    if variant_key is None:
        return [ChannelSkuMapping.channel_variant_key.is_(None)]
    return [
        ChannelSkuMapping.channel_variant_key == variant_key,
        ChannelSkuMapping.channel_variant_key.is_(None),
    ]


class MappingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._inventory = InventoryService(session)

    async def resolve_alert(
        self,
        *,
        channel: str,
        channel_sku: str,
        master_sku_id: int,
        marketplace_id: str | None = None,
        channel_product_id: str | None = None,
    ) -> int:
        """Map an unmapped channel SKU and replay all pending order lines.

        Returns the number of order lines that were replayed.

        Refuses a blank key. It is not one product: every Shopify variant with
        no SKU set arrives as the empty string, and they all share one alert.
        Accepting it maps them all onto whatever master the operator picked for
        the one name the alert happens to show.
        """
        if not channel_sku.strip():
            raise AmbiguousChannelSkuError(
                f"{channel}: 商品コードが空欄の明細は、複数の商品が同じキーになっています。"
                "1つの商品に紐づけることはできません。"
            )
        await self._upsert_mapping(
            channel=channel,
            channel_sku=channel_sku,
            master_sku_id=master_sku_id,
            marketplace_id=marketplace_id,
            channel_product_id=channel_product_id,
        )
        await self._close_alert(
            channel=channel,
            channel_sku=channel_sku,
            marketplace_id=marketplace_id,
            master_sku_id=master_sku_id,
        )
        return await self._replay_pending_lines(
            channel=channel,
            channel_sku=channel_sku,
            master_sku_id=master_sku_id,
        )

    async def find_master_sku_id(
        self,
        *,
        channel: str,
        channel_sku: str,
        marketplace_id: str | None = None,
        variant_key: str | None = None,
    ) -> int | None:
        return await resolve_master_sku_id(
            self._session,
            channel=channel,
            channel_sku=channel_sku,
            marketplace_id=marketplace_id,
            variant_key=variant_key,
        )

    # ---------- internals ----------

    async def _upsert_mapping(
        self,
        *,
        channel: str,
        channel_sku: str,
        master_sku_id: int,
        marketplace_id: str | None,
        channel_product_id: str | None,
    ) -> ChannelSkuMapping:
        result = await self._session.execute(
            select(ChannelSkuMapping).where(
                ChannelSkuMapping.channel == channel,
                ChannelSkuMapping.channel_sku == channel_sku,
                same_marketplace(ChannelSkuMapping.marketplace_id, marketplace_id),
            ),
        )
        mapping = result.scalar_one_or_none()
        if mapping is not None:
            mapping.master_sku_id = master_sku_id
            mapping.is_active = True
            if channel_product_id is not None:
                mapping.channel_product_id = channel_product_id
            return mapping

        mapping = ChannelSkuMapping(
            channel=channel,
            channel_sku=channel_sku,
            channel_product_id=channel_product_id,
            marketplace_id=marketplace_id,
            master_sku_id=master_sku_id,
            is_active=True,
        )
        self._session.add(mapping)
        await self._session.flush()
        return mapping

    async def _close_alert(
        self,
        *,
        channel: str,
        channel_sku: str,
        marketplace_id: str | None,
        master_sku_id: int,
    ) -> None:
        await self._session.execute(
            update(MappingAlert)
            .where(
                MappingAlert.channel == channel,
                MappingAlert.channel_sku == channel_sku,
                same_marketplace(MappingAlert.marketplace_id, marketplace_id),
                MappingAlert.status.in_(
                    [MappingAlertStatusEnum.OPEN, MappingAlertStatusEnum.IN_PROGRESS]
                ),
            )
            .values(
                status=MappingAlertStatusEnum.RESOLVED,
                resolved_master_sku_id=master_sku_id,
                resolved_at=datetime.now(UTC),
            ),
        )

    async def _replay_pending_lines(
        self,
        *,
        channel: str,
        channel_sku: str,
        master_sku_id: int,
    ) -> int:
        """Backfill master_sku_id on parked items and apply them to stock."""
        rows = await self._session.execute(
            select(OrderItem, Order)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.channel == channel,
                Order.status == OrderStatusEnum.PENDING_MAPPING,
                OrderItem.channel_sku == channel_sku,
                OrderItem.master_sku_id.is_(None),
            ),
        )
        replayed = 0
        touched: dict[int, Order] = {}
        for item, order in rows.all():
            item.master_sku_id = master_sku_id
            # Same fan-out as first ingestion: a 共有在庫 parent moves its
            # components, a 在庫管理対象外 master moves nothing.
            await self._inventory.consume_order_line(
                master_sku_id=master_sku_id,
                quantity=item.quantity,
                source=EventSource(
                    channel=order.channel,
                    order_id=order.channel_order_id,
                    line_id=item.line_id,
                ),
                occurred_at=order.ordered_at,
            )
            touched[order.id] = order
            replayed += 1

        if not replayed:
            return 0
        await self._session.flush()

        # Confirm only the orders with nothing left unmapped. Resolving one
        # alert used to mark the whole order 確定, so a two-line order carrying
        # two unknown SKUs lost its second line: still NULL, never consumed,
        # and no longer visible to anything that looks for pending_mapping.
        await _settle_orders(self._session, touched)
        return replayed


# ---------------------------------------------------------------------------
# 一括再解決  (bulk re-resolution)
# ---------------------------------------------------------------------------
#
# The alert screen resolves one channel SKU at a time. This resolves whatever
# is already resolvable, in bulk, for the two moments that produce a backlog:
# mappings added en masse, and the client correcting 管理番号 on the RMS side.
#
# WHY IT SCANS LINES AND NOT ORDERS
#
# An order's status is a summary; `master_sku_id IS NULL` is the fact. Lines
# stranded by the old alert-resolution behaviour sit on orders already marked
# 確定, so a status-based scan cannot see them at all.


@dataclass(slots=True)
class UnresolvedSku:
    """One channel SKU that still has no mapping, with the damage behind it."""

    lines: int = 0
    units: int = 0
    first_ordered_at: datetime | None = None
    last_ordered_at: datetime | None = None

    def add(self, *, quantity: int, ordered_at: datetime | None) -> None:
        self.lines += 1
        self.units += quantity
        if ordered_at is None:
            return
        if self.first_ordered_at is None or ordered_at < self.first_ordered_at:
            self.first_ordered_at = ordered_at
        if self.last_ordered_at is None or ordered_at > self.last_ordered_at:
            self.last_ordered_at = ordered_at


@dataclass(frozen=True, slots=True)
class ReResolution:
    lines_filled: int
    orders_settled: int
    stock_events: int
    cancelled_skipped: int
    unmanaged_skipped: int
    unresolved: dict[tuple[str, str], UnresolvedSku]
    #: Revenue that moves from 未マッピング to a real SKU, cancelled orders
    #: excluded to match how the KPI counts it. The line count alone does not
    #: answer the question 検収 actually asks — 未マッピング売上 is a share of
    #: revenue, and 734 lines could be ¥40,000 or ¥400,000.
    filled_sales_jpy: Decimal = Decimal(0)
    #: Revenue still attributable to nothing, on the same basis.
    unresolved_sales_jpy: Decimal = Decimal(0)
    #: JST span of the lines actually filled, for the rollup rebuild that has
    #: to follow. The rebuild picks its own days from inventory events and
    #: order updates, and this pass produces NEITHER — it rewrites
    #: `order_items.master_sku_id` and nothing else. Detection therefore sees
    #: no change at all, so the range has to be handed over explicitly or the
    #: dashboards keep showing the old attribution for ever.
    filled_first_day: date | None = None
    filled_last_day: date | None = None
    #: Lines whose channel SKU is blank. Skipped deliberately: the key does
    #: not identify one product, so any mapping found under it would be
    #: applied to all of them. Counted rather than dropped, because they are
    #: real revenue that needs a different fix.
    blank_key_lines: int = 0
    blank_key_sales_jpy: Decimal = Decimal(0)


async def reresolve_unmapped_lines(
    session: AsyncSession,
    *,
    apply_stock: bool,
    channel: str | None = None,
    limit: int | None = None,
) -> ReResolution:
    """Fill `master_sku_id` on every line a current mapping can explain.

    `apply_stock` is the whole decision, and it has no safe default:

    * **False** — the historical pass. The physical stocktake and the reconcile
      already settled what is on the shelf, so replaying months of consumption
      on top would subtract the same goods a second time. The point of that run
      is the sales history (P2-011 / P2-042), which reads `master_sku_id`, not
      the event log. Rebuild the daily rollups afterwards.
    * **True** — the live pass, for lines whose stock has not been settled
      since. It matches what first ingestion would have done.

    Nothing here commits; the caller owns the transaction.
    """
    inventory = InventoryService(session)
    unresolved: dict[tuple[str, str], UnresolvedSku] = {}
    touched: dict[int, Order] = {}
    lines_filled = 0
    stock_events = 0
    cancelled_skipped = 0
    unmanaged_skipped = 0
    filled_sales = Decimal(0)
    unresolved_sales = Decimal(0)
    filled_days: list[date] = []
    blank_key_lines = 0
    blank_key_sales = Decimal(0)

    stmt = (
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.master_sku_id.is_(None))
        .order_by(Order.ordered_at, OrderItem.id)
    )
    if channel:
        stmt = stmt.where(Order.channel == channel)
    if limit:
        stmt = stmt.limit(limit)

    for item, order in (await session.execute(stmt)).all():
        live_amount = (
            item.quantity * item.unit_price if order.status not in _CANCEL_STATUSES else Decimal(0)
        )
        if not (item.channel_sku or "").strip():
            # See `blank_key_lines`. Resolving these needs the variant id, not
            # a mapping — `inspect_blank_channel_sku` shows what is behind them.
            blank_key_lines += 1
            blank_key_sales += live_amount
            continue

        master_sku_id = await resolve_master_sku_id(
            session,
            channel=order.channel,
            channel_sku=item.channel_sku,
            marketplace_id=order.marketplace_id,
        )
        # Cancelled lines are excluded from both amounts, to match how the KPI
        # counts 未マッピング売上 — otherwise the figure here would not be
        # comparable with the one on the screen.
        amount = live_amount

        if master_sku_id is None:
            key = (order.channel, item.channel_sku)
            unresolved.setdefault(key, UnresolvedSku()).add(
                quantity=item.quantity, ordered_at=order.ordered_at
            )
            unresolved_sales += amount
            continue

        item.master_sku_id = master_sku_id
        lines_filled += 1
        filled_sales += amount
        if order.ordered_at is not None:
            filled_days.append(to_jst_date(order.ordered_at))
        touched[order.id] = order

        if not apply_stock:
            continue
        if order.status in _CANCEL_STATUSES:
            # Never consumed, so there is nothing to consume now.
            cancelled_skipped += 1
            continue

        application = await inventory.consume_order_line(
            master_sku_id=master_sku_id,
            quantity=item.quantity,
            source=EventSource(
                channel=order.channel,
                order_id=order.channel_order_id,
                line_id=item.line_id,
            ),
            occurred_at=order.ordered_at,
        )
        if application.unmanaged:
            unmanaged_skipped += 1
        stock_events += application.events

    orders_settled = 0
    if touched:
        await session.flush()
        orders_settled = await _settle_orders(session, touched)

    return ReResolution(
        lines_filled=lines_filled,
        orders_settled=orders_settled,
        stock_events=stock_events,
        cancelled_skipped=cancelled_skipped,
        unmanaged_skipped=unmanaged_skipped,
        unresolved=unresolved,
        filled_sales_jpy=filled_sales,
        unresolved_sales_jpy=unresolved_sales,
        filled_first_day=min(filled_days) if filled_days else None,
        filled_last_day=max(filled_days) if filled_days else None,
        blank_key_lines=blank_key_lines,
        blank_key_sales_jpy=blank_key_sales,
    )


async def _settle_orders(session: AsyncSession, touched: dict[int, Order]) -> int:
    """Confirm the orders with nothing unmapped left — recomputed, not assumed.

    A partially resolved order must stay `pending_mapping`, or its remaining
    NULL lines drop out of every view that looks for one.
    """
    remaining = await session.execute(
        select(OrderItem.order_id)
        .where(
            OrderItem.order_id.in_(list(touched)),
            OrderItem.master_sku_id.is_(None),
        )
        .distinct(),
    )
    still_unmapped = set(remaining.scalars().all())

    settled = 0
    for order_id, order in touched.items():
        if order_id in still_unmapped:
            continue
        if order.status != OrderStatusEnum.PENDING_MAPPING:
            continue
        order.status = OrderStatusEnum.CONFIRMED
        settled += 1
    if settled:
        await session.flush()
    return settled


__all__ = [
    "AmbiguousChannelSkuError",
    "MappingNotFoundError",
    "MappingService",
    "ReResolution",
    "UnresolvedSku",
    "reresolve_unmapped_lines",
]
