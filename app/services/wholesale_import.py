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
from datetime import UTC, date, datetime
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
from app.services.inventory import (
    EventSource,
    InventoryService,
    LineApplication,
    reversal_source_order_id,
    strip_reversal_prefix,
)
from app.services.mapping import resolve_master_sku_id
from app.services.timeframe import JST
from app.wholesale_bms import RULES, BmsKind, BmsLine, BmsReading, BmsVoucher

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
class AppliedLedger:
    """What `inventory_events` already holds for one file's vouchers.

    Two sets rather than one, because "this line has an event" and "this line
    counts as imported" stopped being the same question once an import could be
    withdrawn. A withdrawn line keeps its original event for ever — `reverse`
    compensates rather than deletes — so it belongs in `withdrawn`, which an
    import treats as importable again and a withdrawal treats as already done.
    """

    #: (取引番号, 取引明細番号) the import wrote an event for. Stays true
    #: after a withdrawal: the event is still there, and so is the identity
    #: `uq_inventory_event_source` will not let a second import reuse.
    applied: frozenset[tuple[str, str]] = frozenset()
    #: A subset of `applied`: those whose events have been compensated.
    withdrawn: frozenset[tuple[str, str]] = frozenset()

    def lines_of(self, voucher: str) -> int:
        """How many of that 取引番号's lines the ledger has ever moved.

        Counts withdrawn lines too: they are still evidence that the import
        reached that line, which is what makes this comparable against the
        number of lines the file in hand carries.
        """
        return sum(1 for number, _ in self.applied | self.withdrawn if number == voucher)


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
    #: Imported and then withdrawn. Implies `already_applied`, which stays
    #: true because the event is still on record — this says it has been
    #: compensated. Kept apart from "never imported" so a withdrawal reports
    #: its own no-op instead of claiming there was nothing there, and so an
    #: import can refuse rather than silently write nothing.
    already_withdrawn: bool = False

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


def _kind_label(value: str) -> str:
    """`BmsKind` value -> 受注 / 返品, tolerant of a stored value we do not know."""
    try:
        return RULES[BmsKind(value)].label
    except ValueError:
        return value or "(不明)"


