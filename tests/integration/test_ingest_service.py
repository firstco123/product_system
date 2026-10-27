"""Integration tests — OrderIngestService end-to-end against real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.adapters import NormalizedOrder, NormalizedOrderLine
from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    MappingAlert,
    MappingAlertStatusEnum,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services import InventoryService, OrderIngestService

pytestmark = pytest.mark.integration


def _normalized(
    *,
    channel_order_id: str,
    sku: str,
    quantity: int = 1,
    channel: str = "shopify",
    status: str = "confirmed",
    variant_key: str | None = None,
) -> NormalizedOrder:
    return NormalizedOrder(
        channel=channel,
        channel_order_id=channel_order_id,
        status=status,  # type: ignore[arg-type]
        ordered_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
        items=[
            NormalizedOrderLine(
                line_id="L-1",
                channel_sku=sku,
                channel_variant_key=variant_key,
                quantity=quantity,
                unit_price=Decimal("1000"),
            )
        ],
        raw_payload={"sample": True},
    )


async def _seed_mapping(session, *, channel: str, sku: str) -> int:
    master = MasterSku(sku_code=f"MASTER-{sku}", name=sku)
    session.add(master)
    await session.flush()
    session.add(
        ChannelSkuMapping(
            master_sku_id=master.id,
            channel=channel,
            channel_sku=sku,
            is_active=True,
        )
    )
    await session.flush()
    return master.id


async def _seed_bundle_mapping(session, *, parent_code: str, comp_specs: list[tuple[str, int]]):
    """A bundle parent mapped on shopify + component masters with seeded stock."""
    from app.models import BundleComponent

    parent = MasterSku(sku_code=parent_code, name=parent_code, is_bundle=True)
    session.add(parent)
    await session.flush()
    session.add(
        ChannelSkuMapping(
            master_sku_id=parent.id, channel="shopify", channel_sku=parent_code, is_active=True
        )
    )
    inv = InventoryService(session)
    comps = []
    for code, stock in comp_specs:
        c = MasterSku(sku_code=code, name=code)
        session.add(c)
        await session.flush()
        await inv.manual_adjust(
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


async def test_bundle_order_fans_out_to_components(db_session) -> None:
    """An order for a bundle SKU decrements its COMPONENTS, not the parent."""
    parent, (a, b) = await _seed_bundle_mapping(
        db_session, parent_code="N21gold", comp_specs=[("N23gold", 10), ("N32gold", 10)]
    )
    inv = InventoryService(db_session)
    svc = OrderIngestService(db_session)

    result = await svc.ingest(_normalized(channel_order_id="OB-1", sku="N21gold", quantity=2))

    assert result.consumed_count == 1
    assert await inv.get_current_stock(a.id) == 8  # each component -2
    assert await inv.get_current_stock(b.id) == 8
    assert await inv.get_current_stock(parent.id) == 0  # parent holds no own stock
    assert await inv.get_bundle_available(parent.id) == 8  # min(8, 8)


async def test_bundle_cancellation_restores_components(db_session) -> None:
    _parent, (a,) = await _seed_bundle_mapping(
        db_session, parent_code="N21gold", comp_specs=[("N23gold", 10)]
    )
    inv = InventoryService(db_session)
    svc = OrderIngestService(db_session)

    await svc.ingest(_normalized(channel_order_id="OB-2", sku="N21gold", quantity=3))
    assert await inv.get_current_stock(a.id) == 7

    cancelled = await svc.ingest(
        _normalized(channel_order_id="OB-2", sku="N21gold", quantity=3, status="cancelled")
    )
    assert cancelled.cancelled_count == 1
    assert await inv.get_current_stock(a.id) == 10  # component restored


async def test_mapped_order_decrements_inventory(db_session) -> None:
    master_id = await _seed_mapping(db_session, channel="shopify", sku="MAPPED")
    svc = OrderIngestService(db_session)

    result = await svc.ingest(_normalized(channel_order_id="O-1", sku="MAPPED", quantity=3))

    assert result.created
    assert result.consumed_count == 1
    assert result.pending_mapping_count == 0
    assert result.order.status == "confirmed"
    assert await InventoryService(db_session).get_current_stock(master_id) == -3


async def test_unmapped_order_parks_in_pending_mapping(db_session) -> None:
    svc = OrderIngestService(db_session)
    result = await svc.ingest(_normalized(channel_order_id="O-2", sku="UNMAPPED"))

    assert result.pending_mapping_count == 1
    assert result.consumed_count == 0
    assert result.order.status == OrderStatusEnum.PENDING_MAPPING
    item = (
        await db_session.execute(select(OrderItem).where(OrderItem.order_id == result.order.id))
    ).scalar_one()
    assert item.master_sku_id is None
    alert = (await db_session.execute(select(MappingAlert))).scalar_one()
    assert alert.status == MappingAlertStatusEnum.OPEN
    assert alert.channel_sku == "UNMAPPED"


async def test_duplicate_order_is_idempotent(db_session) -> None:
    """Re-ingestion of the same order doesn't double-decrement."""
    master_id = await _seed_mapping(db_session, channel="shopify", sku="A")
    svc = OrderIngestService(db_session)
    order = _normalized(channel_order_id="O-3", sku="A", quantity=2)

    first = await svc.ingest(order)
    second = await svc.ingest(order)

    assert first.created is True
    assert second.created is False
    assert await InventoryService(db_session).get_current_stock(master_id) == -2


