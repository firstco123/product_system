"""Integration — create_gift_sets writes real rows, and the sale then fans out.

The unit tests answer `plan`'s reads with a fake session, so the SQL itself is
unexercised there. These run it against a real database and then follow the set
through to the thing it exists for: an order for one gift set taking two items
off the shelf.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.create_gift_sets import run
from app.models import BundleComponent, ChannelSkuMapping, InventorySnapshot, MasterSku
from app.services.inventory import InventoryService
from app.services.mapping import resolve_master_sku_id

pytestmark = pytest.mark.integration

HEADER = "セットSKU,セット商品名,構成品SKU,数量\n"
#: The same file with the optional channel columns the mappings are written
#: from. Rakuten takes TWO: 商品管理番号 is the page and 楽天SKU the SKU管理番号
#: inside it, and `channel_sku_mappings` is keyed on the pair.
HEADER_CHANNELS = "セットSKU,セット商品名,構成品SKU,数量,ShopifySKU,楽天商品管理番号,楽天SKU\n"


def _csv(
    tmp_path: pathlib.Path,
    body: str,
    name: str = "sets.csv",
    header: str = HEADER,
) -> pathlib.Path:
    path = tmp_path / name
    path.write_bytes((header + body).encode("utf-8-sig"))
    return path


async def _seed(factory, code: str, qty: int | None = 0) -> int:
    async with factory() as session, session.begin():
        master = MasterSku(sku_code=code, name=code, attributes={})
        session.add(master)
        await session.flush()
        if qty is not None:
            session.add(InventorySnapshot(master_sku_id=master.id, on_hand_qty=qty))
        return int(master.id)


async def _components(factory, set_code: str) -> dict[str, int]:
    """The set's components as {component sku_code: quantity_per}."""
    async with factory() as session:
        parent = (
            await session.execute(select(MasterSku.id).where(MasterSku.sku_code == set_code))
        ).scalar_one()
        rows = await session.execute(
            select(MasterSku.sku_code, BundleComponent.quantity_per)
            .join(BundleComponent, BundleComponent.component_master_sku_id == MasterSku.id)
            .where(BundleComponent.bundle_master_sku_id == parent)
        )
        return {code: qty for code, qty in rows.all()}  # noqa: C416


async def test_a_two_product_set_is_written_and_marked_as_a_bundle(_test_engine, tmp_path) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSb34")
    await _seed(factory, "GSb66")
    body = "GSsetA,B34とB66のセット,GSb34,1\nGSsetA,B34とB66のセット,GSb66,1\n"

    assert await run(path=_csv(tmp_path, body), apply_changes=True, session_factory=factory) == 0

    assert await _components(factory, "GSsetA") == {"GSb34": 1, "GSb66": 1}
    async with factory() as session:
        assert (
            await session.scalar(select(MasterSku.is_bundle).where(MasterSku.sku_code == "GSsetA"))
        ) is True


async def test_a_dry_run_writes_nothing(_test_engine, tmp_path) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSdryA")
    body = "GSsetDry,B66 2個セット,GSdryA,2\n"

    assert await run(path=_csv(tmp_path, body), apply_changes=False, session_factory=factory) == 0

    async with factory() as session:
        assert (
            await session.scalar(select(MasterSku.id).where(MasterSku.sku_code == "GSsetDry"))
        ) is None


async def test_a_second_run_does_not_duplicate_the_components(_test_engine, tmp_path) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSrepeat")
    body = "GSsetRepeat,B66 2個セット,GSrepeat,2\n"
    csv = _csv(tmp_path, body)

    assert await run(path=csv, apply_changes=True, session_factory=factory) == 0
    assert await run(path=csv, apply_changes=True, session_factory=factory) == 0

    assert await _components(factory, "GSsetRepeat") == {"GSrepeat": 2}


