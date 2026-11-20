"""Integration tests — pause_stock_push: holding automatic channel writes.

The client asked to set the Christmas sets' Shopify quantities by hand for the
selling period (2026-11-19). These cover the CLI that takes and lifts that hold,
and the properties the hold has to have for the arrangement to be safe:

* report mode writes nothing, because the operator reviews it before applying
* a second run does not refresh "paused since", or a hold left on past its
  season becomes invisible
* lifting it restores the master to the push population exactly
* the set still deducts its components while held, which is the whole point
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.pause_stock_push import DEFAULT_REASON, read_csv, run, show_paused
from app.models import BundleComponent, MasterSku
from app.services import BundlePushService, InventoryService

pytestmark = pytest.mark.integration

# The real registration file's shape: `#` preamble, a Japanese header, one row
# per COMPONENT so each set appears more than once, and columns this CLI has no
# use for. Reading it directly is the point — it is the file that defines the 48.
GIFT_SET_CSV = """# ギフトセット登録用ファイル(Shopify分)
# 1行 = セット1つの構成品1点
セットSKU,セット商品名,構成品SKU,数量,ShopifySKU,楽天商品管理番号,楽天SKU
01gift-pair,01 X'mas Gift Set,B66silverS,2,01gift-pair,,
02gift-duo,02 X'mas Gift Set,B66silverS,1,02gift-duo,,
02gift-duo,02 X'mas Gift Set,B67silverS,1,02gift-duo,,
"""


def _csv(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    path = tmp_path / "gift_sets.csv"
    path.write_bytes(body.encode("utf-8-sig"))
    return path


async def _seed_set(factory, set_code: str, component_code: str) -> int:
    """A bundle parent with one stocked component, as the registration leaves it."""
    async with factory() as session, session.begin():
        component = (
            await session.execute(select(MasterSku).where(MasterSku.sku_code == component_code))
        ).scalar_one_or_none()
        if component is None:
            component = MasterSku(sku_code=component_code, name=component_code, attributes={})
            session.add(component)
            await session.flush()
            await InventoryService(session).manual_adjust(
                master_sku_id=component.id, quantity_delta=9, reason="seed", operator="t"
            )
        parent = MasterSku(sku_code=set_code, name=set_code, is_bundle=True, attributes={})
        session.add(parent)
        await session.flush()
        session.add(
            BundleComponent(
                bundle_master_sku_id=parent.id,
                component_master_sku_id=component.id,
                quantity_per=1,
            )
        )
        return parent.id


async def _master(factory, master_id: int) -> MasterSku:
    async with factory() as session:
        return (
            await session.execute(select(MasterSku).where(MasterSku.id == master_id))
        ).scalar_one()


def test_read_csv_takes_the_registration_file_as_it_is(tmp_path) -> None:
    """No second file to maintain, and no chance of the two disagreeing.

    A hand-made list of 48 codes beside the file that defines them is one more
    thing to keep in step, and the way a set gets held and its twin does not.
    """
    assert read_csv(_csv(tmp_path, GIFT_SET_CSV)) == ["01gift-pair", "02gift-duo"]


def test_read_csv_refuses_a_file_with_an_unreadable_row(tmp_path) -> None:
    """`iter_rows` would skip it in silence; pausing 47 of 48 sets must not pass
    as success, because nothing else in the system would ever mention the 48th."""
    broken = GIFT_SET_CSV + ",03 X'mas Gift Set,B68silverS,1,,,\n"
    with pytest.raises(ValueError, match="読み取れない行"):
        read_csv(_csv(tmp_path, broken))


async def test_report_mode_writes_nothing(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    set_id = await _seed_set(factory, "01gift-pair", "B66silverS")

    assert await run(sku_codes=["01gift-pair"], apply=False, session_factory=factory) == 0

    assert (await _master(factory, set_id)).stock_push_paused_at is None
    async with factory() as session:
        assert set_id in await BundlePushService(session).all_bundle_ids()


async def test_apply_holds_the_set_out_of_the_hourly_push(_test_engine, tmp_path) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    held = await _seed_set(factory, "01gift-pair", "B66silverS")
    other = await _seed_set(factory, "02gift-duo", "B67silverS")

    assert (
        await run(
            sku_codes=read_csv(_csv(tmp_path, GIFT_SET_CSV))[:1],
            apply=True,
            session_factory=factory,
        )
        == 0
    )

    master = await _master(factory, held)
    assert master.stock_push_paused_at is not None
    assert master.stock_push_paused_reason == DEFAULT_REASON
    async with factory() as session:
        ids = await BundlePushService(session).all_bundle_ids()
    assert held not in ids
    assert other in ids  # a hold is per master, never a mode the job runs in


async def test_a_held_set_still_deducts_its_components(_test_engine) -> None:
    """The requirement the hold exists to protect.

    `mark_non_inventory` looks like the same job and would fail here:
    `resolve_consumption` tests `is_stock_managed` on the parent and returns [],
    so the shop would sell sets all season while the bracelets stayed in stock
    on paper. A pause must leave this untouched.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    set_id = await _seed_set(factory, "01gift-pair", "B66silverS")
    await run(sku_codes=["01gift-pair"], apply=True, session_factory=factory)

    async with factory() as session:
        consumption = await InventoryService(session).resolve_consumption(set_id)

    assert len(consumption) == 1
    assert consumption[0][1] == 1


