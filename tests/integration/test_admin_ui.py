"""Integration tests — admin UI end-to-end via ASGITransport.

Covers Basic Auth, every screen renders, and the high-value mutations
(manual adjust, mapping create/delete, alert resolution) wire through
to the underlying services.
"""

from __future__ import annotations

import base64
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

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
    InventoryEventTypeEnum,
    InventorySnapshot,
    MappingAlert,
    MappingAlertStatusEnum,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
    ReconcileDiff,
    ReconcileRun,
    SyncAttempt,
)

pytestmark = pytest.mark.integration

USER = "admin"
PASSWORD = "test_secret"


def _auth_header(user: str = USER, password: str = PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
async def admin_client(_test_engine) -> AsyncIterator[AsyncClient]:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)

    async def _override_session():
        async with factory() as session:
            yield session

    test_settings = Settings(
        app_env="local",
        admin_username=USER,
        admin_password=PASSWORD,
    )
    app.dependency_overrides[get_session] = _override_session
    from app.ui.auth import get_settings as auth_get_settings

    app.dependency_overrides[auth_get_settings] = lambda: test_settings
    from app.ui.routes.home import (
        get_session as home_get_session,  # noqa: F401 (proves no override clash)
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


async def _seed_sku(factory, code: str = "T-1", name: str = "Test") -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code=code, name=name)
        session.add(sku)
        await session.flush()
        return sku.id


async def test_unauthenticated_request_returns_401(admin_client) -> None:
    r = await admin_client.get("/admin/")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


async def test_wrong_password_returns_401(admin_client) -> None:
    r = await admin_client.get("/admin/", headers=_auth_header(password="bad"))
    assert r.status_code == 401


