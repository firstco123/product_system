"""Integration tests — applying the reviewed wholesale correspondence table.

The CLI's `run` takes the file's BYTES, so these build the reviewed CSV inline
instead of writing temp files. What matters here is what reaches
`channel_sku_mappings`, and what is refused before it gets there.
"""

from __future__ import annotations

import csv
import io

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.import_wholesale_mappings import run, wants_apply
from app.models import ChannelSkuMapping, MasterSku
from app.services.mapping import resolve_master_sku_id
from app.services.wholesale_import import WHOLESALE_CHANNEL
from app.wholesale_catalogue import GIFT_BAG_CODE

pytestmark = pytest.mark.integration


@pytest.fixture
def factory(_test_engine):
    """The CLI opens its own session, so it has to be handed the test one."""
    return async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)


BUYER = "0393"
COLUMNS = (
    "商品コード",
    "発注者コード",
    "規格カナ",
    "kind",
    "sku_code",
    "apply",
    "candidates",
    "note",
)


def reviewed_csv(*rows: dict[str, str]) -> bytes:
    """The proposal file as a reviewer hands it back — Excel writes a BOM."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([row.get(name, "") for name in COLUMNS])
    return buffer.getvalue().encode("utf-8-sig")


async def _master(factory, sku_code: str) -> int:
    async with factory() as session, session.begin():
        master = MasterSku(sku_code=sku_code, name=sku_code, attributes={})
        session.add(master)
        await session.flush()
        return master.id


async def _mappings(factory) -> list[ChannelSkuMapping]:
    async with factory() as session:
        rows = await session.execute(
            select(ChannelSkuMapping)
            .where(ChannelSkuMapping.channel == WHOLESALE_CHANNEL)
            .order_by(ChannelSkuMapping.channel_sku)
        )
        return list(rows.scalars().all())


@pytest.mark.parametrize("value", ["yes", "Yes", "Y", "true", "1", "はい", "○"])
def test_the_spellings_of_yes_a_reviewer_might_use(value: str) -> None:
    assert wants_apply(value)


@pytest.mark.parametrize("value", ["", " ", "no", "n", "後で", "maybe"])
def test_anything_that_is_not_yes_is_not_applied(value: str) -> None:
    """A blank `apply` is a row nobody has decided on. It must not read as yes."""
    assert not wants_apply(value)


async def test_only_the_rows_marked_yes_become_mappings(factory) -> None:
    await _master(factory, "B40gold")
    await _master(factory, "R63us5")
    data = reviewed_csv(
        {
            "商品コード": "2700002430758",
            "発注者コード": BUYER,
            "kind": "exact",
            "sku_code": "B40gold",
            "apply": "yes",
        },
        # The colourless match the proposal leaves for a person to judge.
        {
            "商品コード": "2800000048723",
            "発注者コード": BUYER,
            "kind": "colourless",
            "sku_code": "R63us5",
            "apply": "no",
        },
    )

    assert await run(data=data, dry_run=False, session_factory=factory) == 0

    rows = await _mappings(factory)
    assert [row.channel_sku for row in rows] == ["2700002430758"]
    assert rows[0].marketplace_id == BUYER
    # The carrier's 13-digit code IS the sellable unit, so there is no second
    # grain — unlike Rakuten, where the page carries several SKUs.
    assert rows[0].channel_variant_key is None
    assert rows[0].is_active


async def test_the_written_mapping_actually_resolves(factory) -> None:
    """The point of the whole table: the import path can find the master."""
    master = await _master(factory, "B40gold")
    data = reviewed_csv(
        {
            "商品コード": "2700002430758",
            "発注者コード": BUYER,
            "sku_code": "B40gold",
            "apply": "yes",
        }
    )

    await run(data=data, dry_run=False, session_factory=factory)

    async with factory() as session:
        assert (
            await resolve_master_sku_id(
                session,
                channel=WHOLESALE_CHANNEL,
                channel_sku="2700002430758",
                marketplace_id=BUYER,
            )
            == master
        )


async def test_a_dry_run_writes_nothing(factory) -> None:
    await _master(factory, "B40gold")
    data = reviewed_csv(
        {
            "商品コード": "2700002430758",
            "発注者コード": BUYER,
            "sku_code": "B40gold",
            "apply": "yes",
        }
    )

    assert await run(data=data, dry_run=True, session_factory=factory) == 0

    assert await _mappings(factory) == []


async def test_applying_twice_leaves_one_row(factory) -> None:
    """Upsert on uq_channel_sku_mapping, so extending the table and re-running
    touches only what is new."""
    await _master(factory, "B40gold")
    data = reviewed_csv(
        {
            "商品コード": "2700002430758",
            "発注者コード": BUYER,
            "sku_code": "B40gold",
            "apply": "yes",
        }
    )

    await run(data=data, dry_run=False, session_factory=factory)
    await run(data=data, dry_run=False, session_factory=factory)

    assert len(await _mappings(factory)) == 1


async def test_a_corrected_table_repoints_the_mapping(factory) -> None:
    """A reviewer who changes their mind re-runs the file; the code must follow
    the new master rather than keeping the first answer."""
    await _master(factory, "B34gold22cm")
    corrected_id = await _master(factory, "B34gold20cm")
    first = reviewed_csv(
        {
            "商品コード": "2700002430437",
            "発注者コード": BUYER,
            "sku_code": "B34gold22cm",
            "apply": "yes",
        }
    )
    corrected_csv = reviewed_csv(
        {
            "商品コード": "2700002430437",
            "発注者コード": BUYER,
            "sku_code": "B34gold20cm",
            "apply": "yes",
        }
    )

    await run(data=first, dry_run=False, session_factory=factory)
    await run(data=corrected_csv, dry_run=False, session_factory=factory)

    (row,) = await _mappings(factory)
    assert row.master_sku_id == corrected_id


async def test_a_table_naming_a_master_we_do_not_have_is_refused_entirely(factory) -> None:
    """Stop on the whole file: a table referring to masters that are gone was
    built against a different catalogue, so even the rows that DO resolve were
    matched under assumptions that no longer hold."""
    await _master(factory, "B40gold")
    data = reviewed_csv(
        {
            "商品コード": "2700002430758",
            "発注者コード": BUYER,
            "sku_code": "B40gold",
            "apply": "yes",
        },
        {
            "商品コード": "2700002430765",
            "発注者コード": BUYER,
            "sku_code": "B40bronze",
            "apply": "yes",
        },
    )

    assert await run(data=data, dry_run=False, session_factory=factory) == 1
    assert await _mappings(factory) == []


async def test_yes_with_no_master_is_refused_rather_than_guessed(factory) -> None:
    data = reviewed_csv(
        {"商品コード": "2700002430758", "発注者コード": BUYER, "sku_code": "", "apply": "yes"}
    )

    assert await run(data=data, dry_run=False, session_factory=factory) == 1
    assert await _mappings(factory) == []


async def test_a_file_with_nothing_marked_yes_is_an_error_not_a_silent_success(
    factory,
) -> None:
    """Running the proposal with every row still `no` means the review has not
    happened. Exit 0 would read as "imported"."""
    data = reviewed_csv(
        {"商品コード": "2700002430758", "発注者コード": BUYER, "sku_code": "x", "apply": "no"}
    )

    assert await run(data=data, dry_run=False, session_factory=factory) == 1


async def test_the_gift_bag_gets_an_unmanaged_master_of_its_own(factory) -> None:
    """docs/29 §5-4: packaging, not a product. Must be 在庫管理対象外 or it climbs
    forever — 20 shipped against 154 returned in the measured period."""
    await _master(factory, "B40gold")
    data = reviewed_csv(
        {
            "商品コード": "2700002430758",
            "発注者コード": BUYER,
            "sku_code": "B40gold",
            "apply": "yes",
        }
    )

    assert (
        await run(data=data, dry_run=False, gift_bag_sku="IT-GIFTBAG", session_factory=factory) == 0
    )

    async with factory() as session:
        created = await session.execute(select(MasterSku).where(MasterSku.sku_code == "IT-GIFTBAG"))
        bag = created.scalar_one()
    assert bag.is_stock_managed is False
    assert bag.non_inventory_kind == "packaging"
    codes = [row.channel_sku for row in await _mappings(factory)]
    assert GIFT_BAG_CODE in codes


async def test_the_gift_bag_flag_refuses_to_take_over_a_stocked_master(factory) -> None:
    """Flipping `is_stock_managed` off would abandon whatever quantity that
    master holds. A stocked master under this code is something else."""
    await _master(factory, "ALREADY-STOCKED")
    data = reviewed_csv(
        {
            "商品コード": "2700002430758",
            "発注者コード": BUYER,
            "sku_code": "ALREADY-STOCKED",
            "apply": "yes",
        }
    )

    with pytest.raises(ValueError, match="在庫管理対象"):
        await run(data=data, dry_run=False, gift_bag_sku="ALREADY-STOCKED", session_factory=factory)


async def test_a_dry_run_does_not_create_the_gift_bag_master(factory) -> None:
    await _master(factory, "B40gold")
    data = reviewed_csv(
        {
            "商品コード": "2700002430758",
            "発注者コード": BUYER,
            "sku_code": "B40gold",
            "apply": "yes",
        }
    )

    assert (
        await run(data=data, dry_run=True, gift_bag_sku="IT-GIFTBAG", session_factory=factory) == 0
    )

    async with factory() as session:
        found = await session.execute(select(MasterSku).where(MasterSku.sku_code == "IT-GIFTBAG"))
        assert found.scalar_one_or_none() is None


async def test_a_reordered_or_renamed_header_still_reads(factory) -> None:
    """The reviewer edits this in Excel. Columns get moved and renamed, so the
    intake spec carries aliases rather than trusting positions."""
    await _master(factory, "B40gold")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["apply", "master_sku", "marketplace_id", "product_code"])
    writer.writerow(["yes", "B40gold", BUYER, "2700002430758"])

    assert (
        await run(
            data=buffer.getvalue().encode("cp932"),
            dry_run=False,
            session_factory=factory,
        )
        == 0
    )

    (row,) = await _mappings(factory)
    assert row.channel_sku == "2700002430758"
    assert row.marketplace_id == BUYER