async def test_one_bad_row_leaves_the_whole_file_unwritten(_test_engine, tmp_path) -> None:
    """The partial apply is the dangerous outcome: the good set would fan out
    while the broken one silently moved nothing."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSpartial")
    body = "GSsetGood,良いセット,GSpartial,1\nGSsetBad,壊れた行,GSmissing,1\n"

    assert await run(path=_csv(tmp_path, body), apply_changes=True, session_factory=factory) == 1

    async with factory() as session:
        assert (
            await session.scalar(select(MasterSku.id).where(MasterSku.sku_code == "GSsetGood"))
        ) is None


async def test_selling_one_set_consumes_both_components(_test_engine, tmp_path) -> None:
    """The whole point. One order line for the set, two items off the shelf."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSfanA", qty=10)
    await _seed(factory, "GSfanB", qty=10)
    body = "GSsetFan,セット,GSfanA,1\nGSsetFan,セット,GSfanB,2\n"
    assert await run(path=_csv(tmp_path, body), apply_changes=True, session_factory=factory) == 0

    async with factory() as session:
        set_id = (
            await session.execute(select(MasterSku.id).where(MasterSku.sku_code == "GSsetFan"))
        ).scalar_one()
        resolved = await InventoryService(session).resolve_consumption(set_id)

    by_id = dict(resolved)
    async with factory() as session:
        codes = await session.execute(
            select(MasterSku.id, MasterSku.sku_code).where(MasterSku.id.in_(by_id))
        )
        per_code = {code: by_id[mid] for mid, code in codes.all()}

    assert per_code == {"GSfanA": 1, "GSfanB": 2}


async def _set_id(factory, code: str) -> int:
    async with factory() as session:
        return int(
            (
                await session.execute(select(MasterSku.id).where(MasterSku.sku_code == code))
            ).scalar_one()
        )


async def test_a_registered_set_resolves_from_both_channels(_test_engine, tmp_path) -> None:
    """The mapping rows are the half the sale needs: without them a Shopify or
    Rakuten order line for the set has nothing to resolve to, and the order
    parks in pending_mapping however correct the components are.

    Resolved exactly as `app.services.ingest` resolves a live line: Shopify by
    the SKU alone, Rakuten by the (商品管理番号, SKU管理番号) PAIR, because that
    pair is what `RakutenAdapter._to_normalized` puts on the line. A lookup that
    passes the 楽天SKU as `channel_sku` is a query the order path never makes,
    which is how a mapping written at the wrong grain read as correct here.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSmapA")
    await _seed(factory, "GSmapB")
    body = (
        "GSsetMap,セット,GSmapA,1,GSsetMap,gs-page-1,202612gs-1\n"
        "GSsetMap,セット,GSmapB,2,GSsetMap,gs-page-1,202612gs-1\n"
    )

    assert (
        await run(
            path=_csv(tmp_path, body, header=HEADER_CHANNELS),
            apply_changes=True,
            session_factory=factory,
        )
        == 0
    )

    set_id = await _set_id(factory, "GSsetMap")
    async with factory() as session:
        assert (
            await resolve_master_sku_id(session, channel="shopify", channel_sku="GSsetMap")
        ) == set_id
        assert (
            await resolve_master_sku_id(
                session,
                channel="rakuten",
                channel_sku="gs-page-1",
                variant_key="202612gs-1",
            )
        ) == set_id
        rows = await session.execute(
            select(
                ChannelSkuMapping.channel,
                ChannelSkuMapping.channel_sku,
                ChannelSkuMapping.channel_variant_key,
                ChannelSkuMapping.is_active,
            ).where(ChannelSkuMapping.master_sku_id == set_id)
        )
        assert sorted(rows.all()) == [
            ("rakuten", "gs-page-1", "202612gs-1", True),
            ("shopify", "GSsetMap", None, True),
        ]


async def test_a_blank_channel_column_writes_no_mapping_for_that_channel(
    _test_engine, tmp_path
) -> None:
    """Blank is 「このチャネルでは売らない」, not the セットSKU by default."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSblankA")
    body = "GSsetBlank,セット,GSblankA,1,,gs-page-2,202612gs-2\n"

    assert (
        await run(
            path=_csv(tmp_path, body, header=HEADER_CHANNELS),
            apply_changes=True,
            session_factory=factory,
        )
        == 0
    )

    set_id = await _set_id(factory, "GSsetBlank")
    async with factory() as session:
        rows = await session.execute(
            select(ChannelSkuMapping.channel, ChannelSkuMapping.channel_variant_key).where(
                ChannelSkuMapping.master_sku_id == set_id
            )
        )
        # A rakuten row this CLI writes always carries its SKU管理番号. A NULL
        # key here would be a page-level wildcard, which the module docstring
        # explains is the worse half of the pair to get wrong.
        assert rows.all() == [("rakuten", "202612gs-2")]
        assert (
            await resolve_master_sku_id(session, channel="shopify", channel_sku="GSsetBlank")
        ) is None