@dataclass(frozen=True, slots=True)
class VoucherConflict:
    """A 取引番号 this file claims that the OTHER kind already imported.

    `orders` is UNIQUE on (channel, channel_order_id), and 取引番号（発注・返品）
    is ONE column serving two numbering series, so a number used by both kinds
    cannot have two rows. Left alone, `_record_sale` finds the first kind's
    order, attaches this file's lines to it, and silently drops every line whose
    取引明細番号 the first kind already used.

    What makes it worth stopping the whole file for is that the stock events are
    keyed by event type as well, so THEY stay correct — the import looks right
    from every direction except the sales figures, and nothing reconciles to
    reveal it.

    Never observed: the 21 shipment and 79 return vouchers measured 2026-09-16
    share no number. Nothing in the format promises they never will.
    """

    voucher: str
    #: `BmsKind` value already on record for this number.
    recorded_kind: str
    #: `BmsKind` value of the file being imported now.
    file_kind: str

    @property
    def message(self) -> str:
        return (
            f"取引番号 {self.voucher} は既に{_kind_label(self.recorded_kind)}として"
            f"取り込まれています（今回のファイルは{_kind_label(self.file_kind)}）。"
            "同じ取引番号が両方の種別で使われると、後の明細が先の伝票に吸い込まれ、"
            "明細番号が重なる行は記録されません。"
            "在庫だけは正しく動くため、取り込んだ後では気づけません。"
        )


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
    #: Inclusive upper bound on the voucher date. None for an import, which
    #: always runs to the end of the file; set only by a WITHDRAWAL that is
    #: correcting a cutover — see `through_on` on `plan`.
    through_on: date | None = None
    #: Dropped for being dated after `through_on`. Deliberately out of scope,
    #: unlike `excluded_vouchers`, so the partial-withdrawal guards must not
    #: treat an applied line here as stranded.
    beyond_vouchers: tuple[PlannedVoucher, ...] = ()
    #: `COL_PRODUCT_CODE` -> how many lines carried it, for the codes that
    #: resolve to nothing. Keyed by code because the fix is one mapping row per
    #: code, not one per line.
    unmapped_codes: dict[str, int] = field(default_factory=dict)
    #: The last day a stocktake counted anything at or after the cutover, when
    #: that is not the basis count itself. See `recounted_vouchers`.
    counted_through: date | None = None
    #: Kept vouchers whose 取引番号 the other kind already imported. Blocks the
    #: whole file rather than the voucher: see `VoucherConflict`.
    voucher_conflicts: tuple[VoucherConflict, ...] = ()
    #: What the stock ledger already holds for these vouchers. Carried on the
    #: plan because a withdrawal has to ask it questions an import does not.
    ledger: AppliedLedger = field(default_factory=AppliedLedger)
    #: 取引番号 in this file that already have an `orders` row — the only
    #: evidence that a voucher whose every line was unmapped or 在庫管理対象外
    #: was nonetheless imported.
    vouchers_with_orders: frozenset[str] = frozenset()

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
    def stranded_applied_lines(self) -> list[PlannedLine]:
        """Lines a past import moved that THIS plan's cutover puts out of scope.

        Only a reversal cares. A withdrawal is anchored on the file (see
        `reverse`), so the cutover decides which vouchers it names — and if that
        cutover is not the one the import used, the vouchers dated in between
        were applied then but are excluded now. Reversing would undo part of the
        import and report success, which is worse than refusing: stock is left
        half corrected and the result reads clean.

        Non-zero is therefore the precise signal that the cutover does not match
        the import being withdrawn. It cannot fire on an ordinary file: an
        excluded voucher was excluded at import time too, so nothing was applied
        for it. Lines are resolved before the cutover filter in `plan`, which is
        what makes this measurable at all.
        """
        return [
            line
            for voucher in self.excluded_vouchers
            for line in voucher.lines
            if line.already_applied
        ]

    @property
    def stranded_vouchers(self) -> list[PlannedVoucher]:
        """Excluded vouchers a past import nonetheless recorded.

        The same signal as `stranded_applied_lines`, asked of the SALE instead
        of the stock. It has to be asked separately because `_record_sale` books
        every in-scope line while `_move` moves only the mapped, stock-managed
        ones: a voucher whose lines were all unmapped or all 在庫管理対象外 is
        imported without producing one inventory event, so an event-derived
        guard calls it untouched. A withdrawal under a cutover that excludes it
        would then leave its revenue booked and say nothing.

        The gift bag alone makes this reachable rather than theoretical — a
        voucher can legitimately consist of nothing else (docs/29 §5-4).
        """
        return [
            voucher
            for voucher in self.excluded_vouchers
            if voucher.number in self.vouchers_with_orders
            or any(line.already_applied for line in voucher.lines)
        ]

    @property
    def truncated_vouchers(self) -> list[PlannedVoucher]:
        """In-scope vouchers the ledger has moved more lines of than this file holds.

        The third way a withdrawal goes partial, and the one the file anchor
        cannot see by itself: if the carrier re-sent a voucher with rows dropped,
        or the operator is holding a different copy, the missing lines keep their
        movement and the withdrawal reports only what it was shown. Comparing
        the ledger's line count per 取引番号 against the file's catches it
        without needing a record of which file the import read.
        """
        return [
            voucher
            for voucher in self.vouchers
            if self.ledger.lines_of(voucher.number) > len(voucher.lines)
        ]

    @property
    def withdrawn_lines(self) -> list[PlannedLine]:
        """In-scope lines a past withdrawal already put back.

        What makes a withdrawal's preview agree with its result. Without it,
        `already_applied` would be the only thing a preview could count, and
        since a reversal does not delete the import's events a dry run after a
        completed withdrawal would show the whole withdrawal still pending.
        """
        return [line for line in self.all_lines if line.already_withdrawn]

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
    def blocked(self) -> bool:
        """May this plan be applied at all?"""
        return bool(self.voucher_conflicts)

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
            "through_on": self.through_on.isoformat() if self.through_on else None,
            "beyond_voucher_count": len(self.beyond_vouchers),
            "beyond_line_count": sum(len(v.lines) for v in self.beyond_vouchers),
            "movable_line_count": len(self.movable_lines),
            "already_applied_line_count": len(self.already_applied_lines),
            "stranded_applied_line_count": len(self.stranded_applied_lines),
            "stranded_voucher_count": len(self.stranded_vouchers),
            "truncated_voucher_count": len(self.truncated_vouchers),
            "withdrawn_line_count": len(self.withdrawn_lines),
            "unmapped_line_count": len(self.unmapped_lines),
            "unmapped_codes": dict(self.unmapped_codes),
            "unmanaged_line_count": len(self.unmanaged_lines),
            "negative_line_count": len(self.negative_lines),
            "lines_on_cutover_count": len(self.lines_on_cutover),
            "total_delta": self.total_delta,
            "blocked": self.blocked,
            "voucher_conflicts": [c.message for c in self.voucher_conflicts],
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