async def test_cancellation_transition_compensates(db_session) -> None:
    master_id = await _seed_mapping(db_session, channel="shopify", sku="C")
    svc = OrderIngestService(db_session)
    inventory = InventoryService(db_session)

    confirmed = _normalized(channel_order_id="O-4", sku="C", quantity=4)
    await svc.ingest(confirmed)
    assert await inventory.get_current_stock(master_id) == -4

    cancelled = _normalized(channel_order_id="O-4", sku="C", quantity=4, status="cancelled")
    result = await svc.ingest(cancelled)

    assert result.created is False
    assert result.cancelled_count == 1
    assert result.order.status == "cancelled"
    assert await inventory.get_current_stock(master_id) == 0


async def test_repeated_cancellation_does_not_double_compensate(db_session) -> None:
    master_id = await _seed_mapping(db_session, channel="shopify", sku="D")
    svc = OrderIngestService(db_session)
    inventory = InventoryService(db_session)

    await svc.ingest(_normalized(channel_order_id="O-5", sku="D", quantity=2))
    await svc.ingest(_normalized(channel_order_id="O-5", sku="D", quantity=2, status="cancelled"))
    stock_after_first_cancel = await inventory.get_current_stock(master_id)

    # Second cancellation: order is already cancelled; no further compensation.
    result = await svc.ingest(
        _normalized(channel_order_id="O-5", sku="D", quantity=2, status="cancelled")
    )
    assert result.cancelled_count == 0
    assert await inventory.get_current_stock(master_id) == stock_after_first_cancel


async def test_cancelled_on_arrival_does_not_decrement(db_session) -> None:
    master_id = await _seed_mapping(db_session, channel="shopify", sku="E")
    svc = OrderIngestService(db_session)

    await svc.ingest(_normalized(channel_order_id="O-6", sku="E", quantity=1, status="cancelled"))
    assert await InventoryService(db_session).get_current_stock(master_id) == 0


async def test_unmapped_alert_increments_occurrences(db_session) -> None:
    svc = OrderIngestService(db_session)
    await svc.ingest(_normalized(channel_order_id="O-7", sku="GHOST"))
    await svc.ingest(_normalized(channel_order_id="O-8", sku="GHOST"))

    alert = (await db_session.execute(select(MappingAlert))).scalar_one()
    assert alert.channel_sku == "GHOST"
    assert alert.occurrence_count >= 2


async def test_inventory_event_recorded_with_source(db_session) -> None:
    master_id = await _seed_mapping(db_session, channel="shopify", sku="F")
    svc = OrderIngestService(db_session)
    await svc.ingest(_normalized(channel_order_id="O-9", sku="F", quantity=1))

    event = (
        await db_session.execute(
            select(InventoryEvent).where(InventoryEvent.master_sku_id == master_id)
        )
    ).scalar_one()
    assert event.source_channel == "shopify"
    assert event.source_order_id == "O-9"
    assert event.source_line_id == "L-1"
    assert event.quantity_delta == -1