async def test_a_second_run_does_not_reset_paused_since(_test_engine) -> None:
    """Re-running the command is how an operator checks it took effect.

    If that refreshed the timestamp, "held since November" would read as "held
    since today" for ever, and the one signal that a hold outlived its season
    would never fire.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    set_id = await _seed_set(factory, "01gift-pair", "B66silverS")
    await run(sku_codes=["01gift-pair"], apply=True, session_factory=factory)
    first = (await _master(factory, set_id)).stock_push_paused_at

    await run(sku_codes=["01gift-pair"], apply=True, reason="別の理由", session_factory=factory)

    master = await _master(factory, set_id)
    assert master.stock_push_paused_at == first
    assert master.stock_push_paused_reason == DEFAULT_REASON


async def test_resume_returns_the_set_to_the_push(_test_engine) -> None:
    """January. The lever has to work in both directions or it is a trap."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    set_id = await _seed_set(factory, "01gift-pair", "B66silverS")
    await run(sku_codes=["01gift-pair"], apply=True, session_factory=factory)

    await run(sku_codes=["01gift-pair"], apply=True, resume=True, session_factory=factory)

    master = await _master(factory, set_id)
    assert master.stock_push_paused_at is None
    assert master.stock_push_paused_reason is None
    async with factory() as session:
        assert set_id in await BundlePushService(session).all_bundle_ids()


async def test_resume_in_report_mode_writes_nothing(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    set_id = await _seed_set(factory, "01gift-pair", "B66silverS")
    await run(sku_codes=["01gift-pair"], apply=True, session_factory=factory)

    await run(sku_codes=["01gift-pair"], apply=False, resume=True, session_factory=factory)

    assert (await _master(factory, set_id)).stock_push_paused_at is not None


async def test_an_unknown_sku_is_reported_as_a_failure(_test_engine) -> None:
    """Exit 1, like every other CLI here: a typo in a set code must not look like
    a successful hold, or that set keeps being overwritten hourly in silence."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_set(factory, "01gift-pair", "B66silverS")

    code = await run(sku_codes=["01gift-pair", "99gift-typo"], apply=True, session_factory=factory)

    assert code == 1
    # The ones that DO exist are still held: the hold is the safe direction, and
    # refusing all 48 over one typo would leave the client's numbers exposed.
    async with factory() as session:
        held = (
            await session.execute(
                select(MasterSku.sku_code).where(MasterSku.stock_push_paused_at.is_not(None))
            )
        ).scalars()
    assert "01gift-pair" in set(held)


async def test_list_reports_what_is_currently_held(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_set(factory, "01gift-pair", "B66silverS")
    await run(sku_codes=["01gift-pair"], apply=True, session_factory=factory)

    assert await show_paused(session_factory=factory) == 0