async def test_home_renders(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/", headers=_auth_header())
    assert r.status_code == 200
    assert "ダッシュボード" in r.text
    assert "operator:" in r.text
    assert "/admin/manual" in r.text  # header book-icon link to the manual


async def test_manual_page_renders(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/manual", headers=_auth_header())
    assert r.status_code == 200
    assert "管理画面 操作手順書" in r.text
    assert "リコンサイル" in r.text
    assert "困ったときは" in r.text


#: Every destination the flat Phase 1-B nav bar offered. Regrouping the nav must
#: not strand any of them — this list is the contract, not the markup.
LEGACY_NAV_HREFS = [
    "/admin/",
    "/admin/inventory",
    "/admin/reconcile",
    "/admin/sync-errors",
    "/admin/mappings",
    "/admin/alerts",
    "/admin/adjust",
    "/admin/events",
]


async def test_grouped_nav_keeps_every_legacy_destination_reachable(
    admin_client, _test_engine
) -> None:
    r = await admin_client.get("/admin/", headers=_auth_header())
    for href in LEGACY_NAV_HREFS:
        assert f'href="{href}"' in r.text, f"{href} disappeared from the navigation"


async def test_grouped_nav_renders_on_desktop_and_mobile(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/", headers=_auth_header())
    # Group headings appear twice: once in the desktop bar, once in the drawer.
    for label in ("在庫", "運用", "設定"):
        assert r.text.count(f">{label}") >= 2 or r.text.count(label) >= 2

    # Desktop dropdowns are CSS-only; no new JS may be required to open them.
    assert "group-hover:block" in r.text
    assert "group-focus-within:block" in r.text  # keyboard users get the same menu
    # 分析 gained its screens in W4; the group now renders with its links. (Until
    # then an empty group was asserted absent, so it would not be a dead button.)
    assert "分析メニュー" in r.text
    for href in ("/admin/analytics/sales", "/admin/analytics/stockout-risk"):
        assert f'href="{href}"' in r.text


async def test_inventory_list_filters_and_paginates(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_sku(factory, "INV-A", "Apple")
    await _seed_sku(factory, "INV-B", "Banana")

    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    assert r.status_code == 200
    assert "INV-A" in r.text and "INV-B" in r.text

    # Search narrows.
    r = await admin_client.get("/admin/inventory?q=Banana", headers=_auth_header())
    assert "INV-A" not in r.text
    assert "INV-B" in r.text


async def _seed_stock(factory, code: str, qty: int, name: str | None = None) -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code=code, name=name or code)
        session.add(sku)
        await session.flush()
        session.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=qty))
        return sku.id


async def test_inventory_status_buckets_filter(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_stock(factory, "ST-NEG", -3)
    await _seed_stock(factory, "ST-ZERO", 0)
    await _seed_stock(factory, "ST-LOW", 5)
    await _seed_stock(factory, "ST-OK", 50)

    r = await admin_client.get("/admin/inventory?filter=negative", headers=_auth_header())
    assert "ST-NEG" in r.text and "ST-ZERO" not in r.text and "ST-OK" not in r.text

    r = await admin_client.get("/admin/inventory?filter=zero", headers=_auth_header())
    assert "ST-ZERO" in r.text and "ST-NEG" not in r.text and "ST-LOW" not in r.text

    # Low is the exclusive 1..9 bucket — excludes zero and negative.
    r = await admin_client.get("/admin/inventory?filter=low", headers=_auth_header())
    assert "ST-LOW" in r.text and "ST-ZERO" not in r.text and "ST-NEG" not in r.text

    r = await admin_client.get("/admin/inventory?filter=normal", headers=_auth_header())
    assert "ST-OK" in r.text and "ST-LOW" not in r.text


async def test_inventory_default_order_prioritizes_problems(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_stock(factory, "ZZZ-OK", 50)  # alphabetically last, but normal
    await _seed_stock(factory, "AAA-NEG", -1)  # alphabetically first, negative

    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    # Default sort is problem-priority: the negative SKU appears before the normal one.
    assert r.text.index("AAA-NEG") < r.text.index("ZZZ-OK")


async def test_inventory_export_csv(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_stock(factory, "EXP-NEG", -2, "NegItem")
    await _seed_stock(factory, "EXP-OK", 30, "OkItem")

    r = await admin_client.get("/admin/inventory/export.csv", headers=_auth_header())
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "sku_code,name,jan_code,on_hand_qty,status,best_seller,updated_at" in r.text
    assert "EXP-NEG,NegItem,,-2,マイナス" in r.text
    assert "EXP-OK,OkItem,,30,正常" in r.text

    # Export respects the state filter.
    r = await admin_client.get(
        "/admin/inventory/export.csv?filter=negative", headers=_auth_header()
    )
    assert "EXP-NEG" in r.text and "EXP-OK" not in r.text


async def test_inventory_bestseller_flag_and_risk(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    hot = await _seed_stock(factory, "HOT-1", 3, "HotSeller")  # low stock
    await _seed_stock(factory, "COLD-1", 100, "ColdItem")  # no sales

    # Give HOT-1 recent order_consumed events so it's the sole best-seller.
    async with factory() as session, session.begin():
        for i in range(5):
            session.add(
                InventoryEvent(
                    master_sku_id=hot,
                    event_type=InventoryEventTypeEnum.ORDER_CONSUMED,
                    quantity_delta=-4,
                    source_channel="shopify",
                    source_order_id=f"BS-O{i}",
                    source_line_id="L1",
                    occurred_at=datetime.now(UTC),
                )
            )

    # Default view flags the best-seller and its low-stock risk.
    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    assert "売れ筋" in r.text
    assert "売れ筋商品が在庫不足です" in r.text  # HOT-1 qty 3 (<10)

    # The bestseller filter narrows to just the hot SKU.
    r = await admin_client.get("/admin/inventory?filter=bestseller", headers=_auth_header())
    assert "HOT-1" in r.text and "COLD-1" not in r.text


def _badge_count(html: str, key: str) -> int:
    """Read a status badge's number straight out of the rendered page."""
    match = re.search(rf'filter={key}&(?:amp;)?sort=.*?tabular-nums">(\d+)<', html, re.DOTALL)
    assert match is not None, f"badge {key!r} not found in the page"
    return int(match.group(1))


def _csv_rows(text: str) -> list[str]:
    return [line for line in text.splitlines()[1:] if line.strip()]


@pytest.mark.parametrize(("bucket", "qty"), [("negative", -4), ("zero", 0), ("low", 6)])
async def test_inventory_badge_count_equals_its_filtered_view(
    admin_client, _test_engine, bucket: str, qty: int
) -> None:
    """The invariant this screen is built around: a badge number IS the row count
    of the view it links to. They were previously computed by two separately
    written queries, so any divergence in scope silently made the badges lie."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    for i in range(3):
        await _seed_stock(factory, f"BC-{bucket}-{i}", qty)
    # Noise in the other buckets, plus rows the scope must exclude.
    await _seed_stock(factory, "BC-NOISE-1", -9)
    await _seed_stock(factory, "BC-NOISE-2", 0)
    await _seed_stock(factory, "BC-NOISE-3", 3)
    await _seed_stock(factory, "BC-NOISE-4", 77)
    await _seed_hidden(factory, "BC-HIDDEN-UNMANAGED", qty, unmanaged=True)
    await _seed_hidden(factory, "BC-HIDDEN-ARCHIVED", qty, archived=True)

    page = await admin_client.get("/admin/inventory", headers=_auth_header())
    badge = _badge_count(page.text, bucket)

    csv_view = await admin_client.get(
        f"/admin/inventory/export.csv?filter={bucket}", headers=_auth_header()
    )
    assert badge == len(_csv_rows(csv_view.text))
    # And the excluded rows are excluded from BOTH, not just one of them.
    assert "BC-HIDDEN-UNMANAGED" not in page.text
    assert "BC-HIDDEN-ARCHIVED" not in csv_view.text


async def _seed_hidden(
    factory, code: str, qty: int, *, unmanaged: bool = False, archived: bool = False
) -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(
            sku_code=code,
            name=code,
            is_stock_managed=not unmanaged,
            # The CHECK constraint keeps flag and kind in lockstep.
            non_inventory_kind="packaging" if unmanaged else None,
            archived_at=datetime.now(UTC) if archived else None,
            archived_reason="variant cutover" if archived else None,
        )
        session.add(sku)
        await session.flush()
        session.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=qty))
        return sku.id


async def test_inventory_hides_unmanaged_and_archived_until_asked(
    admin_client, _test_engine
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_stock(factory, "VIS-NORMAL", 5)
    await _seed_hidden(factory, "VIS-GIFTBOX", -12509, unmanaged=True)
    await _seed_hidden(factory, "VIS-LEGACY", 0, archived=True)

    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    assert "VIS-NORMAL" in r.text
    assert "VIS-GIFTBOX" not in r.text  # the -12509 runaway must not scare operators
    assert "VIS-LEGACY" not in r.text

    r = await admin_client.get("/admin/inventory?include_hidden=1", headers=_auth_header())
    assert "VIS-GIFTBOX" in r.text and "VIS-LEGACY" in r.text
    assert "在庫管理対象外" in r.text and "アーカイブ済" in r.text

    # The toggle survives paging/sorting links so it cannot be lost mid-navigation.
    assert "include_hidden=1" in r.text


async def test_inventory_bestseller_ranking_ignores_non_merchandise(
    admin_client, _test_engine
) -> None:
    """A gift box "sells" with every order and topped the Phase 1-B ranking.
    Ranking is scoped to the analysable population, so it cannot any more."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    box = await _seed_hidden(factory, "RANK-BOX", 0, unmanaged=True)
    real = await _seed_stock(factory, "RANK-REAL", 40, "RealProduct")

    async with factory() as session, session.begin():
        for i in range(20):  # the box "sells" far more than the real product
            session.add(
                InventoryEvent(
                    master_sku_id=box,
                    event_type=InventoryEventTypeEnum.ORDER_CONSUMED,
                    quantity_delta=-9,
                    source_channel="shopify",
                    source_order_id=f"RK-BOX-{i}",
                    source_line_id="L1",
                    occurred_at=datetime.now(UTC),
                )
            )
        session.add(
            InventoryEvent(
                master_sku_id=real,
                event_type=InventoryEventTypeEnum.ORDER_CONSUMED,
                quantity_delta=-1,
                source_channel="shopify",
                source_order_id="RK-REAL-1",
                source_line_id="L1",
                occurred_at=datetime.now(UTC),
            )
        )

    r = await admin_client.get("/admin/inventory?filter=bestseller", headers=_auth_header())
    assert "RANK-REAL" in r.text
    assert "RANK-BOX" not in r.text


async def test_inventory_export_reports_lifecycle_state(admin_client, _test_engine) -> None:
    """New CSV columns are appended, never inserted: operators have saved Excel
    filters keyed to the Phase 1-B column positions."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_stock(factory, "LC-PLAIN", 12, "PlainItem")
    await _seed_hidden(factory, "LC-BOX", 0, unmanaged=True)

    r = await admin_client.get(
        "/admin/inventory/export.csv?include_hidden=1", headers=_auth_header()
    )
    # Decoded utf-8-sig, like any correct reader: every export carries a BOM so
    # Excel picks UTF-8 rather than the system codepage.
    text = r.content.decode("utf-8-sig")
    header = text.splitlines()[0]
    # The lifecycle columns follow the Phase 1-B ones directly; anything added
    # since (the W5 threshold and velocity) comes after them, never between.
    assert header.startswith(
        "sku_code,name,jan_code,on_hand_qty,status,best_seller,updated_at,stock_managed,archived"
    )
    assert "LC-BOX" in text and "対象外" in text


async def test_stock_managed_toggle_flips_the_flag_and_keeps_filters(
    admin_client, _test_engine
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_stock(factory, "TG-1", 5, "Toggle")

    r = await admin_client.post(
        f"/admin/inventory/{sku_id}/stock-managed?q=Toggle&filter=all",
        data={"kind": "coupon"},
        headers=_auth_header(),
    )
    assert r.status_code == 303
    # The operator is returned to the list they were on, not a reset one.
    assert "q=Toggle" in r.headers["location"]

    async with factory() as session:
        master = (
            await session.execute(select(MasterSku).where(MasterSku.id == sku_id))
        ).scalar_one()
    assert master.is_stock_managed is False
    assert master.non_inventory_kind == "coupon"

    # And back again — the CHECK constraint requires the pair to move together.
    await admin_client.post(f"/admin/inventory/{sku_id}/stock-managed", headers=_auth_header())
    async with factory() as session:
        master = (
            await session.execute(select(MasterSku).where(MasterSku.id == sku_id))
        ).scalar_one()
    assert master.is_stock_managed is True
    assert master.non_inventory_kind is None


async def test_archive_toggle_refuses_a_sku_still_in_use(admin_client, _test_engine) -> None:
    """Archiving hides a SKU from every current-state screen. Doing that while a
    channel can still order it means stock drains from something nobody watches."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    held = await _seed_stock(factory, "AR-STOCK", 4)
    mapped = await _seed_stock(factory, "AR-MAPPED", 0)
    free = await _seed_stock(factory, "AR-FREE", 0)
    async with factory() as session, session.begin():
        session.add(
            ChannelSkuMapping(
                master_sku_id=mapped, channel="shopify", channel_sku="AR-MAPPED", is_active=True
            )
        )

    for blocked in (held, mapped):
        r = await admin_client.post(f"/admin/inventory/{blocked}/archive", headers=_auth_header())
        assert r.status_code == 303
        assert "flash=archive_blocked" in r.headers["location"]

    r = await admin_client.post(f"/admin/inventory/{free}/archive", headers=_auth_header())
    assert "flash=archived" in r.headers["location"]

    async with factory() as session:
        rows = (
            (await session.execute(select(MasterSku).where(MasterSku.id.in_([held, mapped, free]))))
            .scalars()
            .all()
        )
    by_id = {m.id: m for m in rows}
    assert by_id[held].archived_at is None
    assert by_id[mapped].archived_at is None
    assert by_id[free].archived_at is not None

    # Un-archiving is always allowed — the guards protect the hide, not the show.
    r = await admin_client.post(f"/admin/inventory/{free}/archive", headers=_auth_header())
    assert "flash=unarchived" in r.headers["location"]


async def test_lifecycle_toggle_on_a_missing_sku_flashes_instead_of_500(
    admin_client, _test_engine
) -> None:
    r = await admin_client.post("/admin/inventory/999999/archive", headers=_auth_header())
    assert r.status_code == 303
    assert "flash=notfound" in r.headers["location"]


async def test_adjust_dropdown_matches_the_inventory_list_population(
    admin_client, _test_engine
) -> None:
    """A SKU an operator cannot see on the list must not be silently adjustable
    from the dropdown either — otherwise archiving moves work somewhere the
    operator is not looking."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_stock(factory, "ADJ-LIVE", 5)
    hidden = await _seed_hidden(factory, "ADJ-ARCHIVED", 0, archived=True)

    r = await admin_client.get("/admin/adjust", headers=_auth_header())
    assert "ADJ-LIVE" in r.text
    assert "ADJ-ARCHIVED" not in r.text

    # ...but a deep link with that SKU preselected still finds it, so the genuine
    # case (zeroing a retired SKU) does not hit an empty-looking form.
    r = await admin_client.get(f"/admin/adjust?master_sku_id={hidden}", headers=_auth_header())
    assert "ADJ-ARCHIVED" in r.text


async def test_adjust_form_shows_reason_templates(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/adjust", headers=_auth_header())
    assert r.status_code == 200
    assert "検品NG" in r.text  # a reason template
    assert "POPUP戻り在庫" in r.text
    assert "最近の手動調整" in r.text  # recent-history panel


async def test_adjust_confirm_shows_before_after(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_stock(factory, "CONF-1", 10, "Confirmable")

    r = await admin_client.post(
        "/admin/adjust/confirm",
        data={"master_sku_id": sku_id, "quantity_delta": 5, "reason": "棚卸差異"},
        headers=_auth_header(),
    )
    assert r.status_code == 200
    assert "確定して適用" in r.text  # confirm screen, not yet applied
    assert "棚卸差異" in r.text
    assert "15" in r.text  # after = 10 + 5

    # Nothing applied yet — no event exists.
    async with factory() as session:
        events = (
            await session.execute(
                select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
            )
        ).all()
        assert events == []


async def test_adjust_confirm_rejects_negative_before_applying(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_stock(factory, "CONF-2", 3, "TooLow")

    r = await admin_client.post(
        "/admin/adjust/confirm",
        data={"master_sku_id": sku_id, "quantity_delta": -5, "reason": "x"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "insufficient" in r.headers["location"]


async def test_adjust_recent_history_reflects_applied(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_stock(factory, "REC-1", 20, "Recent")

    await admin_client.post(
        "/admin/adjust",
        data={"master_sku_id": sku_id, "quantity_delta": -2, "reason": "破損"},
        headers=_auth_header(),
    )
    r = await admin_client.get("/admin/adjust", headers=_auth_header())
    assert "REC-1" in r.text
    assert "破損" in r.text  # shows in the recent-adjustments panel


async def test_alerts_tabs_counts_and_filter(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        session.add_all(
            [
                MappingAlert(channel="shopify", channel_sku="OPEN-1", status="open"),
                MappingAlert(channel="rakuten", channel_sku="OPEN-2", status="open"),
                MappingAlert(channel="shopify", channel_sku="DONE-1", status="resolved"),
            ]
        )

    # Open tab shows only open alerts.
    r = await admin_client.get("/admin/alerts?status=open", headers=_auth_header())
    assert "OPEN-1" in r.text and "OPEN-2" in r.text and "DONE-1" not in r.text

    # Resolved tab shows only resolved.
    r = await admin_client.get("/admin/alerts?status=resolved", headers=_auth_header())
    assert "DONE-1" in r.text and "OPEN-1" not in r.text

    # Channel filter.
    r = await admin_client.get("/admin/alerts?status=open&channel=rakuten", headers=_auth_header())
    assert "OPEN-2" in r.text and "OPEN-1" not in r.text

    # SKU substring filter.
    r = await admin_client.get("/admin/alerts?status=open&q=OPEN-1", headers=_auth_header())
    assert "OPEN-1" in r.text and "OPEN-2" not in r.text


async def test_alerts_start_moves_to_in_progress(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        alert = MappingAlert(channel="shopify", channel_sku="MISS-IP", status="open")
        session.add(alert)
        await session.flush()
        alert_id = alert.id

    r = await admin_client.post(
        f"/admin/alerts/{alert_id}/start",
        data={"assignee": "田中"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "status=in_progress" in r.headers["location"]

    async with factory() as session:
        alert = (
            await session.execute(select(MappingAlert).where(MappingAlert.id == alert_id))
        ).scalar_one()
        assert alert.status == "in_progress"
        assert alert.assignee == "田中"

    r = await admin_client.get("/admin/alerts?status=in_progress", headers=_auth_header())
    assert "MISS-IP" in r.text
    r = await admin_client.get("/admin/alerts?status=open", headers=_auth_header())
    assert "MISS-IP" not in r.text


async def test_alerts_resolve_from_in_progress_replays(admin_client, _test_engine) -> None:
    """Regression: resolving must work from in_progress, not just open."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "IP-SKU", "InProgResolve")
    async with factory() as session, session.begin():
        session.add(
            MappingAlert(
                channel="shopify", channel_sku="IP-1", status="in_progress", assignee="田中"
            )
        )
        order = Order(
            channel="shopify",
            channel_order_id="O-IP",
            status=OrderStatusEnum.PENDING_MAPPING,
            ordered_at=datetime(2026, 5, 11, tzinfo=UTC),
        )
        session.add(order)
        await session.flush()
        session.add(
            OrderItem(
                order_id=order.id, line_id="L1", channel_sku="IP-1", quantity=1, unit_price=1000
            )
        )
    async with factory() as session:
        alert_id = (
            await session.execute(select(MappingAlert.id).where(MappingAlert.channel_sku == "IP-1"))
        ).scalar_one()

    r = await admin_client.post(
        f"/admin/alerts/{alert_id}/resolve",
        data={"sku_code": "IP-SKU"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "resolved:1" in r.headers["location"]
    assert sku_id is not None

    async with factory() as session:
        alert = (
            await session.execute(select(MappingAlert).where(MappingAlert.id == alert_id))
        ).scalar_one()
        assert alert.status == MappingAlertStatusEnum.RESOLVED


async def test_mapping_create_and_delete(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "M-1", "Mapped")

    r = await admin_client.post(
        "/admin/mappings/new",
        data={
            "master_sku_id": sku_id,
            "channel": "shopify",
            "channel_sku": "SHOP-001",
        },
        headers=_auth_header(),
    )
    assert r.status_code == 303

    async with factory() as session:
        mapping = (
            await session.execute(
                select(ChannelSkuMapping).where(ChannelSkuMapping.channel_sku == "SHOP-001")
            )
        ).scalar_one()
        mapping_id = mapping.id

    r = await admin_client.post(f"/admin/mappings/{mapping_id}/delete", headers=_auth_header())
    assert r.status_code == 303

    async with factory() as session:
        rows = (
            await session.execute(
                select(ChannelSkuMapping).where(ChannelSkuMapping.id == mapping_id)
            )
        ).all()
        assert rows == []


async def test_mapping_csv_export(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "E-1", "ExportMe")
    async with factory() as session, session.begin():
        session.add(
            ChannelSkuMapping(
                master_sku_id=sku_id, channel="shopify", channel_sku="EXP-1", is_active=True
            )
        )

    r = await admin_client.get("/admin/mappings/export.csv", headers=_auth_header())
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "E-1,ExportMe,shopify,EXP-1" in r.text


async def test_mapping_export_carries_the_rakuten_variant_key(admin_client, _test_engine) -> None:
    """Without this column a Rakuten page's SKUs are indistinguishable in the
    export: they share one `channel_sku`, so the file showed one line per page and
    the SKU-level mappings applied on 2026-10-19 could not be seen in it at all.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    gold = await _seed_sku(factory, "VK-GOLD", "Gold")
    silver = await _seed_sku(factory, "VK-SILVER", "Silver")
    async with factory() as session, session.begin():
        session.add_all(
            [
                ChannelSkuMapping(
                    master_sku_id=gold,
                    channel="rakuten",
                    channel_sku="90099",
                    channel_variant_key="90099-gold",
                    is_active=True,
                ),
                ChannelSkuMapping(
                    master_sku_id=silver,
                    channel="rakuten",
                    channel_sku="90099",
                    channel_variant_key="90099-silver",
                    is_active=True,
                ),
            ]
        )

    r = await admin_client.get("/admin/mappings/export.csv", headers=_auth_header())

    assert "channel_variant_key" in r.text.splitlines()[0]
    assert "VK-GOLD,Gold,rakuten,90099,90099-gold" in r.text
    assert "VK-SILVER,Silver,rakuten,90099,90099-silver" in r.text
    # The JST basis identifies a file handed to the client.
    assert 'filename="mappings_' in r.headers["content-disposition"]


async def test_reimporting_an_export_keeps_sku_rows_out_of_page_grain(
    admin_client, _test_engine
) -> None:
    """The hazard the two halves share. If the importer dropped
    `channel_variant_key`, re-importing an export would insert a NULL-key row for
    the page — which names exactly one master for a page selling several colours,
    the 57-master shape — and swallow the rest as duplicate skips.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    gold = await _seed_sku(factory, "RT-GOLD", "Gold")
    async with factory() as session, session.begin():
        session.add(
            ChannelSkuMapping(
                master_sku_id=gold,
                channel="rakuten",
                channel_sku="90100",
                channel_variant_key="90100-gold",
                is_active=True,
            )
        )

    exported = (await admin_client.get("/admin/mappings/export.csv", headers=_auth_header())).text

    r = await admin_client.post(
        "/admin/mappings/import",
        files={"file": ("mappings.csv", exported.encode("utf-8"), "text/csv")},
        headers=_auth_header(),
    )
    assert r.status_code == 303

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ChannelSkuMapping).where(ChannelSkuMapping.channel_sku == "90100")
                )
            )
            .scalars()
            .all()
        )
    assert [x.channel_variant_key for x in rows] == ["90100-gold"]


async def test_manual_adjust_records_event_with_operator(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "ADJ-1", "Adjust me")

    r = await admin_client.post(
        "/admin/adjust",
        data={
            "master_sku_id": sku_id,
            "quantity_delta": 7,
            "reason": "棚卸",
        },
        headers=_auth_header(),
    )
    assert r.status_code == 303

    async with factory() as session:
        event = (
            await session.execute(
                select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert event.event_type == InventoryEventTypeEnum.MANUAL_ADJUST
        assert event.quantity_delta == 7
        assert event.reason == "棚卸"
        assert event.operator == USER


async def test_manual_adjust_rejects_negative_stock(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "NEG-1", "Neg")

    r = await admin_client.post(
        "/admin/adjust",
        data={"master_sku_id": sku_id, "quantity_delta": -5, "reason": "x"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "insufficient" in r.headers["location"]


async def test_event_log_filters(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "EV-1", "Event")
    async with factory() as session, session.begin():
        session.add(
            InventoryEvent(
                master_sku_id=sku_id,
                event_type=InventoryEventTypeEnum.MANUAL_ADJUST,
                quantity_delta=3,
                reason="seed",
                operator="op",
                occurred_at=datetime(2026, 5, 11, 10, 0, tzinfo=UTC),
            )
        )

    r = await admin_client.get("/admin/events?event_type=manual_adjust", headers=_auth_header())
    assert r.status_code == 200
    assert "manual_adjust" in r.text
    assert "+3" in r.text


async def test_alerts_resolve_replays_pending_order(admin_client, _test_engine) -> None:
    """End-to-end: alert resolution backfills mapping and replays parked order."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "ALERT-1", "Will resolve")
    async with factory() as session, session.begin():
        session.add(
            MappingAlert(
                channel="shopify",
                channel_sku="MISSING-1",
                status=MappingAlertStatusEnum.OPEN,
            )
        )
        order = Order(
            channel="shopify",
            channel_order_id="O-ALERT",
            status=OrderStatusEnum.PENDING_MAPPING,
            ordered_at=datetime(2026, 5, 11, tzinfo=UTC),
        )
        session.add(order)
        await session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                line_id="L-1",
                channel_sku="MISSING-1",
                quantity=2,
                unit_price=1000,
            )
        )

    async with factory() as session:
        result = await session.execute(
            select(MappingAlert.id).where(MappingAlert.channel_sku == "MISSING-1")
        )
        alert_id = result.scalar_one()

    r = await admin_client.post(
        f"/admin/alerts/{alert_id}/resolve",
        data={"sku_code": "ALERT-1"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "resolved:1" in r.headers["location"]

    async with factory() as session:
        alert = (
            await session.execute(select(MappingAlert).where(MappingAlert.id == alert_id))
        ).scalar_one()
        assert alert.status == MappingAlertStatusEnum.RESOLVED
        assert alert.resolved_master_sku_id == sku_id

        order = (
            await session.execute(select(Order).where(Order.channel_order_id == "O-ALERT"))
        ).scalar_one()
        assert order.status == "confirmed"

        events = (
            (
                await session.execute(
                    select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].quantity_delta == -2


# --------------------------------------------------------------------------- #
# 同期エラー (sync errors)                                                      #
# --------------------------------------------------------------------------- #


async def _seed_failed_push(factory, sku_id: int, *, channel: str = "shopify") -> int:
    async with factory() as session, session.begin():
        attempt = SyncAttempt(
            attempt_type="push_inventory",
            channel=channel,
            master_sku_id=sku_id,
            payload={"channel_sku": "SHOP-9", "quantity": 5, "triggered_by": "poll"},
            status="failed",
            error_code="ReadTimeout",
            error_message="read timed out",
        )
        session.add(attempt)
        await session.flush()
        return attempt.id


async def test_sync_errors_list_localizes_and_filters(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "SE-1", "SyncErr")
    await _seed_failed_push(factory, sku_id)

    r = await admin_client.get("/admin/sync-errors", headers=_auth_header())
    assert r.status_code == 200
    assert "SE-1" in r.text
    assert "タイムアウト" in r.text  # localized guidance
    assert "再実行" in r.text  # retry button present

    # Default filter is failed; asking for succeeded hides it.
    r = await admin_client.get("/admin/sync-errors?status=succeeded", headers=_auth_header())
    assert "SE-1" not in r.text


class _FakeAdapter:
    """Minimal ChannelAdapter stand-in for exercising the retry push path
    without a live channel (mirrors how the push-service tests fake adapters)."""

    channel = "shopify"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    async def __aenter__(self) -> _FakeAdapter:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def push_inventory(self, channel_sku: str, quantity: int) -> dict[str, object]:
        self.calls.append((channel_sku, quantity))
        if self.fail:
            raise RuntimeError("boom")
        return {"ok": True, "quantity": quantity}


async def test_sync_errors_retry_pushes_current_quantity(
    admin_client, _test_engine, monkeypatch
) -> None:
    from app.ui.routes import sync_errors as sync_errors_module

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "SE-2", "Retryable")
    async with factory() as session, session.begin():
        session.add(InventorySnapshot(master_sku_id=sku_id, on_hand_qty=4))
    attempt_id = await _seed_failed_push(factory, sku_id)

    fake = _FakeAdapter()
    monkeypatch.setattr(sync_errors_module, "build_retry_adapter", lambda channel, settings: fake)

    r = await admin_client.post(
        f"/admin/sync-errors/{attempt_id}/retry",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "retried" in r.headers["location"]
    # It re-pushed the CURRENT snapshot quantity (4), not the stale payload (5).
    assert fake.calls == [("SHOP-9", 4)]

    async with factory() as session:
        child = (
            await session.execute(
                select(SyncAttempt).where(SyncAttempt.parent_attempt_id == attempt_id)
            )
        ).scalar_one()
        assert child.status == "succeeded"
        assert child.payload["quantity"] == 4


async def test_sync_errors_retry_without_adapter_flashes_nocreds(
    admin_client, _test_engine, monkeypatch
) -> None:
    from app.ui.routes import sync_errors as sync_errors_module

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "SE-4", "NoCreds")
    attempt_id = await _seed_failed_push(factory, sku_id)

    monkeypatch.setattr(sync_errors_module, "build_retry_adapter", lambda channel, settings: None)

    r = await admin_client.post(
        f"/admin/sync-errors/{attempt_id}/retry",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "nocreds" in r.headers["location"]


async def test_sync_errors_retry_rejects_non_failed(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "SE-3", "Succeeded")
    async with factory() as session, session.begin():
        attempt = SyncAttempt(
            attempt_type="push_inventory",
            channel="shopify",
            master_sku_id=sku_id,
            payload={"channel_sku": "S", "quantity": 1},
            status="succeeded",
        )
        session.add(attempt)
        await session.flush()
        attempt_id = attempt.id

    r = await admin_client.post(
        f"/admin/sync-errors/{attempt_id}/retry",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "notfailed" in r.headers["location"]


# --------------------------------------------------------------------------- #
# リコンサイル / 在庫CSV取込                                                     #
# --------------------------------------------------------------------------- #


async def _seed_reconcilable_variant(factory) -> int:
    """A variant master + its crossmall mapping (key '006c||') + a snapshot of 10."""
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code="006cV", name="Variant")
        session.add(sku)
        await session.flush()
        session.add(
            ChannelSkuMapping(
                master_sku_id=sku.id, channel="crossmall", channel_sku="006c||", is_active=True
            )
        )
        session.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=10))
        return sku.id


async def test_reconcile_list_renders(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/reconcile", headers=_auth_header())
    assert r.status_code == 200
    assert "リコンサイル" in r.text


async def test_reconcile_upload_rejects_bad_csv(admin_client, _test_engine) -> None:
    bad = "商品コード\r\n006c\r\n".encode("cp932")  # missing 在庫数量
    r = await admin_client.post(
        "/admin/reconcile/upload",
        files={"file": ("bad.csv", bad, "text/csv")},
        headers=_auth_header(),
    )
    assert r.status_code == 200
    assert "取込できません" in r.text


async def test_reconcile_upload_preview_execute_approve_finalize(
    admin_client, _test_engine
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_reconcilable_variant(factory)
    csv_bytes = "商品コード,在庫数量\r\n006c,27\r\n".encode("cp932")

    # 1) Preview — shows the +17 diff, no run created yet.
    r = await admin_client.post(
        "/admin/reconcile/upload",
        files={"file": ("stock.csv", csv_bytes, "text/csv")},
        headers=_auth_header(),
    )
    assert r.status_code == 200
    assert "006cV" in r.text
    assert "+17" in r.text
    async with factory() as session:
        assert (await session.execute(select(ReconcileRun))).scalars().all() == []

    # 2) Execute — creates the run.
    b64 = base64.b64encode(csv_bytes).decode("ascii")
    r = await admin_client.post(
        "/admin/reconcile/execute",
        data={"csv_b64": b64, "filename": "stock.csv"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert "flash=created" in location
    run_id = int(location.split("/admin/reconcile/")[1].split("?")[0])

    # 3) Detail renders with the diff.
    r = await admin_client.get(f"/admin/reconcile/{run_id}", headers=_auth_header())
    assert r.status_code == 200
    assert "+17" in r.text

    # 4) Approve the diff -> snapshot corrected to 27.
    async with factory() as session:
        diff_id = (
            await session.execute(
                select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run_id)
            )
        ).scalar_one()
    r = await admin_client.post(
        f"/admin/reconcile/{run_id}/diffs/{diff_id}/approve",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    async with factory() as session:
        snap = (
            await session.execute(
                select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert snap.on_hand_qty == 27

    # 5) Finalize -> run applied.
    r = await admin_client.post(
        f"/admin/reconcile/{run_id}/finalize",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "finalized" in r.headers["location"]
    async with factory() as session:
        run = await session.get(ReconcileRun, run_id)
        assert run.status == "applied"


async def test_reconcile_export_csv(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_reconcilable_variant(factory)
    csv_bytes = "商品コード,在庫数量\r\n006c,27\r\n".encode("cp932")
    b64 = base64.b64encode(csv_bytes).decode("ascii")
    r = await admin_client.post(
        "/admin/reconcile/execute",
        data={"csv_b64": b64, "filename": "stock.csv"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    run_id = int(r.headers["location"].split("/admin/reconcile/")[1].split("?")[0])

    r = await admin_client.get(f"/admin/reconcile/{run_id}/export.csv", headers=_auth_header())
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "006cV" in r.text
    # The audit export distinguishes the scan-time proposal from what approval
    # actually wrote to inventory_events (they differ when stock moved between).
    assert (
        "sku_code,name,current_qty,target_qty,delta_at_scan,applied_delta,decision,decided_by"
        in r.text
    )


async def test_alerts_show_product_name_and_management_number(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_sku(factory, "N23gold", "N23 ネックレス gold")  # master carries a Shopify-side name
    async with factory() as session, session.begin():
        session.add(
            MappingAlert(
                channel="rakuten",
                channel_sku="10113",
                channel_product_id="itm-10113",
                product_name="馬蹄ネックレス gold",
                status="open",
            )
        )

    r = await admin_client.get("/admin/alerts?status=open", headers=_auth_header())
    assert r.status_code == 200
    assert "馬蹄ネックレス gold" in r.text  # product name identifies the alert
    assert "itm-10113" in r.text  # 商品管理番号 shown (differs from SKU)
    assert "N23 ネックレス gold" in r.text  # master name shown in the resolve dropdown


async def test_shopify_images_render_in_inventory_and_alert_preview(
    admin_client, _test_engine
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    url = "https://cdn.shopify.com/s/files/1/test/n23gold.jpg"
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code="IMG-1", name="画像つき商品", image_url=url)
        session.add(sku)
        await session.flush()
        session.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=5))
        session.add(MappingAlert(channel="rakuten", channel_sku="IMG-A", status="open"))

    # Inventory list renders the thumbnail.
    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    assert r.status_code == 200
    assert url in r.text

    # Alerts page ships the id->image map used by the resolve preview.
    r = await admin_client.get("/admin/alerts?status=open", headers=_auth_header())
    assert r.status_code == 200
    assert 'id="skuImages"' in r.text
    assert url in r.text


async def test_thumbnails_are_click_to_enlarge(admin_client, _test_engine) -> None:
    """Thumbnails are too small to identify a product, so they open a lightbox."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        sku = MasterSku(
            sku_code="ZOOM-1", name="拡大できる商品", image_url="https://cdn.shopify.com/z.jpg"
        )
        session.add(sku)
        await session.flush()
        session.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=1))

    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    assert r.status_code == 200
    assert "data-zoom" in r.text  # thumbnail opts into the lightbox
    assert 'data-caption="ZOOM-1 — 拡大できる商品"' in r.text  # caption identifies it
    assert 'id="imgLightbox"' in r.text  # the shared lightbox is present (base.html)


async def _seed_event(factory, code: str, *, occurred: datetime, delta: int = -1) -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code=code, name=code)
        session.add(sku)
        await session.flush()
        session.add(
            InventoryEvent(
                master_sku_id=sku.id,
                event_type=InventoryEventTypeEnum.CANCELLATION_RETURNED,
                quantity_delta=delta,
                source_channel="shopify",
                source_order_id=f"BD-{code}",
                source_line_id="L1",
                occurred_at=occurred,
            )
        )
        return sku.id


async def test_events_log_exposes_backdated_rows(admin_client, _test_engine) -> None:
    """Until 2026-08-20 a cancellation was stamped with the ORIGINAL ORDER's
    time, so a credit written in August carries a June date. With only
    occurred_at on screen such a row is indistinguishable from a genuine June
    movement — which is the one question this screen exists to answer."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    # created_at defaults to now; an occurred_at two months back is backdated.
    await _seed_event(factory, "BD-OLD", occurred=datetime.now(UTC) - timedelta(days=60))
    await _seed_event(factory, "BD-NOW", occurred=datetime.now(UTC))

    # Match the BADGE, not the bare word — the filter label also says 遡及.
    badge = ">遡及</span>"

    r = await admin_client.get("/admin/events", headers=_auth_header())
    assert r.status_code == 200
    assert "記録日時" in r.text  # the insertion time is on screen at all
    assert badge in r.text

    # The filter isolates exactly the backdated population, for auditing the
    # pre-fix damage across the whole table.
    r = await admin_client.get("/admin/events?backdated=1", headers=_auth_header())
    assert "BD-OLD" in r.text
    assert "BD-NOW" not in r.text

    # A same-day event is never flagged, so the badge stays meaningful.
    r = await admin_client.get("/admin/events?sku_code=BD-NOW", headers=_auth_header())
    assert "BD-NOW" in r.text
    assert badge not in r.text


async def test_alerts_page_does_not_grow_with_alerts_times_masters(
    admin_client, _test_engine
) -> None:
    """The regression that took /admin/alerts down on 2026-08-25.

    A <select> of every master inside every row is O(alerts x masters). At
    156 x 1051 that was 163,956 options and a 28.4 MiB response, which Cloud
    Run refused — HTTP 500 with no traceback, because the app never raised.
    The master list is now emitted once as a <datalist>.

    Asserted as a SHAPE, not a byte budget: the cost of one alert row must not
    depend on how many masters exist. An absolute limit would drift with every
    styling change; this holds regardless of markup.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)

    async def seed_masters(prefix: str, n: int) -> None:
        for i in range(n):
            await _seed_sku(factory, f"{prefix}-{i:03d}", f"Scale product {prefix} {i}")

    async def page_size(n_alerts: int) -> int:
        async with factory() as session, session.begin():
            await session.execute(
                MappingAlert.__table__.delete().where(
                    MappingAlert.channel_sku.like("SCALE-ALERT-%")
                )
            )
            for i in range(n_alerts):
                session.add(
                    MappingAlert(
                        channel="shopify",
                        channel_sku=f"SCALE-ALERT-{i:03d}",
                        status=MappingAlertStatusEnum.OPEN,
                    )
                )
        r = await admin_client.get("/admin/alerts?status=open", headers=_auth_header())
        assert r.status_code == 200
        return len(r.content)

    async def cost_per_alert() -> float:
        return (await page_size(20) - await page_size(1)) / 19

    await seed_masters("SCALE-A", 40)
    with_few_masters = await cost_per_alert()

    await seed_masters("SCALE-B", 160)  # 5x the catalogue
    with_many_masters = await cost_per_alert()

    # Per-row markup is unchanged by catalogue size. If the master list were
    # inside the row again, quintupling the catalogue would roughly quintuple
    # this number.
    assert with_many_masters < with_few_masters * 1.5, (
        f"a 5x larger catalogue changed per-alert cost from {with_few_masters:.0f} "
        f"to {with_many_masters:.0f} bytes — the master list is being rendered "
        "per row again"
    )


async def test_alerts_resolve_rejects_an_unknown_sku_code(admin_client, _test_engine) -> None:
    """A <datalist> only suggests; the browser submits whatever was typed."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        session.add(
            MappingAlert(
                channel="shopify",
                channel_sku="BADSKU-1",
                status=MappingAlertStatusEnum.OPEN,
            )
        )
    async with factory() as session:
        alert_id = (
            await session.execute(
                select(MappingAlert.id).where(MappingAlert.channel_sku == "BADSKU-1")
            )
        ).scalar_one()

    r = await admin_client.post(
        f"/admin/alerts/{alert_id}/resolve",
        data={"sku_code": "TYPED-NONSENSE"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash=badsku" in r.headers["location"]

    async with factory() as session:
        alert = (
            await session.execute(select(MappingAlert).where(MappingAlert.id == alert_id))
        ).scalar_one()
    assert alert.status == MappingAlertStatusEnum.OPEN, "an invalid code must not resolve"


async def test_alerts_sku_picker_excludes_archived_masters(admin_client, _test_engine) -> None:
    """Archived masters are not valid mapping targets — offering the 359 the
    cutover retired invites resolving an alert onto a dead product."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_sku(factory, "PICK-LIVE", "Live product")
    await _seed_hidden(factory, "PICK-ARCHIVED", 0, archived=True)

    r = await admin_client.get("/admin/alerts?status=open", headers=_auth_header())
    assert "PICK-LIVE" in r.text
    assert "PICK-ARCHIVED" not in r.text


# --- run_type isolation (migration 0012) ----------------------------------
#
# reconcile_runs now holds three kinds of run. The daily CROSS MALL
# reconciliation, the Shopify stock audit that replaces it, and the physical
# stocktake all share one approval path — the only code that deliberately
# overwrites a snapshot — so the ONLY thing keeping them apart is the run_type
# filter on every query. Miss one and an audit run becomes work the operator is
# told to approve.


async def _make_run(engine, run_type: str, status: str = "pending_approval") -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import ReconcileRun

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        run = ReconcileRun(
            source="test",
            run_type=run_type,
            status=status,
            triggered_by="pytest",
        )
        session.add(run)
        await session.flush()
        return int(run.id)


async def test_the_reconcile_list_shows_external_checks_but_not_stocktakes(
    admin_client, _test_engine
) -> None:
    """Since W6 (P2-036) this screen hosts the daily Shopify audit, which
    replaced the CROSS MALL reconciliation — so an audit run belongs here. A
    stocktake has its own screen and must not appear."""
    audit_id = await _make_run(_test_engine, "shopify_audit")
    reconcile_id = await _make_run(_test_engine, "reconcile")
    stocktake_id = await _make_run(_test_engine, "stocktake")

    r = await admin_client.get("/admin/reconcile", headers=_auth_header())
    assert r.status_code == 200
    assert f"/admin/reconcile/{reconcile_id}" in r.text
    assert f"/admin/reconcile/{audit_id}" in r.text
    assert f"/admin/reconcile/{stocktake_id}" not in r.text


async def test_the_dashboard_badge_does_not_count_other_run_types(
    admin_client, _test_engine
) -> None:
    """The count docs/23 records as missed by every area design."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import ReconcileRun, ReconcileRunStatusEnum
    from app.services.reconcile import of_run_type

    await _make_run(_test_engine, "stocktake")
    await _make_run(_test_engine, "shopify_audit")

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        filtered = await session.scalar(
            select(func.count())
            .select_from(ReconcileRun)
            .where(
                of_run_type(),
                ReconcileRun.status == ReconcileRunStatusEnum.PENDING_APPROVAL.value,
            )
        )
        unfiltered = await session.scalar(
            select(func.count())
            .select_from(ReconcileRun)
            .where(ReconcileRun.status == ReconcileRunStatusEnum.PENDING_APPROVAL.value)
        )
    assert unfiltered >= filtered + 2, "the two runs just created must be excluded"

    r = await admin_client.get("/admin/", headers=_auth_header())
    assert r.status_code == 200


async def test_the_detail_screen_refuses_a_run_of_another_type(admin_client, _test_engine) -> None:
    """Rendering a stocktake here would offer CROSS MALL wording and actions
    over a different kind of count."""
    stocktake_id = await _make_run(_test_engine, "stocktake")
    r = await admin_client.get(
        f"/admin/reconcile/{stocktake_id}", headers=_auth_header(), follow_redirects=False
    )
    assert r.status_code == 303
    assert "flash=notfound" in r.headers["location"]


async def test_existing_rows_default_to_the_crossmall_reconciliation(_test_engine) -> None:
    """server_default in 0012. Without it every pre-existing row would land
    NULL and vanish from the screens the moment the filters went in."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO reconcile_runs (source, status, diff_count, applied_count,"
                " triggered_by, started_at) VALUES ('legacy', 'applied', 0, 0, 'pytest', now())"
            )
        )
        got = await session.scalar(
            text("SELECT run_type FROM reconcile_runs WHERE source = 'legacy' LIMIT 1")
        )
    assert got == "reconcile"