@dataclass(frozen=False, slots=True)
class WholesaleReverseResult:
    """What `reverse` undid. Mirrors `WholesaleApplyResult` field for field
    where the two have the same meaning, so the screen can show a withdrawal
    the same way it shows an import."""

    #: Vouchers taken out of the sales figures by THIS call. Zero on a re-run,
    #: because a voucher already CANCELLED is not counted again.
    orders_cancelled: int = 0
    reversed_lines: int = 0
    #: Lines whose reversal event already existed — the idempotent no-op.
    already_reversed_lines: int = 0
    #: Lines the import never moved stock for, so there is nothing to undo —
    #: unmapped when the file was first imported, or the gift bag. Counted
    #: rather than ignored: a large number here means the file or the cutover
    #: does not match the import being withdrawn.
    never_applied_lines: int = 0
    #: Lines the plan called applied but whose events could not be found. The
    #: guards and `_already_applied` make this unreachable; it exists so that if
    #: it ever does happen it cannot hide inside `already_reversed_lines`, which
    #: is the field that tells an operator there is nothing left to do.
    unreconciled_lines: int = 0
    events_written: int = 0
    #: The movement written, as a delta. Opposite in sign to the import's
    #: `total_delta`, which is what makes the pair auditable at a glance.
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
        through_on: date | None = None,
    ) -> WholesalePlan:
        """Resolve every line and work out what applying the file would do.

        `reading` must be one `read_bms` returned with no fatal entries; a file
        whose kind is in doubt has no plan, because the sign of every number
        below depends on it.

        `counted_through` only annotates the plan — see
        `WholesalePlan.recounted_vouchers` for why it cannot filter.

        `through_on` closes the scope at the top, inclusive. An import never
        passes it: a file is imported to its end. It exists for the one
        operation that needs a window rather than a tail — correcting a cutover
        that was set too early, where what has to be undone is the vouchers
        between the wrong cutover and the right one, and the ones after the
        right one must be left exactly as they are. Withdrawing the whole file
        and importing it again is NOT an alternative: `reverse` compensates the
        import's events rather than deleting them, so their
        `uq_inventory_event_source` identity stays taken and a second import of
        the same line cannot be written.
        """
        if reading.kind is None or reading.fatal:
            raise ValueError("cannot plan an import from a file that did not read cleanly")

        resolved = await self._resolve_codes(reading)
        masters = await self._load_masters(resolved)
        stock = await self._load_stock(masters)
        ledger = await self._already_applied(reading)
        with_orders = await self._vouchers_with_orders(reading)

        kept: list[PlannedVoucher] = []
        excluded: list[PlannedVoucher] = []
        beyond: list[PlannedVoucher] = []
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
                    ledger=ledger,
                )
                for line in voucher.lines
            )
            planned = PlannedVoucher(voucher=voucher, lines=planned_lines)
            if voucher.occurred_on < cutover_on:
                excluded.append(planned)
                continue
            if through_on is not None and voucher.occurred_on > through_on:
                beyond.append(planned)
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
            through_on=through_on,
            beyond_vouchers=tuple(beyond),
            unmapped_codes=unmapped,
            counted_through=counted_through,
            # Only the KEPT vouchers: an excluded one writes no sale, so its
            # number cannot collide with anything.
            voucher_conflicts=await self._voucher_conflicts(kept, reading.kind),
            ledger=ledger,
            vouchers_with_orders=with_orders,
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
        ledger: AppliedLedger,
    ) -> PlannedLine:
        sign = -1 if kind is BmsKind.ORDER else 1
        delta = sign * line.quantity
        key = (line.voucher, line.voucher_line)
        already = key in ledger.applied
        withdrawn = key in ledger.withdrawn

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
                already_withdrawn=withdrawn,
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
                already_withdrawn=withdrawn,
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
            already_withdrawn=withdrawn,
        )

    # ---------- applying ----------

    async def apply(self, plan: WholesalePlan, *, operator: str) -> WholesaleApplyResult:
        """Write the movement for every line the plan found movable.

        Each line is idempotent on (`wholesale`, 取引番号, 取引明細番号), so
        re-running this over the same file writes nothing the second time —
        the protection docs/29 §7-3 is about, and the reason a damaged 取引番号
        stops the file rather than one row of it.

        Refuses a blocked plan outright. The screen and the CLI both stop
        before offering one, so reaching here means a caller built a plan and
        ignored what it said — which is when a backstop earns its keep,
        because the damage is invisible afterwards.

        Refuses a WITHDRAWN line for the same reason, and it is the sharper of
        the two because the failure is quiet. `reverse` compensates the import's
        events instead of deleting them, so the original
        `uq_inventory_event_source` identity is still taken: `_move` would hit
        the unique index, `_append_sourced_event` would return None, and the
        line would be counted as a duplicate — leaving a caller to print
        「適用しました: 在庫 0明細」over a file it did not import. There is no
        second identity to write it under, so the honest answer is to stop and
        say so; correcting a cutover is done by withdrawing the window that was
        wrong (`plan`'s `through_on`), never by withdrawing everything and
        importing again.
        """
        if plan.blocked:
            raise ValueError(
                "cannot apply a plan whose vouchers collide with an imported kind: "
                + " / ".join(c.voucher for c in plan.voucher_conflicts)
            )
        if plan.withdrawn_lines:
            raise ValueError(
                f"cannot apply: {len(plan.withdrawn_lines)} lines were imported and "
                "then withdrawn, and a withdrawn line cannot be imported again"
            )
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

    async def reverse(self, plan: WholesalePlan, *, operator: str) -> WholesaleReverseResult:
        """Undo what `apply` wrote for this same file and cutover.

        Nothing records which vouchers a past import touched, so a reversal is
        anchored on the file instead: re-read the same file with the same
        cutover and the plan names exactly the vouchers that were applied. That
        symmetry is the design — a reversal cannot drift from the import it
        undoes, because the same code builds it from the same input.

        WHY THIS EXISTS

        The cutover is set once, when the first file is imported, and docs/29
        §7-2 is why it matters: importing against a cutover earlier than the
        real count day replays movement the count already includes, and the
        measured cost of getting it wrong on this data is 3,045 units of
        inflated stock. Until now that was unrecoverable — wholesale was the
        only import flow with no cancel route, unlike reconcile and stocktake.

        WHAT IT TOUCHES

        * Stock, through `reverse_order_line`, which negates the events the
          import wrote — on the masters it wrote them to, at the deltas it used
          — and writes under a `REV:` source of its own, so neither kind's
          identity is displaced. It takes no master and no quantity from this
          plan on purpose; see that method for the three ways today's
          configuration can disagree with what the import actually did.
        * Revenue, by setting the order to CANCELLED. `analytics_rollup`'s
          CANCELLED_STATUSES is ("cancelled", "returned"), so this takes the
          voucher out of the sales figures without deleting the record. The
          order and its lines are kept: they are the evidence of what was
          imported, and a reversal is not a reason to lose it.

        Idempotent throughout, so running it twice is a no-op rather than a
        double correction. And reversible in turn: `_already_applied` subtracts
        the withdrawals, so a withdrawn file can be imported again under the
        corrected cutover, which is the whole point of having this.

        REFUSES ANY WITHDRAWAL THAT COULD ONLY BE PARTIAL

        Undoing some of an import while reporting success is worse than refusing
        it: the stock figure is left half corrected and nothing says so, so
        nobody looks. Three shapes are refused, each with its own fix:

        * `stranded_applied_lines` / `stranded_vouchers` — the cutover is not
          the one the import used, so this plan does not name everything that
          import touched. Re-run with the import's own `--cutover`.
        * `truncated_vouchers` — the ledger has moved more lines of a voucher
          than the file in hand carries. Find the file the import read.

        Callers check the properties first so the operator sees a sentence
        rather than a traceback; the guards here are so no caller can forget.
        """
        if plan.stranded_applied_lines or plan.stranded_vouchers:
            raise ValueError(
                "cannot reverse: this cutover excludes "
                f"{len(plan.stranded_applied_lines)} applied lines and "
                f"{len(plan.stranded_vouchers)} recorded vouchers, "
                "so the withdrawal would be partial"
            )
        if plan.truncated_vouchers:
            raise ValueError(
                f"cannot reverse: {len(plan.truncated_vouchers)} vouchers hold more "
                "imported lines than this file carries, so the withdrawal would be "
                "partial"
            )
        result = WholesaleReverseResult()
        restores_stock = plan.kind is BmsKind.ORDER
        for voucher in plan.vouchers:
            for line in voucher.lines:
                if line.already_withdrawn:
                    result.already_reversed_lines += 1
                    continue
                if not line.already_applied:
                    # The import never moved stock for this line — unmapped at
                    # the time, or the gift bag. Compensating it would invent
                    # stock that was never taken. Note this is NOT gated on
                    # `mapped`: whether the code resolves today has no bearing
                    # on what the import wrote, and the events carry their own
                    # master.
                    result.never_applied_lines += 1
                    continue
                reversal = await self._inventory.reverse_order_line(
                    source=EventSource(
                        channel=WHOLESALE_CHANNEL,
                        order_id=line.line.voucher,
                        line_id=line.line.voucher_line,
                    ),
                    restores_stock=restores_stock,
                    # Reversal time, never the voucher's date: a backdated event
                    # lands in a period the forward-running daily rollups have
                    # already summed and will never revisit.
                    occurred_at=datetime.now(UTC),
                )
                if not reversal.found:
                    # The ledger disagrees with `already_applied`, which was
                    # measured in the same transaction — so this is unreachable
                    # rather than merely unlikely. Counted as its own thing
                    # anyway: the one label it must never borrow is the
                    # idempotent no-op's.
                    result.unreconciled_lines += 1
                elif not reversal.applied:
                    result.already_reversed_lines += 1
                else:
                    result.reversed_lines += 1
                    result.events_written += reversal.written
                    result.total_delta -= line.quantity_delta

            cancelled = await self._withdraw_sale(voucher, operator)
            result.orders_cancelled += int(cancelled)

        log.info(
            "wholesale.reversed",
            kind=str(plan.kind),
            operator=operator,
            orders_cancelled=result.orders_cancelled,
            reversed_lines=result.reversed_lines,
            already_reversed_lines=result.already_reversed_lines,
            never_applied_lines=result.never_applied_lines,
            unreconciled_lines=result.unreconciled_lines,
            events_written=result.events_written,
            total_delta=result.total_delta,
        )
        return result

    async def _withdraw_sale(self, voucher: PlannedVoucher, operator: str) -> bool:
        """Take one voucher out of the sales figures, keeping the record.

        Returns whether this call changed the status, so re-running reports 0
        rather than counting the same voucher again.
        """
        order = (
            await self._session.execute(
                select(Order).where(
                    Order.channel == WHOLESALE_CHANNEL,
                    Order.channel_order_id == voucher.number,
                )
            )
        ).scalar_one_or_none()
        if order is None or order.status == OrderStatusEnum.CANCELLED.value:
            return False
        order.status = OrderStatusEnum.CANCELLED.value
        # A new dict, so SQLAlchemy sees the JSONB as dirty. Mutating in place
        # would not flush.
        order.raw_payload = {
            **(order.raw_payload or {}),
            "reversed_at": datetime.now(UTC).isoformat(),
            "reversed_by": operator,
        }
        await self._session.flush()
        return True

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

        A withdrawn voucher is never seen here: `apply` refuses a plan holding
        one before any of this runs, because its stock cannot be written a
        second time. So there is deliberately no code to lift a `reversed_at`
        stamp — restoring the revenue of a voucher whose stock cannot come back
        would be the inconsistent half of a recovery that is not available.
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

    async def _voucher_conflicts(
        self,
        vouchers: list[PlannedVoucher],
        kind: BmsKind,
    ) -> tuple[VoucherConflict, ...]:
        """Which of these 取引番号 are already on record as the other kind.

        Read from `orders.raw_payload["kind"]`, which `_record_sale` has always
        written, rather than from the status: `returned` is reachable on a
        shipment voucher by other routes, and the kind is what decides the sign.

        A row whose payload carries no kind at all counts as a conflict. This is
        the only writer of wholesale orders, so a missing kind is not a legacy
        shape to be tolerated — and of the two directions, refusing is the one
        whose mistake is visible.
        """
        numbers = [voucher.number for voucher in vouchers]
        if not numbers:
            return ()
        rows = await self._session.execute(
            select(Order.channel_order_id, Order.raw_payload).where(
                Order.channel == WHOLESALE_CHANNEL,
                Order.channel_order_id.in_(numbers),
            )
        )
        conflicts = tuple(
            VoucherConflict(
                voucher=number,
                recorded_kind=str((payload or {}).get("kind") or ""),
                file_kind=str(kind),
            )
            for number, payload in rows.all()
            if (payload or {}).get("kind") != str(kind)
        )
        if conflicts:
            log.warning(
                "wholesale.voucher_kind_conflict",
                kind=str(kind),
                vouchers=[c.voucher for c in conflicts],
            )
        return conflicts

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

    async def _already_applied(self, reading: BmsReading) -> AppliedLedger:
        """What the ledger already holds for this file's vouchers.

        Scoped to the kind's own event type. The two kinds' voucher numbers have
        never collided, but the event type is part of `uq_inventory_event_source`
        and reading across both would call a shipment's line an
        already-imported return.

        A WITHDRAWN line is reported separately and still counts as applied.
        `reverse` does not delete the import's events — it appends a
        compensating IMPORT_REVERSED under the `REV:` source — so the original
        (event_type, source, master) identity stays occupied and
        `uq_inventory_event_source` will refuse to write it again. Calling such
        a line importable would have `apply` offer it, fail the insert, and
        count it as a duplicate while printing 適用しました: the plan must say
        what the write will actually do, so `applied` keeps it and `withdrawn`
        says it has been put back. `apply` refuses a file that contains one.

        One IMPORT_REVERSED under REV: is taken as proof the whole line was
        withdrawn. A bundle line holds one event per component and `reverse`
        writes them inside the caller's single transaction, so there is no state
        in which some of a line's components are compensated and others are not.
        """
        if reading.kind is None or not reading.vouchers:
            return AppliedLedger()
        vouchers = [voucher.voucher for voucher in reading.vouchers]
        originals = await self._session.execute(
            select(InventoryEvent.source_order_id, InventoryEvent.source_line_id)
            .where(
                InventoryEvent.event_type == _EVENT_TYPE[reading.kind],
                InventoryEvent.source_channel == WHOLESALE_CHANNEL,
                InventoryEvent.source_order_id.in_(vouchers),
            )
            .distinct()
        )
        withdrawn = await self._session.execute(
            select(InventoryEvent.source_order_id, InventoryEvent.source_line_id)
            .where(
                InventoryEvent.event_type == InventoryEventTypeEnum.IMPORT_REVERSED,
                InventoryEvent.source_channel == WHOLESALE_CHANNEL,
                InventoryEvent.source_order_id.in_(
                    [reversal_source_order_id(number) for number in vouchers]
                ),
            )
            .distinct()
        )
        reversed_pairs = {
            (strip_reversal_prefix(order_id), line_id)
            for order_id, line_id in withdrawn.all()
            if order_id is not None and line_id is not None
        }
        held = {
            (order_id, line_id)
            for order_id, line_id in originals.all()
            if order_id is not None and line_id is not None
        }
        return AppliedLedger(
            applied=frozenset(held),
            withdrawn=frozenset(held & reversed_pairs),
        )

    async def _vouchers_with_orders(self, reading: BmsReading) -> frozenset[str]:
        """取引番号 in this file that already carry an `orders` row.

        The stock ledger alone cannot say whether a voucher was imported.
        `_record_sale` books the sale for EVERY in-scope line, mapped or not and
        stock-managed or not, so a voucher whose lines are all unmapped or all
        在庫管理対象外 is imported without producing a single inventory event.
        Judging "was this imported?" from events only would call such a voucher
        untouched — and a cutover that excludes it would then withdraw the rest
        of the import and leave its revenue booked, under a clean report.
        """
        if not reading.vouchers:
            return frozenset()
        rows = await self._session.execute(
            select(Order.channel_order_id).where(
                Order.channel == WHOLESALE_CHANNEL,
                Order.channel_order_id.in_([v.voucher for v in reading.vouchers]),
            )
        )
        return frozenset(number for number in rows.scalars().all() if number is not None)


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
    "VoucherConflict",
    "WholesaleApplyResult",
    "WholesaleImportService",
    "WholesalePlan",
    "WholesaleReverseResult",
]
