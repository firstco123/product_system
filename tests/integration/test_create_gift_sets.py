"""Integration — create_gift_sets writes real rows, and the sale then fans out.

The unit tests answer `plan`'s reads with a fake session, so the SQL itself is
unexercised there. These run it against a real database and then follow the set
through to the thing it exists for: an order for one gift set taking two items
off the shelf.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.create_gift_sets import run
from app.models import BundleComponent, InventorySnapshot, MasterSku
from app.services.inventory import InventoryService

pytestmark = pytest.mark.integration

HEADER = "セットSKU,セット商品名,構成品SKU,数量\n"


def _csv(tmp_path: pathlib.Path, body: str, name: str = "sets.csv") -> pathlib.Path:
    path = tmp_path / name
    path.write_bytes((HEADER + body).encode("utf-8-sig"))
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