# --- 2026-10-03 responsive review ---------------------------------------------


async def test_a_bundle_parent_is_not_listed_as_zero_stock(admin_client, _test_engine) -> None:
    """A parent holds no stock of its own; listed, it read 「0・ゼロ」 and
    inflated the zero badge against the dashboard's count."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_stock(factory, "POOL-SKU", 0)
    async with factory() as session, session.begin():
        session.add(MasterSku(sku_code="SET-PARENT", name="SET-PARENT", is_bundle=True))

    for hidden in ("", "&include_hidden=1"):
        r = await admin_client.get(f"/admin/inventory?filter=all{hidden}", headers=_auth_header())
        assert "POOL-SKU" in r.text
        assert "SET-PARENT" not in r.text

    r = await admin_client.get("/admin/inventory?filter=zero", headers=_auth_header())
    assert "POOL-SKU" in r.text and "SET-PARENT" not in r.text


async def test_every_inventory_row_opens_a_panel_with_the_full_name(
    admin_client, _test_engine
) -> None:
    """The table shows one line; the full title lives in the row's panel."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    long_name = "【送料無料】 クロス ネックレス スマイル 笑顔 にこちゃん 十字架 チャーム" * 2
    sku_id = await _seed_stock(factory, "LONG-NAME", 3, long_name)

    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    assert f'data-detail="inv-{sku_id}"' in r.text
    assert f'<template id="inv-{sku_id}"' in r.text
    panel = r.text.split(f'<template id="inv-{sku_id}"', 1)[1].split("</template>", 1)[0]
    assert long_name in panel
    assert f"/admin/analytics/sku/{sku_id}" in panel


