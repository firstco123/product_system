"""Integration tests — InventoryService invariants against real Postgres.

Skipped automatically when the test DB is unreachable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import BundleComponent, InventoryEvent, InventoryEventTypeEnum, MasterSku
from app.services import (
    EventSource,
    InventoryInsufficientError,
    InventoryService,
    MasterSkuNotFoundError,
)

pytestmark = pytest.mark.integration


async def _make_sku(session, code: str = "TEST-001") -> MasterSku:
    sku = MasterSku(sku_code=code, name="Test SKU")
    session.add(sku)
    await session.flush()
    return sku


async def _make_bundle(session, parent_code: str, comp_specs: list[tuple[str, int]]):
    """Create a bundle parent (is_bundle) + component masters with seeded stock +
    bundle_components links. Returns (parent, [components])."""
    parent = MasterSku(sku_code=parent_code, name=parent_code, is_bundle=True)
    session.add(parent)
    await session.flush()
    svc = InventoryService(session)
    comps = []
    for code, stock in comp_specs:
        c = MasterSku(sku_code=code, name=code)
        session.add(c)
        await session.flush()
        if stock:
            await svc.manual_adjust(
                master_sku_id=c.id, quantity_delta=stock, reason="seed", operator="t"
            )
        session.add(
            BundleComponent(
                bundle_master_sku_id=parent.id, component_master_sku_id=c.id, quantity_per=1
            )
        )
        comps.append(c)
    await session.flush()
    return parent, comps


async def test_get_bundle_available_is_min_over_components(db_session) -> None:
    parent, _ = await _make_bundle(db_session, "N21gold", [("N23gold", 27), ("N32gold", 55)])
    svc = InventoryService(db_session)
    assert await svc.get_bundle_available(parent.id) == 27  # min(27, 55)


async def test_resolve_consumption_fans_out_bundle_but_not_normal(db_session) -> None:
    parent, (a, b) = await _make_bundle(db_session, "N21gold", [("N23gold", 5), ("N32gold", 5)])
    svc = InventoryService(db_session)
    assert set(await svc.resolve_consumption(parent.id)) == {(a.id, 1), (b.id, 1)}
    assert await svc.resolve_consumption(a.id) == [(a.id, 1)]  # a normal SKU is itself


async def test_bundle_fan_out_decrements_each_component(db_session) -> None:
    """One bundle order line decrements every component; all component events
    share one source (master_sku_id disambiguates the widened UNIQUE)."""
    parent, (a, b) = await _make_bundle(db_session, "N21gold", [("N23gold", 27), ("N32gold", 55)])
    svc = InventoryService(db_session)
    src = EventSource(channel="shopify", order_id="O-1", line_id="L-1")
    for comp_id, qty_per in await svc.resolve_consumption(parent.id):
        await svc.consume_for_order_line(master_sku_id=comp_id, quantity=2 * qty_per, source=src)
    assert await svc.get_current_stock(a.id) == 25  # 27 - 2
    assert await svc.get_current_stock(b.id) == 53  # 55 - 2
    assert await svc.get_bundle_available(parent.id) == 25  # min(25, 53)


async def test_consume_decrements_snapshot(db_session) -> None:
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)

    await service.manual_adjust(
        master_sku_id=sku.id, quantity_delta=10, reason="receipt", operator="op1"
    )
    await service.consume_for_order_line(
        master_sku_id=sku.id,
        quantity=3,
        source=EventSource(channel="shopify", order_id="O-1", line_id="L-1"),
    )

    assert await service.get_current_stock(sku.id) == 7


async def test_consume_is_idempotent(db_session) -> None:
    """Re-applying the same source identifiers must not double-decrement."""
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.manual_adjust(
        master_sku_id=sku.id, quantity_delta=10, reason="receipt", operator="op1"
    )
    src = EventSource(channel="shopify", order_id="O-1", line_id="L-1")

    first = await service.consume_for_order_line(master_sku_id=sku.id, quantity=3, source=src)
    second = await service.consume_for_order_line(master_sku_id=sku.id, quantity=3, source=src)

    assert first is not None
    assert second is None  # idempotent skip
    assert await service.get_current_stock(sku.id) == 7


async def test_cancellation_restores_stock(db_session) -> None:
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.manual_adjust(
        master_sku_id=sku.id, quantity_delta=5, reason="receipt", operator="op1"
    )
    src = EventSource(channel="rakuten", order_id="R-1", line_id="L-1")

    await service.consume_for_order_line(master_sku_id=sku.id, quantity=2, source=src)
    await service.cancel_order_line(master_sku_id=sku.id, quantity=2, source=src)

    assert await service.get_current_stock(sku.id) == 5


async def test_cancellation_is_idempotent(db_session) -> None:
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.manual_adjust(
        master_sku_id=sku.id, quantity_delta=5, reason="receipt", operator="op1"
    )
    src = EventSource(channel="rakuten", order_id="R-2", line_id="L-1")
    await service.consume_for_order_line(master_sku_id=sku.id, quantity=2, source=src)

    first = await service.cancel_order_line(master_sku_id=sku.id, quantity=2, source=src)
    second = await service.cancel_order_line(master_sku_id=sku.id, quantity=2, source=src)

    assert first is not None
    assert second is None
    assert await service.get_current_stock(sku.id) == 5


async def test_manual_adjust_rejects_negative_stock(db_session) -> None:
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.manual_adjust(
        master_sku_id=sku.id, quantity_delta=2, reason="initial", operator="op1"
    )
    with pytest.raises(InventoryInsufficientError):
        await service.manual_adjust(
            master_sku_id=sku.id, quantity_delta=-5, reason="bad", operator="op1"
        )


async def test_consume_allows_oversell_to_negative(db_session) -> None:
    """Order-driven events must NOT block on insufficient stock.

    Backorders/oversells are a business reality; the system records them
    accurately and the operator reconciles through manual_adjust.
    """
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.consume_for_order_line(
        master_sku_id=sku.id,
        quantity=3,
        source=EventSource(channel="shopify", order_id="O-1", line_id="L-1"),
    )
    assert await service.get_current_stock(sku.id) == -3


async def test_unknown_sku_raises(db_session) -> None:
    service = InventoryService(db_session)
    with pytest.raises(MasterSkuNotFoundError):
        await service.manual_adjust(
            master_sku_id=99999, quantity_delta=1, reason="x", operator="op1"
        )


async def test_event_records_source_and_operator(db_session) -> None:
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.manual_adjust(
        master_sku_id=sku.id,
        quantity_delta=5,
        reason="stocktake",
        operator="alice@example.com",
        occurred_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
    )

    result = await db_session.execute(
        select(InventoryEvent).where(InventoryEvent.master_sku_id == sku.id)
    )
    event = result.scalar_one()
    assert event.event_type == InventoryEventTypeEnum.MANUAL_ADJUST
    assert event.reason == "stocktake"
    assert event.operator == "alice@example.com"
    assert event.quantity_delta == 5


# ---------------------------------------------------------------------------
# Reversing an import. The 卸売EDI cutover is set once and was previously
# irreversible, so a run made against the wrong 棚卸日 could not be undone.
# ---------------------------------------------------------------------------


async def test_reversing_a_consumed_line_restores_stock_exactly_once(db_session) -> None:
    sku = await _make_sku(db_session, "B14goldanklet")
    svc = InventoryService(db_session)
    await svc.manual_adjust(master_sku_id=sku.id, quantity_delta=10, reason="seed", operator="t")
    src = EventSource(channel="wholesale", order_id="12345", line_id="1")

    await svc.consume_order_line(master_sku_id=sku.id, quantity=3, source=src)
    assert await svc.get_current_stock(sku.id) == 7

    first = await svc.reverse_order_line(source=src, restores_stock=True)
    assert (first.found, first.written) == (1, 1)
    assert await svc.get_current_stock(sku.id) == 10

    # Running the reversal again is a no-op, not a second correction.
    second = await svc.reverse_order_line(source=src, restores_stock=True)
    # Still FOUND the original — that is what tells a caller the line really was
    # withdrawn, rather than never imported.
    assert (second.found, second.written, second.already) == (1, 0, 1)
    assert await svc.get_current_stock(sku.id) == 10


async def test_reversing_a_credited_line_takes_the_stock_away_again(db_session) -> None:
    """A 返品 import ADDS stock, so undoing it must subtract."""
    sku = await _make_sku(db_session, "B14silveranklet")
    svc = InventoryService(db_session)
    await svc.manual_adjust(master_sku_id=sku.id, quantity_delta=10, reason="seed", operator="t")
    # credit_unlinked_return, not return_order_line: 元取引番号 is empty on every
    # carrier return line, so a wholesale 返品 names no shipment to reverse.
    voucher = EventSource(channel="wholesale", order_id="R-900", line_id="1")
    await svc.credit_unlinked_return(master_sku_id=sku.id, quantity=4, source=voucher)
    assert await svc.get_current_stock(sku.id) == 14

    await svc.reverse_order_line(source=voucher, restores_stock=False)
    assert await svc.get_current_stock(sku.id) == 10


async def test_a_reversal_does_not_occupy_the_return_event_for_the_same_voucher(
    db_session,
) -> None:
    """The reason IMPORT_REVERSED is its own event type.

    `uq_inventory_event_source` includes event_type. Had a reversal written
    CANCELLATION_RETURNED on the voucher's own source, it would have consumed the
    identity a 返品 import of that same 取引番号 needs, and that import would
    then silently no-op.
    """
    sku = await _make_sku(db_session, "B21goldanklet")
    svc = InventoryService(db_session)
    await svc.manual_adjust(master_sku_id=sku.id, quantity_delta=20, reason="seed", operator="t")
    voucher = EventSource(channel="wholesale", order_id="55555", line_id="1")

    await svc.consume_order_line(master_sku_id=sku.id, quantity=2, source=voucher)
    await svc.reverse_order_line(source=voucher, restores_stock=True)
    # A 返品 of the same 取引番号 can still write its own event afterwards.
    credited = await svc.cancel_order_line(master_sku_id=sku.id, quantity=2, source=voucher)
    assert credited is not None

    types = (
        (
            await db_session.execute(
                select(InventoryEvent.event_type).where(InventoryEvent.source_order_id == "55555")
            )
        )
        .scalars()
        .all()
    )
    # The voucher's own identity holds only the import's two events; the
    # reversal lives under REV:, so neither kind was displaced.
    assert sorted(types) == [
        InventoryEventTypeEnum.CANCELLATION_RETURNED.value,
        InventoryEventTypeEnum.ORDER_CONSUMED.value,
    ]
    reversed_rows = (
        (
            await db_session.execute(
                select(InventoryEvent.event_type).where(
                    InventoryEvent.source_order_id == "REV:55555"
                )
            )
        )
        .scalars()
        .all()
    )
    assert reversed_rows == [InventoryEventTypeEnum.IMPORT_REVERSED.value]


async def test_reversing_a_bundle_line_restores_every_component(db_session) -> None:
    parent, (a, b) = await _make_bundle(db_session, "N21gold", [("N23gold", 27), ("N32gold", 55)])
    svc = InventoryService(db_session)
    voucher = EventSource(channel="wholesale", order_id="77777", line_id="1")
    await svc.consume_order_line(master_sku_id=parent.id, quantity=2, source=voucher)
    assert (await svc.get_current_stock(a.id), await svc.get_current_stock(b.id)) == (25, 53)

    result = await svc.reverse_order_line(source=voucher, restores_stock=True)
    # Two events because the import wrote two, not because the parent expands to
    # two today — see `reverse_order_line`.
    assert (result.found, result.written) == (2, 2)
    assert (await svc.get_current_stock(a.id), await svc.get_current_stock(b.id)) == (27, 55)


async def test_reversing_an_unmanaged_master_writes_nothing(db_session) -> None:
    """A ギフトバッグ was never consumed, so it must not be credited now either —
    the asymmetry that would otherwise invent stock for an item that has none."""
    sku = MasterSku(
        sku_code="GIFTBAG",
        name="gift bag",
        is_stock_managed=False,
        # ck_master_sku_non_inventory_kind: the flag and the kind are always
        # set as a pair, so the flag alone will not insert.
        non_inventory_kind="packaging",
    )
    db_session.add(sku)
    await db_session.flush()
    svc = InventoryService(db_session)

    result = await svc.reverse_order_line(
        source=EventSource(channel="wholesale", order_id="88888", line_id="1"),
        restores_stock=True,
    )
    # Nothing FOUND, which is the honest answer: an unmanaged master was never
    # written, so there is no movement to compensate.
    assert (result.found, result.written) == (0, 0)
