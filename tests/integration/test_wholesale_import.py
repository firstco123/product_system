"""Integration tests — WholesaleImportService against real Postgres (docs/29 §4).

The sign is what these tests are really about. A shipment and a return read from
almost identical files and move stock in opposite directions, so every test here
states which direction it expects rather than only how much.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    BundleComponent,
    ChannelSkuMapping,
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
)
from app.models.enums import NonInventoryKindEnum
from app.services import InventoryService, WholesaleImportService
from app.services.wholesale_import import WHOLESALE_CHANNEL
from app.wholesale_bms import BmsKind, read_bms
from tests.test_wholesale_bms import order_file, return_file

pytestmark = pytest.mark.integration

BUYER = "0393"
CUTOVER = date(2026, 9, 1)

#: The two defaults the shared fixtures use, so a test that does not care which
#: product it is can stay quiet about it.
ORDER_CODE = "2700002430598"
RETURN_CODE = "2700002488353"


async def _master(
    session,
    *,
    sku_code: str,
    stock: int | None = 0,
    is_stock_managed: bool = True,
    non_inventory_kind: str | None = None,
) -> MasterSku:
    master = MasterSku(
        sku_code=sku_code,
        name=f"{sku_code} の商品",
        is_stock_managed=is_stock_managed,
        non_inventory_kind=non_inventory_kind,
    )
    session.add(master)
    await session.flush()
    if stock is not None and is_stock_managed:
        session.add(InventorySnapshot(master_sku_id=master.id, on_hand_qty=stock))
        await session.flush()
    return master


async def _map(session, *, code: str, master: MasterSku, is_active: bool = True) -> None:
    session.add(
        ChannelSkuMapping(
            master_sku_id=master.id,
            channel=WHOLESALE_CHANNEL,
            channel_sku=code,
            # Wholesale rows never carry one; the carrier's code IS the
            # sellable unit.
            channel_variant_key=None,
            marketplace_id=BUYER,
            is_active=is_active,
        )
    )
    await session.flush()


async def _stock_of(session, master_sku_id: int) -> int:
    return await InventoryService(session).get_current_stock(master_sku_id)


async def _events_of(session, master_sku_id: int) -> list[InventoryEvent]:
    rows = await session.execute(
        select(InventoryEvent)
        .where(InventoryEvent.master_sku_id == master_sku_id)
        .order_by(InventoryEvent.id)
    )
    return list(rows.scalars().all())


# ------------------------------------------------------------------ the two signs


async def test_a_shipment_decrements_and_records_the_voucher(db_session) -> None:
    master = await _master(db_session, sku_code="N63-GD", stock=10)
    await _map(db_session, code=ORDER_CODE, master=master)
    reading = read_bms(order_file({"出荷数量（バラ）": "3", "発注数量（バラ）": "3"}))

    service = WholesaleImportService(db_session)
    plan = await service.plan(reading, cutover_on=CUTOVER)
    result = await service.apply(plan, operator="tester")

    assert plan.kind is BmsKind.ORDER
    assert plan.total_delta == -3
    assert result.applied_lines == 1
    assert await _stock_of(db_session, master.id) == 7
    (event,) = await _events_of(db_session, master.id)
    assert event.event_type == InventoryEventTypeEnum.ORDER_CONSUMED
    assert event.quantity_delta == -3
    assert event.source_channel == WHOLESALE_CHANNEL
    # The zero-padded voucher and line number, intact — the idempotency key.
    assert event.source_order_id == "000109900"
    assert event.source_line_id == "01"


async def test_a_return_increments_although_no_shipment_was_ever_recorded(db_session) -> None:
    """The case `return_order_line` refuses by design.

    元取引番号 is empty on every line the carrier sends, so there is no shipment
    to match. docs/29 §4 settles it as an independent addition; if this went
    through the guarded path, all 1,887 return lines would be silently dropped.
    """
    master = await _master(db_session, sku_code="B51-SV", stock=2)
    await _map(db_session, code=RETURN_CODE, master=master)
    reading = read_bms(return_file({"返品数量（バラ）": "5"}))

    service = WholesaleImportService(db_session)
    plan = await service.plan(reading, cutover_on=date(2026, 6, 1))
    result = await service.apply(plan, operator="tester")

    assert plan.kind is BmsKind.RETURN
    assert plan.total_delta == 5
    assert result.applied_lines == 1
    assert await _stock_of(db_session, master.id) == 7
    (event,) = await _events_of(db_session, master.id)
    # Not `receipt`: the analytics rollup counts `cancellation_returned` as the
    # returned column, so a wholesale return has to land there to be visible.
    assert event.event_type == InventoryEventTypeEnum.CANCELLATION_RETURNED
    assert event.quantity_delta == 5


async def test_the_event_is_stamped_at_jst_midnight_of_the_voucher_date(db_session) -> None:
    """A JST day is [15:00 UTC previous day, 15:00 UTC). A date stamped at UTC
    midnight files the movement under the previous JST day in every aggregate."""
    master = await _master(db_session, sku_code="N63-GD", stock=5)
    await _map(db_session, code=ORDER_CODE, master=master)
    reading = read_bms(order_file({"発注日": "20260914"}))

    service = WholesaleImportService(db_session)
    await service.apply(await service.plan(reading, cutover_on=CUTOVER), operator="t")

    (event,) = await _events_of(db_session, master.id)
    assert event.occurred_at.isoformat() == "2026-09-13T15:00:00+00:00"


# ------------------------------------------------------------------- idempotency


async def test_re_applying_the_same_file_moves_nothing(db_session) -> None:
    master = await _master(db_session, sku_code="N63-GD", stock=10)
    await _map(db_session, code=ORDER_CODE, master=master)
    reading = read_bms(order_file({"出荷数量（バラ）": "3"}))
    service = WholesaleImportService(db_session)

    first = await service.apply(await service.plan(reading, cutover_on=CUTOVER), operator="t")
    second_plan = await service.plan(reading, cutover_on=CUTOVER)
    second = await service.apply(second_plan, operator="t")

    assert first.applied_lines == 1
    # The SECOND plan already knows, so the screen says "already imported"
    # rather than offering an import that would quietly do nothing.
    assert len(second_plan.already_applied_lines) == 1
    assert second_plan.movable_lines == []
    assert second.applied_lines == 0
    assert await _stock_of(db_session, master.id) == 7
    assert len(await _events_of(db_session, master.id)) == 1


async def test_a_voucher_already_imported_still_shows_its_current_stock(db_session) -> None:
    """An already-applied line projects its CURRENT stock, not stock minus the
    quantity again — otherwise a re-upload preview reads like a second hit."""
    master = await _master(db_session, sku_code="N63-GD", stock=10)
    await _map(db_session, code=ORDER_CODE, master=master)
    reading = read_bms(order_file({"出荷数量（バラ）": "4"}))
    service = WholesaleImportService(db_session)
    await service.apply(await service.plan(reading, cutover_on=CUTOVER), operator="t")

    plan = await service.plan(reading, cutover_on=CUTOVER)

    (line,) = plan.all_lines
    assert line.already_applied
    assert line.current_qty == 6
    assert line.projected_qty == 6


# ---------------------------------------------------------------- the cutover date


async def test_vouchers_dated_before_the_cutover_are_excluded_not_imported(db_session) -> None:
    """docs/29 §7-2. The supplied history would add 3,045 phantom units, and the
    return file genuinely reaches back to 2026-06-30, so every upload carries
    pre-cutover rows."""
    master = await _master(db_session, sku_code="B51-SV", stock=0)
    await _map(db_session, code=RETURN_CODE, master=master)
    reading = read_bms(
        return_file(
            {"取引番号（発注・返品）": "000171243", "計上日": "20260630", "返品数量（バラ）": "7"},
            {"取引番号（発注・返品）": "000171244", "計上日": "20260909", "返品数量（バラ）": "2"},
        )
    )

    service = WholesaleImportService(db_session)
    plan = await service.plan(reading, cutover_on=date(2026, 9, 1))
    result = await service.apply(plan, operator="t")

    assert [v.voucher.voucher for v in plan.excluded_vouchers] == ["000171243"]
    assert [v.voucher.voucher for v in plan.vouchers] == ["000171244"]
    assert result.applied_lines == 1
    assert await _stock_of(db_session, master.id) == 2


async def test_the_cutover_day_itself_is_imported_and_counted(db_session) -> None:
    """The boundary is inclusive — a 開始日 includes itself — but the count was
    taken at some hour of that day and the file carries only a date, so those
    lines are counted separately for the operator to judge."""
    master = await _master(db_session, sku_code="N63-GD", stock=10)
    await _map(db_session, code=ORDER_CODE, master=master)
    reading = read_bms(order_file({"発注日": "20260901", "出荷数量（バラ）": "2"}))

    service = WholesaleImportService(db_session)
    plan = await service.plan(reading, cutover_on=date(2026, 9, 1))
    await service.apply(plan, operator="t")

    assert len(plan.vouchers) == 1
    assert len(plan.lines_on_cutover) == 1
    assert await _stock_of(db_session, master.id) == 8


async def test_planning_needs_a_cutover_and_will_not_invent_one(db_session) -> None:
    service = WholesaleImportService(db_session)
    reading = read_bms(order_file())

    with pytest.raises(TypeError):
        await service.plan(reading)  # type: ignore[call-arg]


# ------------------------------------------------------- what must not be applied


async def test_an_unmapped_code_is_reported_and_never_guessed_at(db_session) -> None:
    reading = read_bms(order_file({"出荷数量（バラ）": "3"}))

    service = WholesaleImportService(db_session)
    plan = await service.plan(reading, cutover_on=CUTOVER)
    result = await service.apply(plan, operator="t")

    assert plan.unmapped_codes == {ORDER_CODE: 1}
    assert len(plan.unmapped_lines) == 1
    assert plan.movable_lines == []
    assert result.applied_lines == 0


async def test_an_inactive_mapping_does_not_resolve(db_session) -> None:
    master = await _master(db_session, sku_code="N63-GD", stock=10)
    await _map(db_session, code=ORDER_CODE, master=master, is_active=False)

    service = WholesaleImportService(db_session)
    plan = await service.plan(read_bms(order_file()), cutover_on=CUTOVER)

    assert plan.unmapped_codes == {ORDER_CODE: 1}
    assert await _stock_of(db_session, master.id) == 10


async def test_the_gift_bag_moves_no_stock_in_either_direction(db_session) -> None:
    """docs/29 §5-4: 20 shipped against 154 returned. Without the
    在庫管理対象外 stop this one code climbs forever — the shape that took Phase
    1-B's gift boxes to -12,509."""
    bag = await _master(
        db_session,
        sku_code="IT-GIFTBAG",
        stock=None,
        is_stock_managed=False,
        non_inventory_kind=NonInventoryKindEnum.PACKAGING,
    )
    await _map(db_session, code=RETURN_CODE, master=bag)
    reading = read_bms(return_file({"返品数量（バラ）": "154"}))

    service = WholesaleImportService(db_session)
    plan = await service.plan(reading, cutover_on=date(2026, 6, 1))
    result = await service.apply(plan, operator="t")

    assert len(plan.unmanaged_lines) == 1
    assert plan.total_delta == 0
    assert result.unmanaged_lines == 1
    assert result.applied_lines == 0
    assert await _events_of(db_session, bag.id) == []