# --- 棚卸の確定・取り消し --------------------------------------------------
#
# The stocktake detail screen is one of the few the client drives by hand, and
# the only one they drive during the physical count. `finalize_run` REFUSES
# while any diff is still pending and `cancel_run` refuses once anything is
# approved; the reconcile screen has caught both since W6, this one did not, so
# either action escaped the route as an HTTP 500. The screen also promised the
# opposite of what the service does, so an operator had every reason to click
# 確定 with differences outstanding.


async def _stocktake_run_with_diff(engine, *, decision: str, applied_count: int = 0) -> int:
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        master = MasterSku(sku_code=f"ST-{decision}", name="count me")
        session.add(master)
        await session.flush()
        run = ReconcileRun(
            source="stocktake",
            run_type="stocktake",
            status="pending_approval",
            triggered_by="pytest",
            # `cancel_run` guards on this counter, which `approve_diff`
            # increments — not on any diff's decision column.
            applied_count=applied_count,
        )
        session.add(run)
        await session.flush()
        session.add(
            ReconcileDiff(
                reconcile_run_id=run.id,
                master_sku_id=master.id,
                current_qty=3,
                target_qty=5,
                delta=2,
                decision=decision,
            )
        )
        await session.flush()
        return int(run.id)


async def test_finalising_a_stocktake_with_an_unreviewed_difference_is_refused(
    admin_client, _test_engine
) -> None:
    run_id = await _stocktake_run_with_diff(_test_engine, decision="pending")

    r = await admin_client.post(
        f"/admin/stocktake/{run_id}/finalize", headers=_auth_header(), follow_redirects=False
    )

    assert r.status_code == 303
    assert "flash=pendingdiffs" in r.headers["location"]


