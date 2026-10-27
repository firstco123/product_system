"""Integration tests — BundlePushService (batched derived-availability push)."""

from __future__ import annotations

import pytest

from app.models import BundleComponent, ChannelSkuMapping, MasterSku
from app.services import BundlePushService, EventSource, InventoryService
from app.services.mapping import channel_targets

pytestmark = pytest.mark.integration


class _FakeAdapter:
    """Minimal ChannelAdapter double that records push_inventory calls."""

    channel = "shopify"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def push_inventory(self, channel_sku: str, quantity: int) -> dict:
        self.calls.append((channel_sku, quantity))
        return {"ok": True}


async def _bundle(session, parent_code, comp_specs, *, shopify_sku=None):
    parent = MasterSku(sku_code=parent_code, name=parent_code, is_bundle=True)
    session.add(parent)
    await session.flush()
    if shopify_sku:
        session.add(
            ChannelSkuMapping(
                master_sku_id=parent.id, channel="shopify", channel_sku=shopify_sku, is_active=True
            )
        )
    inv = InventoryService(session)
    comps = []
    for code, stock in comp_specs:
        c = MasterSku(sku_code=code, name=code)
        session.add(c)
        await session.flush()
        if stock:
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


async def test_push_bundles_pushes_derived_availability(db_session) -> None:
    parent, _ = await _bundle(
        db_session, "N21gold", [("N23gold", 27), ("N32gold", 55)], shopify_sku="N21gold"
    )
    svc = BundlePushService(db_session)
    fake = _FakeAdapter()
    attempts = await svc.push_bundles(fake, [parent.id], triggered_by="test")
    assert fake.calls == [("N21gold", 27)]  # min(27, 55)
    assert len(attempts) == 1
    assert attempts[0].status == "succeeded"


async def test_push_bundles_skips_bundle_without_channel_mapping(db_session) -> None:
    parent, _ = await _bundle(db_session, "N21gold", [("N23gold", 5)], shopify_sku=None)
    svc = BundlePushService(db_session)
    fake = _FakeAdapter()
    attempts = await svc.push_bundles(fake, [parent.id], triggered_by="test")
    assert fake.calls == []
    assert attempts == []


async def test_dependent_bundle_ids_reverse_lookup(db_session) -> None:
    """A shared component (N23) resolves to every bundle that consumes it."""
    n23 = MasterSku(sku_code="N23gold", name="N23")
    db_session.add(n23)
    await db_session.flush()
    s1, _ = await _bundle(db_session, "N21gold", [], shopify_sku="N21gold")
    s2, _ = await _bundle(db_session, "N09gold", [], shopify_sku="N09gold")
    db_session.add_all(
        [
            BundleComponent(bundle_master_sku_id=s1.id, component_master_sku_id=n23.id),
            BundleComponent(bundle_master_sku_id=s2.id, component_master_sku_id=n23.id),
        ]
    )
    await db_session.flush()
    svc = BundlePushService(db_session)
    assert set(await svc.dependent_bundle_ids([n23.id])) == {s1.id, s2.id}


async def test_push_bundles_clamps_negative_component_to_zero(db_session) -> None:
    # A component driven NEGATIVE (order_consumed allows oversell) must advertise
    # 0, never a negative quantity.
    parent, (a, _b) = await _bundle(
        db_session, "N21gold", [("N23gold", 3), ("N32gold", 55)], shopify_sku="N21gold"
    )
    inv = InventoryService(db_session)
    await inv.consume_for_order_line(  # 3 - 5 = -2 (oversell)
        master_sku_id=a.id,
        quantity=5,
        source=EventSource(channel="shopify", order_id="O", line_id="L"),
    )
    svc = BundlePushService(db_session)
    fake = _FakeAdapter()
    await svc.push_bundles(fake, [parent.id], triggered_by="test")
    assert fake.calls == [("N21gold", 0)]  # min(-2, 55) -> clamped to 0


# --------------------------------------------- every page, not one of them (Stage 1)


class _RakutenFake:
    """Records the (page, variant, quantity) triples it is asked to write.

    Keyword-only `variant_key`, exactly like the real adapter, so a caller that
    forgets it fails here the way it fails in production.
    """

    channel = "rakuten"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int]] = []

    async def push_inventory(
        self, channel_sku: str, quantity: int, *, variant_key: str | None = None
    ) -> None:
        if variant_key is None:
            raise ValueError("Rakuten inventory is SKU-level; push_inventory needs the SKU管理番号")
        self.calls.append((channel_sku, variant_key, quantity))
        return None


async def _map(session, master_sku_id: int, channel: str, sku: str, key: str | None = None) -> None:
    session.add(
        ChannelSkuMapping(
            master_sku_id=master_sku_id,
            channel=channel,
            channel_sku=sku,
            channel_variant_key=key,
            is_active=True,
        )
    )
    await session.flush()