# ------------------------------------------------------------------ negative stock


async def test_a_shipment_that_outruns_stock_is_applied_and_flagged(db_session) -> None:
    """docs/29 §8-1 assumed `manual_adjust` and so proposed holding the line.
    The goods shipped; a held line leaves stock wrong in the other direction
    with nothing on screen. It applies, and the plan says it will go negative."""
    master = await _master(db_session, sku_code="N63-GD", stock=1)
    await _map(db_session, code=ORDER_CODE, master=master)
    reading = read_bms(order_file({"出荷数量（バラ）": "4"}))

    service = WholesaleImportService(db_session)
    plan = await service.plan(reading, cutover_on=CUTOVER)
    result = await service.apply(plan, operator="t")

    (line,) = plan.all_lines
    assert line.current_qty == 1
    assert line.projected_qty == -3
    assert line.goes_negative
    assert len(plan.negative_lines) == 1
    assert result.applied_lines == 1
    assert await _stock_of(db_session, master.id) == -3


async def test_a_master_with_no_snapshot_row_projects_from_zero(db_session) -> None:
    """138 of the 575 SKU-level masters have no snapshot row. The projection has
    to read that as 0 rather than skipping the line."""
    master = await _master(db_session, sku_code="N63-GD", stock=None)
    await _map(db_session, code=ORDER_CODE, master=master)
    reading = read_bms(order_file({"出荷数量（バラ）": "2"}))

    service = WholesaleImportService(db_session)
    plan = await service.plan(reading, cutover_on=CUTOVER)
    await service.apply(plan, operator="t")

    (line,) = plan.all_lines
    assert line.current_qty == 0
    assert line.projected_qty == -2
    assert await _stock_of(db_session, master.id) == -2