async def test_order_payload_persisted_as_jsonb(db_session) -> None:
    await _seed_mapping(db_session, channel="shopify", sku="G")
    svc = OrderIngestService(db_session)
    await svc.ingest(_normalized(channel_order_id="O-10", sku="G", quantity=1))

    order = (
        await db_session.execute(select(Order).where(Order.channel_order_id == "O-10"))
    ).scalar_one()
    assert order.raw_payload == {"sample": True}


async def _seed_unmanaged_mapping(session, *, sku: str, kind: str = "packaging") -> int:
    """A mapped master that holds no stock — gift box, coupon, made-to-order."""
    master = MasterSku(
        sku_code=f"MASTER-{sku}",
        name=sku,
        is_stock_managed=False,
        non_inventory_kind=kind,
    )
    session.add(master)
    await session.flush()
    session.add(
        ChannelSkuMapping(
            master_sku_id=master.id, channel="shopify", channel_sku=sku, is_active=True
        )
    )
    await session.flush()
    return master.id


async def test_unmanaged_sku_writes_no_event_on_order_or_cancellation(db_session) -> None:
    """The whole point of the flag: a gift box neither consumes nor is credited.

    Guarding only the consume path would be worse than not guarding at all — the
    cancellation would then ADD stock the order never took, and the gift box
    would climb positive instead of falling to -12509.
    """
    master_id = await _seed_unmanaged_mapping(db_session, sku="GIFTBOX")
    inv = InventoryService(db_session)
    svc = OrderIngestService(db_session)

    result = await svc.ingest(_normalized(channel_order_id="UM-1", sku="GIFTBOX", quantity=4))
    assert result.consumed_count == 0
    assert result.unmanaged_skipped == 1
    assert result.pending_mapping_count == 0  # it IS mapped; nothing for an operator to do
    assert result.order.status == "confirmed"

    cancelled = await svc.ingest(
        _normalized(channel_order_id="UM-1", sku="GIFTBOX", quantity=4, status="cancelled")
    )
    assert cancelled.cancelled_count == 0
    assert cancelled.unmanaged_skipped == 1

    events = (
        (
            await db_session.execute(
                select(InventoryEvent).where(InventoryEvent.master_sku_id == master_id)
            )
        )
        .scalars()
        .all()
    )
    assert events == []
    assert await inv.get_current_stock(master_id) == 0


async def test_unmanaged_line_does_not_block_its_neighbours(db_session) -> None:
    """A mixed order still consumes the real merchandise on the same order."""
    box = await _seed_unmanaged_mapping(db_session, sku="BOX2")
    real = await _seed_mapping(db_session, channel="shopify", sku="REAL2")
    svc = OrderIngestService(db_session)

    order = NormalizedOrder(
        channel="shopify",
        channel_order_id="UM-2",
        status="confirmed",  # type: ignore[arg-type]
        ordered_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
        items=[
            NormalizedOrderLine(
                line_id="L-1", channel_sku="BOX2", quantity=1, unit_price=Decimal("0")
            ),
            NormalizedOrderLine(
                line_id="L-2", channel_sku="REAL2", quantity=2, unit_price=Decimal("1000")
            ),
        ],
        raw_payload={},
    )
    result = await svc.ingest(order)

    assert result.consumed_count == 1
    assert result.unmanaged_skipped == 1
    inv = InventoryService(db_session)
    assert await inv.get_current_stock(box) == 0
    assert await inv.get_current_stock(real) == -2


async def test_a_line_repaired_without_stock_is_not_credited_on_cancellation(
    db_session,
) -> None:
    """`reresolve_order_items` deliberately fills `master_sku_id` in on
    historical lines without moving stock, because the physical count already
    settled the shelf. A backdated cancellation on one of those lines must not
    then credit stock the order never took — the compensation is driven by what
    the event log records, not by the line merely carrying a master SKU.
    """
    master_id = await _seed_mapping(db_session, channel="shopify", sku="REPAIRED")
    svc = OrderIngestService(db_session)
    inventory = InventoryService(db_session)

    # Arrives unmapped, so ingestion parks it and writes no event.
    await svc.ingest(_normalized(channel_order_id="O-REPAIRED", sku="UNKNOWN-THEN", quantity=3))
    assert await inventory.get_current_stock(master_id) == 0

    # The repair: the master SKU is filled in, stock untouched.
    item = (
        await db_session.execute(select(OrderItem).where(OrderItem.channel_sku == "UNKNOWN-THEN"))
    ).scalar_one()
    item.master_sku_id = master_id
    await db_session.flush()

    result = await svc.ingest(
        _normalized(channel_order_id="O-REPAIRED", sku="UNKNOWN-THEN", status="cancelled")
    )

    assert result.cancelled_count == 0
    assert await inventory.get_current_stock(master_id) == 0


