"""WholesaleImportService — turning a read 流通BMS file into stock movement.

docs/29 §4. `app.wholesale_bms` has already decided WHAT the file is; this
decides what it would do to our stock, and then does it.

Two steps on purpose, mirroring every other upload screen: `plan` touches no
events and answers "what would change", `apply` writes. The operator approves a
plan they have seen, which is the whole reason the 照合 flow exists and the
reason a wholesale shipment of 3 units cannot quietly land on the wrong master.

Why this is not `ReconcileService.approve_diff` (docs/29 §8): a 照合 or a
stocktake SETS stock to a counted absolute, and its diff carries
`current_qty`/`target_qty`. Wholesale is a MOVEMENT — 3 units left, 2 came
back — and the arithmetic has to compose with everything else that happened
that day. Routing it through the absolute path would overwrite concurrent
channel orders with a number computed before them.

What this deliberately does NOT do:

* **Guess the cutover.** `cutover_on` has no default. The history the client
  already sent us would add 3,045 phantom units if imported (docs/29 §7-2), and
  the return file genuinely reaches back to 2026-06-30, so every upload carries
  pre-cutover rows that must be dropped. A parameter with a default is a
  parameter someone forgets.
* **Stop a shipment that would go negative.** docs/29 §8-1 assumed this path
  would use `manual_adjust`, which refuses negatives, and said to hold such a
  line. It does not: the goods shipped, and a held line leaves stock wrong in
  the other direction with nothing on the screen to say so. Negative-going lines
  are listed in the plan so the operator sees them BEFORE approving. See
  docs/29 §10-9.
* **Write the sale.** `orders`/`order_items` with 原単価 is docs/29 §6, a
  separate item. It reads the same `PlannedVoucher`, so it adds rather than
  rewrites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models import (
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services.inventory import EventSource, InventoryService, LineApplication
from app.services.mapping import resolve_master_sku_id
from app.services.timeframe import JST
from app.wholesale_bms import BmsKind, BmsLine, BmsReading, BmsVoucher

log = get_logger(__name__)

#: `channel` on both `channel_sku_mappings` and the inventory event source.
#: Matches `ChannelEnum.WHOLESALE`, which has existed unused since Phase 1-A.
WHOLESALE_CHANNEL = "wholesale"

#: Which event each kind appends. A wholesale return is a return OF A SALE, so
#: it belongs in the same bucket the analytics rollup already splits out
#: (`analytics_rollup.day_movement_stmt` sums `cancellation_returned` as the
#: returned column, and migration 0009 indexes it). `receipt` would have hidden
#: every wholesale return from the returns figure and inflated the 入荷 reporting
#: that Phase 2.5 builds on instead.
_EVENT_TYPE = {
    BmsKind.ORDER: InventoryEventTypeEnum.ORDER_CONSUMED,
    BmsKind.RETURN: InventoryEventTypeEnum.CANCELLATION_RETURNED,
}

#: What the `orders` row says happened. `returned` is one of
#: `analytics_rollup.CANCELLED_STATUSES`, so a return voucher's quantity and
#: amount are counted as the sale that did not stick rather than as revenue —
#: which is what lets the price stay positive on both kinds.
_ORDER_STATUS = {
    BmsKind.ORDER: OrderStatusEnum.SHIPPED,
    BmsKind.RETURN: OrderStatusEnum.RETURNED,
}


@dataclass(frozen=True, slots=True)
class PlannedLine:
    """One BMS detail line, resolved against our masters."""

    line: BmsLine
    #: None when the line's `COL_PRODUCT_CODE` resolves to no active
    #: wholesale mapping. The line is reported, never applied, never guessed at.
    master_sku_id: int | None
    sku_code: str | None
    master_name: str | None
    is_stock_managed: bool
    #: Signed: negative for a shipment, positive for a return. This is the sign
    #: that `app.wholesale_bms` exists to get right.
    quantity_delta: int
    current_qty: int | None
    #: What this line alone would leave behind. Not a prediction of the final
    #: number — several lines of one file can touch the same master, and channel
    #: orders keep arriving — so it is shown as the reason a line needs a look,
    #: not as a result.
    projected_qty: int | None
    #: Already on record under this voucher and line number. A re-upload is
    #: harmless (the source key makes it a no-op) but it must not be presented
    #: as an import that is about to do something.
    already_applied: bool

    @property
    def mapped(self) -> bool:
        return self.master_sku_id is not None

    @property
    def goes_negative(self) -> bool:
        return self.projected_qty is not None and self.projected_qty < 0

    @property
    def movable(self) -> bool:
        """Would `apply` write an event for this line?"""
        return self.mapped and self.is_stock_managed and not self.already_applied


@dataclass(frozen=True, slots=True)
class PlannedVoucher:
    voucher: BmsVoucher
    lines: tuple[PlannedLine, ...]

    @property
    def occurred_on(self) -> date:
        return self.voucher.occurred_on

    @property
    def number(self) -> str:
        """取引番号. Spelled out because `voucher.voucher.voucher` is unreadable."""
        return self.voucher.voucher

    @property
    def buyer_code(self) -> str:
        return self.voucher.buyer_code


@dataclass(slots=True)
class WholesalePlan:
    """What applying this file would do, before anything is written."""

    kind: BmsKind
    cutover_on: date
    vouchers: tuple[PlannedVoucher, ...] = ()
    #: Dropped for being dated before the cutover (docs/29 §7-2). Kept rather
    #: than discarded so the screen can say "17伝票は棚卸日より前のため対象外"
    #: instead of silently importing less than the file contains.
    excluded_vouchers: tuple[PlannedVoucher, ...] = ()
    #: `COL_PRODUCT_CODE` -> how many lines carried it, for the codes that
    #: resolve to nothing. Keyed by code because the fix is one mapping row per
    #: code, not one per line.
    unmapped_codes: dict[str, int] = field(default_factory=dict)
    #: The last day a stocktake counted anything at or after the cutover, when
    #: that is not the basis count itself. See `recounted_vouchers`.
    counted_through: date | None = None

    @property
    def all_lines(self) -> list[PlannedLine]:
        return [line for voucher in self.vouchers for line in voucher.lines]

    @property
    def movable_lines(self) -> list[PlannedLine]:
        return [line for line in self.all_lines if line.movable]

    @property
    def negative_lines(self) -> list[PlannedLine]:
        return [line for line in self.movable_lines if line.goes_negative]

    @property
    def unmanaged_lines(self) -> list[PlannedLine]:
        """Mapped, but the master carries no stock — the ギフトバッグ case.

        docs/29 §5-4: 20 shipped against 154 returned. Without the stop this one
        code climbs forever, which is how Phase 1-B's gift boxes reached
        -12,509."""
        return [line for line in self.all_lines if line.mapped and not line.is_stock_managed]

    @property
    def already_applied_lines(self) -> list[PlannedLine]:
        return [line for line in self.all_lines if line.already_applied]

    @property
    def unmapped_lines(self) -> list[PlannedLine]:
        return [line for line in self.all_lines if not line.mapped]

    @property
    def lines_on_cutover(self) -> list[PlannedLine]:
        """Lines dated exactly on the cutover day.

        The boundary is inclusive — the cutover is a 開始日 — but the count was
        taken at some hour of that day and the file carries only a date, so a
        shipment that left before the counting is subtracted twice. Surfaced as
        a number the operator can act on rather than resolved by assumption;
        docs/29 §10-10 carries the question for the client.
        """
        return [
            line
            for voucher in self.vouchers
            for line in voucher.lines
            if voucher.occurred_on == self.cutover_on
        ]

    @property
    def recounted_vouchers(self) -> tuple[PlannedVoucher, ...]:
        """Kept vouchers a later stocktake has already absorbed (docs/29 §10-28).

        A physical count sets stock absolutely, so a voucher dated on or before a
        stocktake that has since been applied is already inside that count, and
        adding it subtracts it twice. The client counts category by category over
        five weekends while wholesale starts from the first, so this is the
        ordinary shape of the risk rather than an exotic one.

        Reported, never dropped. Whether a given line is actually affected
        depends on whether ITS master was in that instalment, and a finalised run
        keeps only the SKUs that differed, so the data cannot say (docs/29
        §10-22). Filtering on a guess would lose real movements; saying the number
        out loud lets the operator import before the next counting weekend, which
        is the mitigation the spec asks for.
        """
        if self.counted_through is None:
            return ()
        return tuple(
            voucher for voucher in self.vouchers if voucher.occurred_on <= self.counted_through
        )

    @property
    def total_delta(self) -> int:
        return sum(line.quantity_delta for line in self.movable_lines)

    def as_dict(self) -> dict[str, Any]:
        """Template-friendly shape for the 検証 screen."""
        return {
            "kind": str(self.kind),
            "cutover_on": self.cutover_on.isoformat(),
            "counted_through": (self.counted_through.isoformat() if self.counted_through else None),
            "recounted_line_count": sum(len(v.lines) for v in self.recounted_vouchers),
            "voucher_count": len(self.vouchers),
            "excluded_voucher_count": len(self.excluded_vouchers),
            "excluded_line_count": sum(len(v.lines) for v in self.excluded_vouchers),
            "movable_line_count": len(self.movable_lines),
            "already_applied_line_count": len(self.already_applied_lines),
            "unmapped_line_count": len(self.unmapped_lines),
            "unmapped_codes": dict(self.unmapped_codes),
            "unmanaged_line_count": len(self.unmanaged_lines),
            "negative_line_count": len(self.negative_lines),
            "lines_on_cutover_count": len(self.lines_on_cutover),
            "total_delta": self.total_delta,
        }


@dataclass(slots=True)
class WholesaleApplyResult:
    #: `orders` rows created. One per voucher, and zero on a re-run.
    orders_written: int = 0
    #: `order_items` rows created — every line of an in-scope voucher, mapped or
    #: not, because the sale happened either way (docs/29 §6).
    order_lines_written: int = 0
    #: Existing order_items that had no master and now have one. A line that was
    #: unmapped at first import would otherwise stay invisible to the sales
    #: rollup for ever, since that query skips a NULL master_sku_id.
    order_lines_repaired: int = 0
    applied_lines: int = 0
    #: Lines whose event already existed when `apply` reached them. Distinct
    #: from the plan's `already_applied_lines`, which was measured earlier: a
    #: non-zero count here means something imported the same file in between.
    duplicate_lines: int = 0
    unmanaged_lines: int = 0
    events_written: int = 0
    total_delta: int = 0


class WholesaleImportService:
    """Plans and applies one wholesale file. Never commits; the caller owns the
    transaction, so one upload is all-or-nothing."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._inventory = InventoryService(session)

    # ---------- planning ----------

    async def plan(
        self,
        reading: BmsReading,
        *,
        cutover_on: date,
        counted_through: date | None = None,
    ) -> WholesalePlan:
        """Resolve every line and work out what applying the file would do.

        `reading` must be one `read_bms` returned with no fatal entries; a file
        whose kind is in doubt has no plan, because the sign of every number
        below depends on it.

        `counted_through` only annotates the plan — see
        `WholesalePlan.recounted_vouchers` for why it cannot filter.
        """
        if reading.kind is None or reading.fatal:
            raise ValueError("cannot plan an import from a file that did not read cleanly")

        resolved = await self._resolve_codes(reading)
        masters = await self._load_masters(resolved)
        stock = await self._load_stock(masters)
        applied = await self._already_applied(reading)

        kept: list[PlannedVoucher] = []
        excluded: list[PlannedVoucher] = []
        unmapped: dict[str, int] = {}
        # Running projection per master, so the second line of a voucher that
        # touches the same SKU does not claim the same stock as the first.
        running = dict(stock)

        for voucher in reading.vouchers:
            planned_lines = tuple(
                self._plan_line(
                    line,
                    kind=reading.kind,
                    master_sku_id=resolved.get((line.product_code, voucher.buyer_code)),
                    masters=masters,
                    running=running,
                    applied=applied,
                )
                for line in voucher.lines
            )
            planned = PlannedVoucher(voucher=voucher, lines=planned_lines)
            if voucher.occurred_on < cutover_on:
                excluded.append(planned)
                continue
            kept.append(planned)
            for planned_line in planned_lines:
                if not planned_line.mapped:
                    unmapped[planned_line.line.product_code] = (
                        unmapped.get(planned_line.line.product_code, 0) + 1
                    )

        plan = WholesalePlan(
            kind=reading.kind,
            cutover_on=cutover_on,
            vouchers=tuple(kept),
            excluded_vouchers=tuple(excluded),
            unmapped_codes=unmapped,
            counted_through=counted_through,
        )
        log.info(
            "wholesale.planned",
            kind=str(reading.kind),
            cutover_on=cutover_on.isoformat(),
            **{
                key: value
                for key, value in plan.as_dict().items()
                if key not in ("kind", "cutover_on", "unmapped_codes")
            },
        )
        return plan

    def _plan_line(
        self,
        line: BmsLine,
        *,
        kind: BmsKind,
        master_sku_id: int | None,
        masters: dict[int, MasterSku],
        running: dict[int, int],
        applied: set[tuple[str, str]],
    ) -> PlannedLine:
        sign = -1 if kind is BmsKind.ORDER else 1
        delta = sign * line.quantity
        already = (line.voucher, line.voucher_line) in applied

        if master_sku_id is None:
            return PlannedLine(
                line=line,
                master_sku_id=None,
                sku_code=None,
                master_name=None,
                is_stock_managed=False,
                quantity_delta=delta,
                current_qty=None,
                projected_qty=None,
                already_applied=already,
            )

        master = masters[master_sku_id]
        if not master.is_stock_managed:
            # No projection for a master that holds no stock: a number here
            # would invite someone to reconcile it.
            return PlannedLine(
                line=line,
                master_sku_id=master_sku_id,
                sku_code=master.sku_code,
                master_name=master.name,
                is_stock_managed=False,
                quantity_delta=delta,
                current_qty=None,
                projected_qty=None,
                already_applied=already,
            )

        current = running.get(master_sku_id, 0)
        projected = current if already else current + delta
        running[master_sku_id] = projected
        return PlannedLine(
            line=line,
            master_sku_id=master_sku_id,
            sku_code=master.sku_code,
            master_name=master.name,
            is_stock_managed=True,
            quantity_delta=delta,
            current_qty=current,
            projected_qty=projected,
            already_applied=already,
        )

    # ---------- applying ----------

    async def apply(self, plan: WholesalePlan, *, operator: str) -> WholesaleApplyResult:
        """Write the movement for every line the plan found movable.

        Each line is idempotent on (`wholesale`, 取引番号, 取引明細番号), so
        re-running this over the same file writes nothing the second time —
        the protection docs/29 §7-3 is about, and the reason a damaged 取引番号
        stops the file rather than one row of it.
        """
        result = WholesaleApplyResult()
        for voucher in plan.vouchers:
            # The sale is recorded for EVERY line of an in-scope voucher, including
            # the unmapped ones and the gift bag: the money changed hands whether
            # or not we can say which master it belongs to, and an order_item with
            # a NULL master keeps the line recoverable once the mapping lands.
            # Stock moves only for the lines below. docs/29 §6.
            created_order, created_lines, repaired = await self._record_sale(voucher, plan.kind)
            result.orders_written += int(created_order)
            result.order_lines_written += created_lines
            result.order_lines_repaired += repaired

            for line in voucher.lines:
                if not line.mapped or line.already_applied:
                    continue
                application = await self._move(line, plan.kind, voucher.occurred_on)
                if application.unmanaged:
                    result.unmanaged_lines += 1
                elif not application.applied:
                    result.duplicate_lines += 1
                else:
                    result.applied_lines += 1
                    result.events_written += application.events
                    result.total_delta += line.quantity_delta

        log.info(
            "wholesale.applied",
            kind=str(plan.kind),
            operator=operator,
            applied_lines=result.applied_lines,
            duplicate_lines=result.duplicate_lines,
            unmanaged_lines=result.unmanaged_lines,
            events_written=result.events_written,
            total_delta=result.total_delta,
        )
        return result

    async def _record_sale(
        self,
        voucher: PlannedVoucher,
        kind: BmsKind,
    ) -> tuple[bool, int, int]:
        """One voucher as one `orders` row and its `order_items` (docs/29 §6).

        Returns (order created, items created, items repaired).

        `unit_price` is **原単価** — what we charge them, which is our revenue.
        売単価 on the same row is the shop's retail price and is not ours: over the
        335 measured shipment lines it books ¥6,049,918 against 原単価's
        ¥3,389,736, a 78% overstatement of wholesale turnover.

        The carrier agrees with that arithmetic. Their own `原価金額` column sums
        to exactly ¥3,389,736 over the same lines, which is 原単価 × 出荷数量 —
        so the formula here is confirmed by the file rather than assumed from it.
        `test_the_unit_price_matches_the_carriers_own_amount_column` holds that.

        A return voucher is recorded with status `returned`, which
        `analytics_rollup.CANCELLED_STATUSES` already treats as "the sale did not
        stick": its quantity and amount land in the returned columns instead of
        net sales. That is why a return needs no negative price — the status
        carries the sign, and inverting the amount as well would subtract it
        twice.

        The status is set from the file's kind alone, never `pending_mapping`,
        even when lines are unmapped. That status means an operator has a mapping
        alert to work through, and wholesale raises none — the review step on the
        correspondence table is where an unmapped code is dealt with. An order
        parked as pending with no alert behind it is a screen that lies.
        """
        existing = await self._session.execute(
            select(Order).where(
                Order.channel == WHOLESALE_CHANNEL,
                Order.channel_order_id == voucher.number,
            )
        )
        order = existing.scalar_one_or_none()
        created_order = order is None
        if order is None:
            order = Order(
                channel=WHOLESALE_CHANNEL,
                channel_order_id=voucher.number,
                marketplace_id=voucher.buyer_code,
                status=_ORDER_STATUS[kind],
                ordered_at=_as_jst_midnight(voucher.occurred_on),
                # docs/29 §6: 納品先 is out of scope for reporting, but throwing
                # it away makes per-store sales unanswerable for ever. Nine
                # stores, and the file is the only place they appear.
                raw_payload={
                    "kind": str(kind),
                    "取引番号": voucher.number,
                    "発注者コード": voucher.buyer_code,
                    "最終納品先コード": voucher.voucher.destination_code,
                    "最終納品先名称": voucher.voucher.destination_name,
                    "作成日時": voucher.voucher.created_at_raw,
                },
            )
            self._session.add(order)
            await self._session.flush()

        known = {
            item.line_id: item
            for item in (
                await self._session.execute(select(OrderItem).where(OrderItem.order_id == order.id))
            )
            .scalars()
            .all()
        }

        created = 0
        repaired = 0
        for line in voucher.lines:
            item = known.get(line.line.voucher_line)
            if item is None:
                self._session.add(
                    OrderItem(
                        order_id=order.id,
                        line_id=line.line.voucher_line,
                        channel_sku=line.line.product_code,
                        master_sku_id=line.master_sku_id,
                        quantity=line.line.quantity,
                        unit_price=line.line.unit_cost,
                        currency="JPY",
                    )
                )
                created += 1
                continue
            if item.master_sku_id is None and line.master_sku_id is not None:
                # The code was unmapped when this line was first imported and is
                # mapped now. Filling it in here rather than leaving it to
                # `reresolve_order_items`, because that CLI deliberately does not
                # move stock, while this path has just moved it.
                item.master_sku_id = line.master_sku_id
                repaired += 1
        if created or repaired:
            await self._session.flush()
        return created_order, created, repaired

    async def _move(
        self,
        line: PlannedLine,
        kind: BmsKind,
        occurred_on: date,
    ) -> LineApplication:
        """Apply ONE planned line, in the direction its file's kind decided.

        The two calls are not interchangeable and not symmetric:
        `consume_order_line` permits the result to go negative (the goods left),
        while `credit_unlinked_return` is the only caller allowed to credit stock
        with no matching consumption on record — see its docstring for why
        wholesale is that case and channel orders are not.
        """
        assert line.master_sku_id is not None  # narrowed by the caller's `mapped`
        source = EventSource(
            channel=WHOLESALE_CHANNEL,
            order_id=line.line.voucher,
            line_id=line.line.voucher_line,
        )
        move = (
            self._inventory.consume_order_line
            if kind is BmsKind.ORDER
            else self._inventory.credit_unlinked_return
        )
        return await move(
            master_sku_id=line.master_sku_id,
            quantity=line.line.quantity,
            source=source,
            occurred_at=_as_jst_midnight(occurred_on),
        )

    # ---------- lookups ----------

    async def _resolve_codes(self, reading: BmsReading) -> dict[tuple[str, str], int | None]:
        """`COL_PRODUCT_CODE` -> master, resolved once per DISTINCT code.

        Per code, not per line: the 2026-09-16 返品 file holds 1,887 lines over
        77 codes, and resolving per line would be 1,810 identical queries.

        Goes through `resolve_master_sku_id` rather than querying
        `channel_sku_mappings` here, as `ChannelSkuMapping` asks. Wholesale rows
        carry no `channel_variant_key`, so the second step of that resolver never
        fires — but the rule is what keeps an ad-hoc lookup from being copied
        somewhere a page-level row would make it raise.
        """
        pairs = sorted(
            {
                (line.product_code, voucher.buyer_code)
                for voucher in reading.vouchers
                for line in voucher.lines
            }
        )
        resolved: dict[tuple[str, str], int | None] = {}
        for code, buyer in pairs:
            resolved[(code, buyer)] = await resolve_master_sku_id(
                self._session,
                channel=WHOLESALE_CHANNEL,
                channel_sku=code,
                marketplace_id=buyer,
            )
        buyer_codes = {buyer for _, buyer in pairs}
        if len(buyer_codes) > 1:
            # Every one of the 2,222 rows measured carried 0393, the one
            # wholesale customer we have (docs/29 §1). A second 発注者コード is
            # not an error, but it is the `marketplace_id` mappings are keyed on,
            # so it decides which master a code resolves to.
            log.info("wholesale.multiple_buyer_codes", buyer_codes=sorted(buyer_codes))
        return resolved

    async def _load_masters(
        self, resolved: dict[tuple[str, str], int | None]
    ) -> dict[int, MasterSku]:
        ids = {master_id for master_id in resolved.values() if master_id is not None}
        if not ids:
            return {}
        rows = await self._session.execute(select(MasterSku).where(MasterSku.id.in_(ids)))
        return {master.id: master for master in rows.scalars().all()}

    async def _load_stock(self, masters: dict[int, MasterSku]) -> dict[int, int]:
        if not masters:
            return {}
        rows = await self._session.execute(
            select(InventorySnapshot.master_sku_id, InventorySnapshot.on_hand_qty).where(
                InventorySnapshot.master_sku_id.in_(masters)
            )
        )
        return dict(rows.tuples().all())

    async def _already_applied(self, reading: BmsReading) -> set[tuple[str, str]]:
        """(取引番号, 取引明細番号) pairs this kind has already written events for.

        Scoped to the kind's own event type. The two kinds' voucher numbers have
        never collided, but the event type is part of `uq_inventory_event_source`
        and reading across both would call a shipment's line an
        already-imported return.
        """
        if reading.kind is None or not reading.vouchers:
            return set()
        vouchers = [voucher.voucher for voucher in reading.vouchers]
        rows = await self._session.execute(
            select(InventoryEvent.source_order_id, InventoryEvent.source_line_id)
            .where(
                InventoryEvent.event_type == _EVENT_TYPE[reading.kind],
                InventoryEvent.source_channel == WHOLESALE_CHANNEL,
                InventoryEvent.source_order_id.in_(vouchers),
            )
            .distinct()
        )
        return {
            (order_id, line_id)
            for order_id, line_id in rows.all()
            if order_id is not None and line_id is not None
        }


def _as_jst_midnight(on: date) -> datetime:
    """The BMS files carry a date and no time, so pick the time deliberately.

    00:00 JST, not 00:00 UTC. A JST day is the UTC interval
    [15:00 previous day, 15:00) — see `app.services.timeframe` — so stamping a
    JST calendar date at UTC midnight files that movement under the PREVIOUS
    JST day in every daily aggregate. Nine hours of error on a date that came
    with no time at all.
    """
    return datetime(on.year, on.month, on.day, tzinfo=JST)


__all__ = [
    "WHOLESALE_CHANNEL",
    "PlannedLine",
    "PlannedVoucher",
    "WholesaleApplyResult",
    "WholesaleImportService",
    "WholesalePlan",
]