async def test_two_lines_on_one_master_project_cumulatively(db_session) -> None:
    """A voucher can name the same product twice. The second line must not claim
    the stock the first already spent, or neither looks like it goes negative."""
    master = await _master(db_session, sku_code="N63-GD", stock=3)
    await _map(db_session, code=ORDER_CODE, master=master)
    reading = read_bms(
        order_file(
            {"取引明細番号（発注・返品）": "01", "出荷数量（バラ）": "2"},
            {"取引明細番号（発注・返品）": "02", "出荷数量（バラ）": "2"},
        )
    )

    service = WholesaleImportService(db_session)
    plan = await service.plan(reading, cutover_on=CUTOVER)
    await service.apply(plan, operator="t")

    first, second = plan.all_lines
    assert (first.current_qty, first.projected_qty) == (3, 1)
    assert (second.current_qty, second.projected_qty) == (1, -1)
    assert len(plan.negative_lines) == 1
    assert plan.total_delta == -4
    assert await _stock_of(db_session, master.id) == -1


# ------------------------------------------------------------------------ bundles


async def test_a_bundle_fans_out_both_ways(db_session) -> None:
    """A set sold wholesale must decrement its components, and one returned must
    credit the same components — `credit_unlinked_return` mirrors
    `consume_order_line` so a return cannot credit something the shipment never
    took from."""
    left = await _master(db_session, sku_code="COMP-L", stock=10)
    right = await _master(db_session, sku_code="COMP-R", stock=10)
    parent = await _master(db_session, sku_code="SET-1", stock=0)
    db_session.add_all(
        [
            BundleComponent(
                bundle_master_sku_id=parent.id,
                component_master_sku_id=left.id,
                quantity_per=1,
            ),
            BundleComponent(
                bundle_master_sku_id=parent.id,
                component_master_sku_id=right.id,
                quantity_per=2,
            ),
        ]
    )
    await db_session.flush()
    await _map(db_session, code=ORDER_CODE, master=parent)
    await _map(db_session, code=RETURN_CODE, master=parent)

    service = WholesaleImportService(db_session)
    shipped = await service.plan(
        read_bms(order_file({"出荷数量（バラ）": "2"})), cutover_on=CUTOVER
    )
    ship_result = await service.apply(shipped, operator="t")
    assert ship_result.events_written == 2
    assert await _stock_of(db_session, left.id) == 8
    assert await _stock_of(db_session, right.id) == 6

    returned = await service.plan(
        read_bms(return_file({"返品数量（バラ）": "1"})), cutover_on=date(2026, 6, 1)
    )
    return_result = await service.apply(returned, operator="t")
    assert return_result.events_written == 2
    assert await _stock_of(db_session, left.id) == 9
    assert await _stock_of(db_session, right.id) == 8