async def test_a_rerun_adds_a_mapping_the_components_check_used_to_hide(
    _test_engine, tmp_path
) -> None:
    """The half-finished session: components written, mapping not. The old
    already_registered flag dropped such a set entirely, so no re-run could ever
    put the missing mapping back."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSreA")
    body = "GSsetRe,セット,GSreA,2,GSsetRe,gs-page-3,202612gs-3\n"
    path = _csv(tmp_path, body, header=HEADER_CHANNELS)

    assert await run(path=path, apply_changes=True, session_factory=factory) == 0
    set_id = await _set_id(factory, "GSsetRe")
    async with factory() as session, session.begin():
        await session.execute(
            delete(ChannelSkuMapping).where(
                ChannelSkuMapping.master_sku_id == set_id,
                ChannelSkuMapping.channel == "shopify",
            )
        )

    assert await run(path=path, apply_changes=True, session_factory=factory) == 0

    async with factory() as session:
        assert (
            await resolve_master_sku_id(session, channel="shopify", channel_sku="GSsetRe")
        ) == set_id
    # The components were not written a second time: the UNIQUE constraint
    # would have refused the run above outright.
    assert await _components(factory, "GSsetRe") == {"GSreA": 2}


async def test_no_rakuten_row_this_cli_writes_has_a_null_variant_key(
    _test_engine, tmp_path
) -> None:
    """Swept across every rakuten row the CLI can produce, not just one.

    A NULL `channel_variant_key` on a rakuten row is a page-level WILDCARD:
    `resolve_master_sku_id`'s second step hands it every unmapped SKU on that
    商品管理番号, so the set would absorb the sales of everything else sold from
    the page. `push_safety.screen_targets` would also drop the target, so the
    availability push would go silent. Both halves of the pair are mandatory
    precisely so this row cannot be created here.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSnullA")
    await _seed(factory, "GSnullB")
    body = (
        "GSsetNull1,セット1,GSnullA,1,GSsetNull1,gs-page-9,202612gs-n1\n"
        "GSsetNull2,セット2,GSnullB,2,,gs-page-9,202612gs-n2\n"
    )

    assert (
        await run(
            path=_csv(tmp_path, body, header=HEADER_CHANNELS),
            apply_changes=True,
            session_factory=factory,
        )
        == 0
    )

    async with factory() as session:
        rows = await session.execute(
            select(ChannelSkuMapping.channel_sku).where(
                ChannelSkuMapping.channel == "rakuten",
                ChannelSkuMapping.channel_sku == "gs-page-9",
                ChannelSkuMapping.channel_variant_key.is_(None),
            )
        )
        assert rows.all() == []
        # And the two sets really do share the one page, which is why the
        # uniqueness checks have to key on the pair rather than on the page.
        held = await session.execute(
            select(ChannelSkuMapping.channel_variant_key)
            .where(ChannelSkuMapping.channel_sku == "gs-page-9")
            .order_by(ChannelSkuMapping.channel_variant_key)
        )
        assert [k for (k,) in held.all()] == ["202612gs-n1", "202612gs-n2"]