async def _seed_split_page(db_session) -> tuple[int, int]:
    """One Rakuten page mapped twice: page-level to a coarse master, and
    per-variant to the master that actually holds that colour's stock.

    This is the live shape after the 2026-10-19 SKU-level cutover: 21 pages
    carry both kinds of row at once.
    """
    coarse = MasterSku(sku_code="073c", name="coarse page master")
    fine = MasterSku(sku_code="B34gold20cm", name="gold / 20cm")
    db_session.add_all([coarse, fine])
    await db_session.flush()
    db_session.add_all(
        [
            ChannelSkuMapping(
                master_sku_id=coarse.id, channel="rakuten", channel_sku="073c", is_active=True
            ),
            ChannelSkuMapping(
                master_sku_id=fine.id,
                channel="rakuten",
                channel_sku="073c",
                channel_variant_key="b34gold20cm",
                is_active=True,
            ),
        ]
    )
    await db_session.flush()
    inv = InventoryService(db_session)
    await inv.manual_adjust(
        master_sku_id=coarse.id, quantity_delta=10, reason="seed", operator="test"
    )
    await inv.manual_adjust(
        master_sku_id=fine.id, quantity_delta=10, reason="seed", operator="test"
    )
    return coarse.id, fine.id


async def test_a_line_naming_its_variant_consumes_the_variant_master(db_session) -> None:
    """The order line's SKU管理番号 decides the master, not the page.

    Before the key was threaded through, this order drained `073c` — a master
    the stocktake deliberately excludes, so nothing would ever correct it,
    while the per-colour master it should have hit stayed untouched.
    """
    coarse_id, fine_id = await _seed_split_page(db_session)
    inv = InventoryService(db_session)
    svc = OrderIngestService(db_session)

    result = await svc.ingest(
        _normalized(
            channel_order_id="RK-1",
            sku="073c",
            channel="rakuten",
            quantity=3,
            variant_key="b34gold20cm",
        )
    )

    assert result.consumed_count == 1
    assert await inv.get_current_stock(fine_id) == 7
    assert await inv.get_current_stock(coarse_id) == 10  # untouched


async def test_a_line_without_a_variant_still_resolves_by_page(db_session) -> None:
    """No flag day: a channel that sends no variant key keeps working.

    Shopify and Amazon lines carry None, and so does any Rakuten page not yet
    split, so the page-level row must still resolve on its own.
    """
    coarse_id, fine_id = await _seed_split_page(db_session)
    inv = InventoryService(db_session)
    svc = OrderIngestService(db_session)

    result = await svc.ingest(
        _normalized(channel_order_id="RK-2", sku="073c", channel="rakuten", quantity=2)
    )

    assert result.consumed_count == 1
    assert await inv.get_current_stock(coarse_id) == 8
    assert await inv.get_current_stock(fine_id) == 10  # untouched


async def test_an_unmapped_variant_falls_back_to_the_page(db_session) -> None:
    """A key that matches no row resolves by page rather than going unmapped.

    That keeps a partly-split page working, and is why the fallback is logged:
    a key that fails to match is otherwise indistinguishable from no key, which
    is precisely how the coarse masters kept draining unnoticed.
    """
    coarse_id, fine_id = await _seed_split_page(db_session)
    inv = InventoryService(db_session)
    svc = OrderIngestService(db_session)

    result = await svc.ingest(
        _normalized(
            channel_order_id="RK-3",
            sku="073c",
            channel="rakuten",
            quantity=1,
            variant_key="b34silver19cm",  # a real SKU of this page, not mapped yet
        )
    )

    assert result.consumed_count == 1
    assert await inv.get_current_stock(coarse_id) == 9
    assert await inv.get_current_stock(fine_id) == 10