# ----------------------------------------------------------------- refusing input


async def test_a_file_that_did_not_read_cleanly_has_no_plan(db_session) -> None:
    """Every number in a plan is signed by the kind, so a file whose kind is in
    doubt must not reach planning at all."""
    reading = read_bms(order_file({"処理種別": "02"}))
    assert reading.fatal

    with pytest.raises(ValueError, match="did not read cleanly"):
        await WholesaleImportService(db_session).plan(reading, cutover_on=CUTOVER)


async def test_the_screen_shape_reports_every_category_at_once(db_session) -> None:
    short = await _master(db_session, sku_code="N63-GD", stock=1)
    await _map(db_session, code=ORDER_CODE, master=short)
    reading = read_bms(
        order_file(
            {"取引明細番号（発注・返品）": "01", "出荷数量（バラ）": "4"},
            {"取引明細番号（発注・返品）": "02", "商品コード（発注用）": "2700002430437"},
            {
                "取引番号（発注・返品）": "000109765",
                "取引明細番号（発注・返品）": "01",
                "発注日": "20260815",
            },
        )
    )

    plan = await WholesaleImportService(db_session).plan(reading, cutover_on=CUTOVER)

    shape = plan.as_dict()
    assert shape["kind"] == "order"
    assert shape["voucher_count"] == 1
    assert shape["excluded_voucher_count"] == 1
    assert shape["excluded_line_count"] == 1
    assert shape["movable_line_count"] == 1
    assert shape["unmapped_line_count"] == 1
    assert shape["unmapped_codes"] == {"2700002430437": 1}
    assert shape["negative_line_count"] == 1
    assert shape["total_delta"] == -4
    assert shape["cutover_on"] == "2026-09-01"


async def test_a_price_is_carried_through_for_the_sales_step(db_session) -> None:
    """原単価 is not used by §4, but §6 bills from it and reads the same plan.
    Pinned here so the parser's Decimal does not quietly become a float."""
    master = await _master(db_session, sku_code="N63-GD", stock=5)
    await _map(db_session, code=ORDER_CODE, master=master)
    reading = read_bms(order_file({"原単価": "7898"}))

    plan = await WholesaleImportService(db_session).plan(reading, cutover_on=CUTOVER)

    (line,) = plan.all_lines
    assert line.line.unit_cost == Decimal("7898")
    assert isinstance(line.line.unit_cost, Decimal)
