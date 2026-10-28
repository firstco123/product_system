"""Integration tests — the 卸売EDI screen (docs/29 §8).

Four steps: upload → 検証 → 確認 → 実行. The tests are written around the
property that makes the screen safe — nothing is written until the confirmation
is submitted — and around the three numbers an operator must be shown before
they submit it.

Through the authenticated async client rather than the plain `client` fixture,
because these routes commit: the rollback-only `db_session` would hide what they
wrote. Same shape as `test_admin_ui.py`.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import get_session
from app.main import app
from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    InventorySnapshot,
    MasterSku,
    Order,
    ReconcileDiff,
    ReconcileRun,
    ReconcileRunStatusEnum,
    ReconcileRunTypeEnum,
)
from app.services.reconcile import EXTERNAL_CHECK_TYPES
from app.services.wholesale_import import WHOLESALE_CHANNEL
from tests.test_wholesale_bms import order_file, return_file

pytestmark = pytest.mark.integration

USER = "admin"
PASSWORD = "test_secret"
BUYER = "0393"
ORDER_CODE = "2700002430598"
#: The stocktake's own count date, and the cutover that follows from it.
#: They are not the same day: the submitted figures are aligned to the
#: count day's close of business, so the first day whose wholesale
#: movements we add is the next one (docs/29 §10-10).
COUNTED_ON = date(2026, 10, 1)
CUTOVER = COUNTED_ON + timedelta(days=1)
#: The sample voucher is dated 2026-09-14, so anything that should actually
#: import has to be dated after the cutover.
IN_SCOPE = {"発注日": "20261116"}


def _auth() -> dict[str, str]:
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def factory(_test_engine):
    return async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def admin_client(_test_engine, factory) -> AsyncIterator[AsyncClient]:
    async def _override_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    from app.ui.auth import get_settings as auth_get_settings

    app.dependency_overrides[auth_get_settings] = lambda: Settings(
        app_env="local", admin_username=USER, admin_password=PASSWORD
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _seed(factory, *, stock: int = 10, mapped: bool = True) -> int:
    async with factory() as session, session.begin():
        master = MasterSku(sku_code="N63-GD", name="316L anchor necklace", attributes={})
        session.add(master)
        await session.flush()
        session.add(InventorySnapshot(master_sku_id=master.id, on_hand_qty=stock))
        if mapped:
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


async def _stocktake(factory, counted_on: date = COUNTED_ON, scope_note: str | None = None) -> int:
    """Returns the run id, because choosing a basis means naming one."""
    async with factory() as session, session.begin():
        run = ReconcileRun(
            source="test",
            run_type=str(ReconcileRunTypeEnum.STOCKTAKE),
            status=str(ReconcileRunStatusEnum.APPLIED),
            triggered_by="test",
            counted_on=counted_on,
            scope_note=scope_note,
            started_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        session.add(run)
        await session.flush()
        return run.id


async def _rows(factory, statement):
    async with factory() as session:
        return list((await session.execute(statement)).scalars().all())


async def _upload(admin_client, data: bytes, cutover: str = "", basis: str = ""):
    return await admin_client.post(
        "/admin/wholesale/upload",
        files={"file": ("juchu.csv", data, "text/csv")},
        data={"cutover": cutover, "basis": basis},
        headers=_auth(),
    )


async def _execute(
    admin_client,
    data: bytes,
    filename: str = "juchu.csv",
    cutover: str = "",
    basis: str = "",
):
    return await admin_client.post(
        "/admin/wholesale/execute",
        data={
            "file_b64": base64.b64encode(data).decode("ascii"),
            "filename": filename,
            "cutover": cutover,
            "basis": basis,
        },
        headers=_auth(),
        follow_redirects=False,
    )


#: The button renders as `<button type="submit" disabled` when there is nothing
#: to import. Matched on the tag, not the bare word: `disabled:bg-ink-300` and
#: `disabled:cursor-not-allowed` are Tailwind classes present on every render,
#: so `"disabled" in page.text` is true either way and proves nothing.
_DISABLED = """type="submit" disabled"""


# --------------------------------------------------------------------- the form


async def test_the_form_shows_the_cutover_before_a_file_is_chosen(admin_client, factory) -> None:
    """The commonest outcome of an upload is "almost all of it was pre-cutover",
    and an operator who does not know the date reads that as a failure."""
    await _stocktake(factory, date(2026, 10, 25))

    page = await admin_client.get("/admin/wholesale/upload", headers=_auth())

    assert page.status_code == 200
    # The cutover shown is the day AFTER the count, and the count it came from is
    # named alongside it so the two can be checked against each other.
    assert "2026-10-26" in page.text
    assert "2026-10-25" in page.text
    assert "内容を確認する" in page.text
    assert _DISABLED not in page.text


async def test_the_form_says_why_it_cannot_import_yet(admin_client) -> None:
    """With no stocktake there is no cutover, so the submit button is disabled
    rather than leading to an error after the file has been chosen."""
    page = await admin_client.get("/admin/wholesale/upload", headers=_auth())

    assert page.status_code == 200
    assert "まだ取り込めません" in page.text
    assert _DISABLED in page.text


async def test_the_form_warns_against_opening_the_file_in_excel(admin_client) -> None:
    """docs/29 §7-3 asked for this in the operations manual. It is on the screen
    where the file is chosen, because that is where the mistake is made."""
    page = await admin_client.get("/admin/wholesale/upload", headers=_auth())

    assert "Excel" in page.text
    assert "000109765" in page.text


async def test_the_screen_needs_authentication(admin_client) -> None:
    assert (await admin_client.get("/admin/wholesale")).status_code == 401
    assert (await admin_client.get("/admin/wholesale/upload")).status_code == 401


# ------------------------------------------------------------------ the preview


async def test_the_preview_writes_nothing(admin_client, factory) -> None:
    await _seed(factory)
    await _stocktake(factory)

    page = await _upload(admin_client, order_file(IN_SCOPE))

    assert page.status_code == 200
    assert "この内容で取り込む" in page.text
    assert await _rows(factory, select(InventoryEvent)) == []
    assert await _rows(factory, select(Order)) == []


async def test_the_preview_names_the_kind_it_read(admin_client, factory) -> None:
    """The operator picks a file and the direction of every quantity in it comes
    from the contents, so the screen has to say which it decided."""
    await _seed(factory)
    await _stocktake(factory)

    shipment = await _upload(admin_client, order_file(IN_SCOPE))
    credit = await _upload(admin_client, return_file({"計上日": "20261116"}))

    assert "受注" in shipment.text
    assert "返品" in credit.text


async def test_the_preview_shows_what_falls_outside_the_cutover(admin_client, factory) -> None:
    await _seed(factory)
    await _stocktake(factory)

    page = await _upload(
        admin_client,
        order_file(
            {"取引番号（発注・返品）": "000109900", "発注日": "20261116"},
            {"取引番号（発注・返品）": "000109765", "発注日": "20260815"},
        ),
    )

    assert "カットオーバー" in page.text
    assert "取り込みません" in page.text


async def test_the_preview_flags_the_lines_that_go_negative(admin_client, factory) -> None:
    """docs/29 §10-9: these are applied, because the goods shipped. So the
    number has to be on the confirmation screen, not discovered afterwards."""
    await _seed(factory, stock=1)
    await _stocktake(factory)

    page = await _upload(admin_client, order_file({**IN_SCOPE, "出荷数量（バラ）": "4"}))

    assert "在庫マイナスになります" in page.text
    assert "保留にはせず適用します" in page.text


async def test_the_preview_says_an_unmapped_line_still_books_its_revenue(
    admin_client, factory
) -> None:
    await _seed(factory, mapped=False)
    await _stocktake(factory)

    page = await _upload(admin_client, order_file(IN_SCOPE))

    assert "商品が特定できません" in page.text
    assert "売上は記録します" in page.text
    assert ORDER_CODE in page.text


async def test_a_file_that_cannot_be_read_stops_at_the_preview(admin_client, factory) -> None:
    """A mixed-kind file has no plan at all, so there is nothing to confirm."""
    await _seed(factory)
    await _stocktake(factory)

    page = await _upload(
        admin_client,
        order_file(
            {"取引明細番号（発注・返品）": "01"},
            {"取引明細番号（発注・返品）": "02", "メッセージ種": "08"},
        ),
    )

    assert page.status_code == 200
    assert "取り込めません" in page.text
    assert "この内容で取り込む" not in page.text


async def test_excel_damage_is_explained_on_the_preview(admin_client, factory) -> None:
    await _seed(factory)
    await _stocktake(factory)

    page = await _upload(admin_client, order_file({**IN_SCOPE, "取引番号（発注・返品）": "109900"}))

    assert "取り込めません" in page.text
    assert "Excel" in page.text


async def test_an_early_cutover_override_is_refused_on_the_preview(admin_client, factory) -> None:
    await _seed(factory)
    await _stocktake(factory)

    page = await _upload(admin_client, order_file(IN_SCOPE), cutover="2026-06-01")

    assert "3,045" in page.text
    assert "この内容で取り込む" not in page.text


# ------------------------------------------------------------------ the execute


async def test_confirming_applies_stock_and_sales_and_records_the_run(
    admin_client, factory
) -> None:
    master = await _seed(factory)
    await _stocktake(factory)

    done = await _execute(
        admin_client, order_file({**IN_SCOPE, "出荷数量（バラ）": "3"}), filename="juchu.csv"
    )

    assert done.status_code == 303
    assert done.headers["location"] == "/admin/wholesale?flash=applied"

    (event,) = await _rows(
        factory, select(InventoryEvent).where(InventoryEvent.master_sku_id == master)
    )
    assert event.quantity_delta == -3
    assert event.source_order_id == "000109900"
    assert len(await _rows(factory, select(Order))) == 1

    (run,) = await _rows(
        factory,
        select(ReconcileRun).where(ReconcileRun.run_type == str(ReconcileRunTypeEnum.WHOLESALE)),
    )
    # Already confirmed on the previous screen, so there is nothing to approve.
    assert run.status == ReconcileRunStatusEnum.APPLIED
    assert run.finished_at is not None
    assert run.applied_count == 1
    assert run.csv_filename == "juchu.csv"
    assert run.counted_on == CUTOVER
    assert run.notes is not None and "在庫 1明細" in run.notes


async def test_a_wholesale_run_carries_no_diffs(admin_client, factory) -> None:
    """`reconcile_diffs` is UNIQUE on (run, master_sku_id) and one voucher may
    name the same product twice, so the lines cannot live there. They live in
    `inventory_events`, keyed per line, which is what makes a re-upload a no-op.
    """
    await _seed(factory)
    await _stocktake(factory)

    await _execute(
        admin_client,
        order_file(
            {"取引明細番号（発注・返品）": "01", **IN_SCOPE, "出荷数量（バラ）": "2"},
            {"取引明細番号（発注・返品）": "02", **IN_SCOPE, "出荷数量（バラ）": "2"},
        ),
    )

    assert await _rows(factory, select(ReconcileDiff)) == []
    # Both lines still moved stock, as their own events.
    assert len(await _rows(factory, select(InventoryEvent))) == 2


async def test_confirming_the_same_file_twice_changes_nothing(admin_client, factory) -> None:
    master = await _seed(factory)
    await _stocktake(factory)
    data = order_file({**IN_SCOPE, "出荷数量（バラ）": "3"})

    await _execute(admin_client, data)
    await _execute(admin_client, data)

    events = await _rows(
        factory, select(InventoryEvent).where(InventoryEvent.master_sku_id == master)
    )
    assert len(events) == 1
    assert len(await _rows(factory, select(Order))) == 1


async def test_a_tampered_payload_is_sent_back_to_the_form(admin_client) -> None:
    done = await admin_client.post(
        "/admin/wholesale/execute",
        data={"file_b64": "not base64 at all!!", "filename": "f.csv"},
        headers=_auth(),
        follow_redirects=False,
    )

    assert done.status_code == 303
    assert "flash=" in done.headers["location"]


async def test_executing_without_a_cutover_is_refused(admin_client, factory) -> None:
    """The guard has to hold on the execute route too, not only on the preview:
    the form can be re-submitted after the preview was rendered."""
    await _seed(factory)

    done = await _execute(admin_client, order_file(IN_SCOPE))

    assert done.status_code == 303
    assert done.headers["location"] == "/admin/wholesale/upload?flash=nocutover"
    assert await _rows(factory, select(InventoryEvent)) == []


# -------------------------------------------------------------------- the history


async def test_the_history_lists_what_was_imported(admin_client, factory) -> None:
    await _seed(factory)
    await _stocktake(factory)
    await _execute(admin_client, order_file(IN_SCOPE), filename="juchu_20261116.csv")

    page = await admin_client.get("/admin/wholesale", headers=_auth())

    assert page.status_code == 200
    assert "juchu_20261116.csv" in page.text


async def test_an_empty_history_says_so(admin_client) -> None:
    page = await admin_client.get("/admin/wholesale", headers=_auth())

    assert page.status_code == 200
    assert "まだ取り込んだファイルはありません" in page.text


async def test_a_wholesale_run_stays_out_of_the_other_two_queues(admin_client, factory) -> None:
    """The reason `of_run_type` exists. A wholesale import appearing in the
    reconcile queue would be work an operator is expected to approve, and the
    dashboard's 未承認 badge would count it."""
    await _seed(factory)
    await _stocktake(factory)
    await _execute(admin_client, order_file(IN_SCOPE), filename="only_wholesale.csv")

    assert ReconcileRunTypeEnum.WHOLESALE not in EXTERNAL_CHECK_TYPES
    reconcile = await admin_client.get("/admin/reconcile", headers=_auth())
    stocktake = await admin_client.get("/admin/stocktake", headers=_auth())
    assert reconcile.status_code == 200
    assert stocktake.status_code == 200
    assert "only_wholesale.csv" not in reconcile.text
    assert "only_wholesale.csv" not in stocktake.text