async def test_an_inactive_mapping_on_this_set_is_reactivated_not_reinserted(
    _test_engine, tmp_path
) -> None:
    """`resolve_master_sku_id` filters `is_active`, so an inactive row leaves the
    set unresolvable — and an INSERT cannot repair it, because that row already
    holds `uq_channel_sku_mapping`. A re-run has to UPDATE it."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSoffA")
    body = "GSsetOff,セット,GSoffA,2,GSsetOff,gs-page-4,202612gs-4\n"
    path = _csv(tmp_path, body, header=HEADER_CHANNELS)

    assert await run(path=path, apply_changes=True, session_factory=factory) == 0
    set_id = await _set_id(factory, "GSsetOff")
    async with factory() as session, session.begin():
        await session.execute(
            update(ChannelSkuMapping)
            .where(ChannelSkuMapping.master_sku_id == set_id)
            .values(is_active=False)
        )

    assert await run(path=path, apply_changes=True, session_factory=factory) == 0

    async with factory() as session:
        rows = await session.execute(
            select(ChannelSkuMapping.channel, ChannelSkuMapping.is_active)
            .where(ChannelSkuMapping.master_sku_id == set_id)
            .order_by(ChannelSkuMapping.channel)
        )
        # Two rows, not four: reactivated in place rather than inserted beside.
        assert rows.all() == [("rakuten", True), ("shopify", True)]
        assert (
            await resolve_master_sku_id(
                session,
                channel="rakuten",
                channel_sku="gs-page-4",
                variant_key="202612gs-4",
            )
        ) == set_id


async def test_a_half_filled_rakuten_pair_leaves_the_file_unwritten(_test_engine, tmp_path) -> None:
    """The 商品管理番号 is not in the client's correspondence table and has to be
    asked for, so the half-filled file is the one that will actually arrive."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GShalfA")
    body = "GSsetHalf,セット,GShalfA,1,GSsetHalf,,202612gs-5\n"

    assert (
        await run(
            path=_csv(tmp_path, body, header=HEADER_CHANNELS),
            apply_changes=True,
            session_factory=factory,
        )
        == 1
    )

    async with factory() as session:
        assert (
            await session.scalar(select(MasterSku.id).where(MasterSku.sku_code == "GSsetHalf"))
        ) is None


async def test_a_row_that_lost_its_trailing_fields_leaves_the_file_unwritten(
    _test_engine, tmp_path
) -> None:
    """`csv_intake` drops such a row in silence, so the set used to register with
    a component missing — every sale under-deducting, nothing on screen."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed(factory, "GSragA")
    await _seed(factory, "GSragB")
    body = "GSsetRag,セット,GSragA,1,GSsetRag,,\nGSsetRag,セット,GSragB,1,GSsetRag\n"

    assert (
        await run(
            path=_csv(tmp_path, body, header=HEADER_CHANNELS),
            apply_changes=True,
            session_factory=factory,
        )
        == 1
    )

    async with factory() as session:
        assert (
            await session.scalar(select(MasterSku.id).where(MasterSku.sku_code == "GSsetRag"))
        ) is None


async def test_a_channel_sku_belonging_to_another_product_leaves_the_file_unwritten(
    _test_engine, tmp_path
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    other = await _seed(factory, "GSheldA")
    async with factory() as session, session.begin():
        session.add(
            ChannelSkuMapping(
                master_sku_id=other,
                channel="shopify",
                channel_sku="GSsetHeld",
                is_active=True,
            )
        )
    body = "GSsetHeld,セット,GSheldA,1,GSsetHeld,,\n"

    assert (
        await run(
            path=_csv(tmp_path, body, header=HEADER_CHANNELS),
            apply_changes=True,
            session_factory=factory,
        )
        == 1
    )

    async with factory() as session:
        assert (
            await session.scalar(select(MasterSku.id).where(MasterSku.sku_code == "GSsetHeld"))
        ) is None
