"""Integration tests — where the wholesale cutover comes from (docs/29 §7).

This is the guard against the single most expensive mistake available in this
feature: a cutover a month early silently imports the history the client already
sent and leaves stock 3,045 units too high. Every refusal below is cheaper than
that, so the tests are written around what must NOT be importable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.import_wholesale_file import parse_cutover
from app.cli.import_wholesale_file import run as import_file
from app.models import (
    ChannelSkuMapping,
    InventorySnapshot,
    MasterSku,
    Order,
    ReconcileRun,
    ReconcileRunStatusEnum,
    ReconcileRunTypeEnum,
)
from app.services.wholesale_cutover import CUTOVER_FLOOR, check_override, resolve_cutover
from app.services.wholesale_import import WHOLESALE_CHANNEL
from tests.test_wholesale_bms import order_file

pytestmark = pytest.mark.integration

BUYER = "0393"
ORDER_CODE = "2700002430598"


async def _stocktake(
    session,
    *,
    counted_on: date | None,
    status: str = ReconcileRunStatusEnum.APPLIED,
    run_type: str = ReconcileRunTypeEnum.STOCKTAKE,
) -> None:
    session.add(
        ReconcileRun(
            source="test",
            run_type=str(run_type),
            status=str(status),
            triggered_by="test",
            counted_on=counted_on,
            started_at=datetime(2026, 10, 20, tzinfo=UTC),
        )
    )
    await session.flush()


# ------------------------------------------------------------------ derivation


async def test_the_cutover_is_the_applied_stocktakes_count_date(db_session) -> None:
    """docs/29 §7-1: 在庫計算の開始日 = 10月の棚卸実施日. Derived rather than typed,
    so the import and the count cannot disagree about when stock became true."""
    await _stocktake(db_session, counted_on=date(2026, 10, 25))

    resolution = await resolve_cutover(db_session)

    assert resolution.ok
    assert resolution.cutover_on == date(2026, 10, 25)
    assert resolution.counted_on == (date(2026, 10, 25),)


async def test_with_no_applied_stocktake_there_is_no_cutover(db_session) -> None:
    """Today's state. There is no date at which our stock is known to be right,
    so there is no movement that can be added to it — docs/29 §7-2 as a check
    rather than a paragraph."""
    resolution = await resolve_cutover(db_session)

    assert not resolution.ok
    assert resolution.cutover_on is None
    assert "棚卸" in resolution.reason


async def test_an_unapplied_stocktake_does_not_count(db_session) -> None:
    """A count still awaiting approval has not set anything. Treating it as the
    cutover would start wholesale from a figure nobody has confirmed."""
    await _stocktake(
        db_session,
        counted_on=date(2026, 10, 25),
        status=ReconcileRunStatusEnum.PENDING_APPROVAL,
    )

    assert not (await resolve_cutover(db_session)).ok


async def test_a_reconcile_run_is_not_a_stocktake(db_session) -> None:
    """`reconcile_runs` holds three kinds of run since migration 0012. A CROSS
    MALL comparison is not a physical count and cannot date the cutover."""
    await _stocktake(
        db_session,
        counted_on=date(2026, 10, 25),
        run_type=ReconcileRunTypeEnum.RECONCILE,
    )

    assert not (await resolve_cutover(db_session)).ok


async def test_a_stocktake_with_no_count_date_is_ignored(db_session) -> None:
    await _stocktake(db_session, counted_on=None)

    assert not (await resolve_cutover(db_session)).ok


async def test_counts_on_several_days_refuse_to_pick_one(db_session) -> None:
    """The case docs/29 does not cover, and cannot be resolved here.

    A SKU counted early needs the wholesale movement from ITS date onward; a SKU
    counted late already has that movement inside its count. One global date is
    wrong for one of them either way, and which way to lean is an operational
    call — so it is surfaced, not guessed.
    """
    await _stocktake(db_session, counted_on=date(2026, 10, 20))
    await _stocktake(db_session, counted_on=date(2026, 11, 5))

    resolution = await resolve_cutover(db_session)

    assert not resolution.ok
    assert resolution.counted_on == (date(2026, 10, 20), date(2026, 11, 5))
    assert "複数日" in resolution.reason
    assert "--cutover" in resolution.reason


async def test_the_same_day_counted_in_two_instalments_is_one_date(db_session) -> None:
    """The client counts by category, so several runs on one day are normal and
    are not the ambiguous case."""
    await _stocktake(db_session, counted_on=date(2026, 10, 25))
    await _stocktake(db_session, counted_on=date(2026, 10, 25))

    resolution = await resolve_cutover(db_session)

    assert resolution.ok
    assert resolution.cutover_on == date(2026, 10, 25)


async def test_a_stocktake_before_october_is_refused(db_session) -> None:
    """The agreement names 10月の棚卸実施日, so an earlier count is a different
    count — most likely one of the Phase 1-B runs."""
    await _stocktake(db_session, counted_on=date(2026, 7, 16))

    resolution = await resolve_cutover(db_session)

    assert not resolution.ok
    assert "2026-10-01" in resolution.reason


# -------------------------------------------------------------------- override


@pytest.mark.parametrize(
    "day",
    [date(2026, 6, 30), date(2026, 8, 16), date(2026, 9, 30)],
)
def test_an_override_before_october_is_refused(day: date) -> None:
    """These are the dates the supplied history actually covers: 受注 from
    8/16, 返品 from 6/30. Any of them pulls in the 3,045 units."""
    refusal = check_override(day)

    assert refusal is not None
    assert "3,045" in refusal


@pytest.mark.parametrize("day", [CUTOVER_FLOOR, date(2026, 10, 25), date(2026, 11, 16)])
def test_an_override_from_october_onward_is_allowed(day: date) -> None:
    """The multi-date stocktake case needs an override, so it must be possible."""
    assert check_override(day) is None


def test_the_cutover_argument_is_an_iso_date() -> None:
    assert parse_cutover("2026-10-25") == date(2026, 10, 25)
    with pytest.raises(ValueError):
        parse_cutover("2026/10/25")


# ------------------------------------------------------------ through the CLI


@pytest.fixture
def factory(_test_engine):
    return async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)


async def _seed_mapped_master(factory) -> int:
    async with factory() as session, session.begin():
        master = MasterSku(sku_code="N63-GD", name="N63-GD", attributes={})
        session.add(master)
        await session.flush()
        session.add(InventorySnapshot(master_sku_id=master.id, on_hand_qty=10))
        session.add(
            ChannelSkuMapping(
                master_sku_id=master.id,
                channel=WHOLESALE_CHANNEL,
                channel_sku=ORDER_CODE,
                marketplace_id=BUYER,
                is_active=True,
            )
        )
        return master.id


async def _orders(factory) -> list[Order]:
    async with factory() as session:
        rows = await session.execute(
            Order.__table__.select().where(Order.channel == WHOLESALE_CHANNEL)
        )
        return list(rows.all())


async def test_the_cli_refuses_to_import_before_any_stocktake(factory) -> None:
    """The whole point: with no stocktake on record, a wholesale file cannot be
    imported at all, however it is invoked."""
    await _seed_mapped_master(factory)

    code = await import_file(
        data=order_file(),
        dry_run=False,
        operator="t",
        session_factory=factory,
    )

    assert code == 1
    assert await _orders(factory) == []


async def test_the_cli_refuses_an_early_override_before_reading_the_database(
    factory,
) -> None:
    await _seed_mapped_master(factory)

    code = await import_file(
        data=order_file(),
        dry_run=False,
        operator="t",
        cutover_on=date(2026, 6, 1),
        session_factory=factory,
    )

    assert code == 1
    assert await _orders(factory) == []


async def test_the_cli_imports_once_a_stocktake_dates_the_cutover(factory) -> None:
    await _seed_mapped_master(factory)
    async with factory() as session, session.begin():
        await _stocktake(session, counted_on=date(2026, 10, 1))

    # The sample voucher is dated 2026-09-14, so a 10-01 cutover excludes it:
    # the derived date is doing its job rather than the import failing.
    assert (
        await import_file(
            data=order_file(),
            dry_run=False,
            operator="t",
            session_factory=factory,
        )
        == 0
    )
    assert await _orders(factory) == []

    # A voucher dated after the cutover imports, with no override involved.
    assert (
        await import_file(
            data=order_file({"発注日": "20261116"}),
            dry_run=False,
            operator="t",
            session_factory=factory,
        )
        == 0
    )
    assert len(await _orders(factory)) == 1


async def test_a_dry_run_writes_nothing_even_with_a_valid_cutover(factory) -> None:
    await _seed_mapped_master(factory)
    async with factory() as session, session.begin():
        await _stocktake(session, counted_on=date(2026, 10, 1))

    code = await import_file(
        data=order_file({"発注日": "20261116"}),
        dry_run=True,
        operator="t",
        session_factory=factory,
    )

    assert code == 0
    assert await _orders(factory) == []


async def test_an_unreadable_file_never_reaches_the_cutover_check(factory) -> None:
    """A file whose kind is in doubt has no plan, so it must not even ask what
    the cutover is — the error the operator needs is the one about the file."""
    code = await import_file(
        data=order_file({"処理種別": "02"}),
        dry_run=True,
        operator="t",
        session_factory=factory,
    )

    assert code == 1
