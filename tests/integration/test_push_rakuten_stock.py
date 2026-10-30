"""Integration tests — the Rakuten stock push CLI (P2-039/041).

Before this CLI there was no path that wrote stock to Rakuten at all: the bundle
job builds a Shopify adapter and selects bundles only, and `_adapters.py` had no
Rakuten builder. These tests cover the two things that decide whether the first
live write is safe — that a dry run reports exactly what the real run would do,
and that what `push_safety` refuses never reaches the adapter.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.push_rakuten_stock import EXIT_NOTHING_SELECTED, EXIT_OK, run
from app.models import ChannelSkuMapping, MasterSku, SyncAttempt
from app.models.enums import NonInventoryKindEnum
from app.services import BundlePushService, InventoryService

pytestmark = pytest.mark.integration


@pytest.fixture
def factory(_test_engine):
    """The CLI opens its own session, so it has to be handed the test one."""
    return async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)


class _RakutenFake:
    """Records what it is asked to write, and refuses a page-level call.

    Keyword-only `variant_key`, and a ValueError without one, exactly like the
    real adapter — so a target that should have been screened out fails here the
    way it fails against RMS.
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


async def _master(
    factory,
    code: str,
    *,
    stock: int = 0,
    rakuten: tuple[str, str | None] | list[tuple[str, str | None]] = (),
    shopify: str | None = None,
    stock_managed: bool = True,
) -> int:
    pairs = [rakuten] if isinstance(rakuten, tuple) and rakuten else list(rakuten)
    async with factory() as session, session.begin():
        # `is_stock_managed = (non_inventory_kind IS NULL)` is a CHECK constraint:
        # an 在庫管理対象外 master has to say WHY, which is what keeps gift boxes
        # from silently accumulating the -12,509 they reached in Phase 1-B.
        master = MasterSku(
            sku_code=code,
            name=code,
            is_stock_managed=stock_managed,
            non_inventory_kind=None if stock_managed else NonInventoryKindEnum.PACKAGING.value,
        )
        session.add(master)
        await session.flush()
        if stock:
            await InventoryService(session).manual_adjust(
                master_sku_id=master.id, quantity_delta=stock, reason="seed", operator="t"
            )
        for page, key in pairs:
            session.add(
                ChannelSkuMapping(
                    master_sku_id=master.id,
                    channel="rakuten",
                    channel_sku=page,
                    channel_variant_key=key,
                    is_active=True,
                )
            )
        if shopify:
            session.add(
                ChannelSkuMapping(
                    master_sku_id=master.id,
                    channel="shopify",
                    channel_sku=shopify,
                    is_active=True,
                )
            )
        return master.id


# ---------- selection ----------


async def test_masters_mapped_on_finds_ordinary_masters_not_just_bundles(factory) -> None:
    """`all_bundle_ids` cannot serve this: the Rakuten catalogue is ordinary
    masters almost entirely, and a stock push is not about bundles."""
    wanted = await _master(factory, "N55gold", rakuten=("10048", "1147"))
    await _master(factory, "N99gold", shopify="N99gold")
    await _master(factory, "N98gold", rakuten=("10050", "1200"), stock_managed=False)

    async with factory() as session:
        ids = await BundlePushService(session).masters_mapped_on("rakuten")

    assert ids == [wanted]


async def test_a_master_with_no_mapping_is_absent_from_the_plan(factory) -> None:
    """An absence is not a refusal, so it produces no plan row to report."""
    mapped = await _master(factory, "N55gold", stock=4, rakuten=("10048", "1147"))
    unmapped = await _master(factory, "N55silver", stock=9)

    async with factory() as session:
        plans = await BundlePushService(session).resolve_push_plan(
            channel="rakuten", master_ids=[mapped, unmapped]
        )

    assert [p.master_sku_id for p in plans] == [mapped]
    assert plans[0].available == 4


# ---------- the CLI ----------


async def test_a_dry_run_writes_nothing_and_reports_what_it_would_write(factory, capsys) -> None:
    await _master(factory, "N55gold", stock=6, rakuten=[("10048", None), ("10048", "1147")])
    fake = _RakutenFake()

    code = await run(
        sku_codes=["N55gold"],
        push_all=False,
        limit=None,
        dry_run=True,
        triggered_by="t",
        session_factory=factory,
        adapter=fake,
    )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert fake.calls == []
    assert "10048/1147" in out
    assert "書き込み 1件" in out
    # The page-level row is reported as out of scope, not as a problem.
    assert "対象外" in out


async def test_a_real_run_writes_the_sku_and_never_the_page(factory) -> None:
    """The page row costs nothing now. It used to produce one failed
    sync_attempt and one Slack error per page, on every run."""
    await _master(factory, "N55gold", stock=6, rakuten=[("10048", None), ("10048", "1147")])
    fake = _RakutenFake()

    code = await run(
        sku_codes=["N55gold"],
        push_all=False,
        limit=None,
        dry_run=False,
        triggered_by="t",
        session_factory=factory,
        adapter=fake,
    )

    assert code == EXIT_OK
    assert fake.calls == [("10048", "1147", 6)]
    async with factory() as session:
        attempts = (await session.execute(select(SyncAttempt))).scalars().all()
    assert [a.status for a in attempts] == ["succeeded"]


async def test_a_coarse_master_is_reported_and_nothing_is_sent(factory, capsys) -> None:
    """57 of 141 mapped masters hold several SKUs of one page. There is no
    quantity this push could send that is correct, so it sends none."""
    await _master(
        factory,
        "0020c",
        stock=5,
        rakuten=[("0020", "1396"), ("0020", "1397"), ("0020", "1399")],
    )
    fake = _RakutenFake()

    code = await run(
        sku_codes=["0020c"],
        push_all=False,
        limit=None,
        dry_run=False,
        triggered_by="t",
        session_factory=factory,
        adapter=fake,
    )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert fake.calls == []
    assert "中止 1ページ" in out
    assert "分割" in out
    async with factory() as session:
        assert (await session.execute(select(SyncAttempt))).scalars().all() == []


async def test_an_unknown_sku_code_is_named_rather_than_silently_pushing_nothing(
    factory, capsys
) -> None:
    """`--sku N55Gold` resolving to nothing must not read as "Rakuten did not
    change" — that is the hardest result to chase."""
    await _master(factory, "N55gold", stock=6, rakuten=("10048", "1147"))

    code = await run(
        sku_codes=["N55Gold"],
        push_all=False,
        limit=None,
        dry_run=True,
        triggered_by="t",
        session_factory=factory,
        adapter=_RakutenFake(),
    )

    out = capsys.readouterr().out
    assert code == EXIT_NOTHING_SELECTED
    assert "N55Gold" in out


async def test_limit_caps_the_masters_touched(factory) -> None:
    """`--limit` is why the first live run need not be all 419 pages at once."""
    await _master(factory, "N55gold", stock=1, rakuten=("10048", "1147"))
    await _master(factory, "N55silver", stock=2, rakuten=("10048", "1148"))
    fake = _RakutenFake()

    await run(
        sku_codes=["N55gold", "N55silver"],
        push_all=False,
        limit=1,
        dry_run=False,
        triggered_by="t",
        session_factory=factory,
        adapter=fake,
    )

    assert fake.calls == [("10048", "1147", 1)]