# ------------------------------------------- choosing which count was the basis


async def test_the_form_offers_the_recorded_counts_when_several_dates_exist(
    admin_client, factory
) -> None:
    """The five-weekend case on screen.

    Which instalment counted the wholesale masters is not in the data — a
    finalised run keeps only the SKUs that differed (docs/29 §10-22) — so the
    screen offers the counts it has rather than asking for a date to be typed.
    A typed date is the input that can inflate stock by 3,045 units.
    """
    await _stocktake(factory, date(2026, 11, 2), scope_note="卸売優先82点 / 82SKUを計数")
    await _stocktake(factory, date(2026, 11, 15), scope_note="リング / 310SKUを計数")

    page = await admin_client.get("/admin/wholesale/upload", headers=_auth())

    assert page.status_code == 200
    assert "起点にする棚卸を選んでください" in page.text
    assert "卸売優先82点" in page.text
    assert "リング" in page.text
    # There is something to choose, so this is not the dead end no count at all is.
    assert _DISABLED not in page.text


async def test_choosing_the_basis_imports_from_the_day_after_that_count(
    admin_client, factory
) -> None:
    """The operator names the wholesale instalment and the cutover follows from
    it, with the four later weekends no longer blocking the import."""
    master = await _seed(factory)
    wholesale = await _stocktake(factory, date(2026, 11, 2), scope_note="卸売優先82点")
    await _stocktake(factory, date(2026, 11, 15), scope_note="リング")

    done = await _execute(
        admin_client,
        order_file({**IN_SCOPE, "出荷数量（バラ）": "3"}),
        basis=str(wholesale),
    )

    assert done.headers["location"] == "/admin/wholesale?flash=applied"
    (run,) = await _rows(
        factory,
        select(ReconcileRun).where(ReconcileRun.run_type == str(ReconcileRunTypeEnum.WHOLESALE)),
    )
    assert run.counted_on == date(2026, 11, 3)
    (event,) = await _rows(
        factory, select(InventoryEvent).where(InventoryEvent.master_sku_id == master)
    )
    assert event.quantity_delta == -3