async def test_the_stocktake_refusal_explains_itself_on_the_screen(
    admin_client, _test_engine
) -> None:
    """A redirect the operator cannot read is only a quieter failure."""
    run_id = await _stocktake_run_with_diff(_test_engine, decision="pending")

    r = await admin_client.get(
        f"/admin/stocktake/{run_id}?flash=pendingdiffs", headers=_auth_header()
    )

    assert r.status_code == 200
    assert "未承認の差分が残っています" in r.text


async def test_the_stocktake_screen_does_not_promise_to_skip_unreviewed_differences(
    admin_client, _test_engine
) -> None:
    """It used to say 確定すると未承認の差分は見送り扱いになります — the opposite of
    what the service does."""
    run_id = await _stocktake_run_with_diff(_test_engine, decision="pending")

    r = await admin_client.get(f"/admin/stocktake/{run_id}", headers=_auth_header())

    assert "見送り扱いになります" not in r.text
    assert "承認または見送りにしてください" in r.text


async def test_finalising_a_stocktake_once_everything_is_decided_succeeds(
    admin_client, _test_engine
) -> None:
    run_id = await _stocktake_run_with_diff(_test_engine, decision="skipped")

    r = await admin_client.post(
        f"/admin/stocktake/{run_id}/finalize", headers=_auth_header(), follow_redirects=False
    )

    assert r.status_code == 303
    assert "flash=finalized" in r.headers["location"]


async def test_cancelling_a_stocktake_after_an_approval_is_refused(
    admin_client, _test_engine
) -> None:
    """Approved diffs are already written to stock; cancelling cannot unwrite
    them."""
    run_id = await _stocktake_run_with_diff(_test_engine, decision="approved", applied_count=1)

    r = await admin_client.post(
        f"/admin/stocktake/{run_id}/cancel", headers=_auth_header(), follow_redirects=False
    )

    assert r.status_code == 303
    assert "flash=cancelfailed" in r.headers["location"]