async def test_a_bundle_sold_from_several_pages_is_written_to_all_of_them(
    db_session,
) -> None:
    """The bug this replaced: `.scalars().first()` with no ORDER BY.

    558 of 609 masters hold more than one Rakuten page (measured 2026-10-27), so
    one page got the right availability and the rest kept whatever they had —
    and which one was arbitrary, because nothing ordered the rows.
    """
    parent, _ = await _bundle(db_session, "N21gold", [("N23gold", 7), ("N32gold", 9)])
    await _map(db_session, parent.id, "rakuten", "10087", "10087-gold")
    await _map(db_session, parent.id, "rakuten", "10090", "10090-gold")
    await _map(db_session, parent.id, "rakuten", "10090", "10090-gold-L")

    fake = _RakutenFake()
    attempts = await BundlePushService(db_session).push_bundles(
        fake, [parent.id], triggered_by="test"
    )

    assert fake.calls == [
        ("10087", "10087-gold", 7),
        ("10090", "10090-gold", 7),
        ("10090", "10090-gold-L", 7),
    ]
    assert len(attempts) == 3
    assert {a.status for a in attempts} == {"succeeded"}


async def test_the_order_is_fixed_so_a_repeated_push_is_a_repeat(db_session) -> None:
    """Insertion order must not decide it, or two runs of the same job write the
    same numbers in a different sequence and their logs cannot be compared."""
    parent, _ = await _bundle(db_session, "N21gold", [("N23gold", 4)])
    for sku, key in (("10090", "b"), ("10087", "z"), ("10090", "a")):
        await _map(db_session, parent.id, "rakuten", sku, key)

    fake = _RakutenFake()
    await BundlePushService(db_session).push_bundles(fake, [parent.id], triggered_by="t")

    assert [(sku, key) for sku, key, _ in fake.calls] == [
        ("10087", "z"),
        ("10090", "a"),
        ("10090", "b"),
    ]


async def test_the_variant_key_reaches_the_adapter(db_session) -> None:
    """Before this was threaded through `PushRequest`, every Rakuten push raised
    ValueError and became a failed sync_attempts row plus a Slack error — so no
    Rakuten stock write had ever succeeded through this service."""
    parent, _ = await _bundle(db_session, "N21gold", [("N23gold", 3)])
    await _map(db_session, parent.id, "rakuten", "10087", "10087-gold-S")

    fake = _RakutenFake()
    (attempt,) = await BundlePushService(db_session).push_bundles(
        fake, [parent.id], triggered_by="t"
    )

    assert fake.calls == [("10087", "10087-gold-S", 3)]
    assert attempt.status == "succeeded"
    # Recorded, so the sync-errors retry can rebuild the same address.
    assert attempt.payload["variant_key"] == "10087-gold-S"


async def test_a_shopify_push_is_not_handed_a_variant_key(db_session) -> None:
    """Shopify's `push_inventory` has no such parameter, so passing one
    unconditionally would be a TypeError on every Shopify push."""
    parent, _ = await _bundle(db_session, "N21gold", [("N23gold", 6)], shopify_sku="N21gold")

    fake = _FakeAdapter()  # its push_inventory takes (sku, quantity) only
    (attempt,) = await BundlePushService(db_session).push_bundles(
        fake, [parent.id], triggered_by="t"
    )

    assert fake.calls == [("N21gold", 6)]
    assert attempt.status == "succeeded"
    assert attempt.payload["variant_key"] is None


async def test_an_inactive_mapping_is_not_written_to(db_session) -> None:
    parent, _ = await _bundle(db_session, "N21gold", [("N23gold", 5)])
    await _map(db_session, parent.id, "rakuten", "10087", "10087-gold")
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=parent.id,
            channel="rakuten",
            channel_sku="10099",
            channel_variant_key="10099-gold",
            is_active=False,
        )
    )
    await db_session.flush()

    fake = _RakutenFake()
    await BundlePushService(db_session).push_bundles(fake, [parent.id], triggered_by="t")

    assert [sku for sku, _, _ in fake.calls] == ["10087"]


async def test_a_dry_run_lists_every_page_and_writes_none(db_session) -> None:
    parent, _ = await _bundle(db_session, "N21gold", [("N23gold", 5)])
    await _map(db_session, parent.id, "rakuten", "10087", "10087-gold")
    await _map(db_session, parent.id, "rakuten", "10090", "10090-gold")

    fake = _RakutenFake()
    attempts = await BundlePushService(db_session).push_bundles(
        fake, [parent.id], triggered_by="t", dry_run=True
    )

    assert fake.calls == []
    assert attempts == []


async def test_channel_targets_answers_per_channel(db_session) -> None:
    """One master, two channels, different grains: Rakuten's page plus variant,
    Shopify's bare SKU."""
    parent, _ = await _bundle(db_session, "N21gold", [("N23gold", 1)])
    await _map(db_session, parent.id, "rakuten", "10087", "10087-gold")
    await _map(db_session, parent.id, "shopify", "N21gold", None)

    rakuten = await channel_targets(db_session, master_sku_id=parent.id, channel="rakuten")
    shopify = await channel_targets(db_session, master_sku_id=parent.id, channel="shopify")

    assert [(t.channel_sku, t.variant_key) for t in rakuten] == [("10087", "10087-gold")]
    assert [(t.channel_sku, t.variant_key) for t in shopify] == [("N21gold", None)]
    assert rakuten[0].label == "10087/10087-gold"
    assert shopify[0].label == "N21gold"


async def test_a_master_with_no_mapping_has_no_targets(db_session) -> None:
    parent, _ = await _bundle(db_session, "N21gold", [("N23gold", 1)])

    assert await channel_targets(db_session, master_sku_id=parent.id, channel="rakuten") == []