async def test_without_a_basis_several_counts_refuse_rather_than_guess(
    admin_client, factory
) -> None:
    """Leaning the wrong way is the one mistake this feature cannot make cheaply,
    so an unanswered question stops the import instead of resolving itself."""
    await _seed(factory)
    await _stocktake(factory, date(2026, 11, 2))
    await _stocktake(factory, date(2026, 11, 15))

    done = await _execute(admin_client, order_file(IN_SCOPE))

    assert done.headers["location"] == "/admin/wholesale/upload?flash=nocutover"
    assert await _rows(factory, select(InventoryEvent)) == []


async def test_a_stale_basis_choice_does_not_fall_through_to_another_count(
    admin_client, factory
) -> None:
    """A bookmarked form, or a run since un-applied. Silently using some other
    run's date would be the same class of error as guessing."""
    await _seed(factory)
    await _stocktake(factory, date(2026, 11, 2))

    done = await _execute(admin_client, order_file(IN_SCOPE), basis="99999")

    assert done.headers["location"] == "/admin/wholesale/upload?flash=nocutover"
    assert await _rows(factory, select(InventoryEvent)) == []


async def test_the_preview_warns_when_a_later_count_has_absorbed_those_dates(
    admin_client, factory
) -> None:
    """docs/29 §10-28 on the screen.

    The lines are still imported — which SKUs a finalised count covered cannot be
    read back, so filtering would drop real movements — but the operator is told,
    which is what makes "import before the next counting weekend" actionable.
    """
    await _seed(factory)
    wholesale = await _stocktake(factory, date(2026, 11, 2))
    await _stocktake(factory, date(2026, 11, 15))

    page = await _upload(
        admin_client,
        order_file({"発注日": "20261110", "出荷数量（バラ）": "2"}),
        basis=str(wholesale),
    )

    assert page.status_code == 200
    assert "2026-11-15" in page.text
    assert "二重に引かれます" in page.text
