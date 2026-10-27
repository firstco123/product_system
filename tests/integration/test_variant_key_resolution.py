"""マッピングの解決 — ページ単位の行とSKU単位の行が同居しても一意に決まること.

A Rakuten page keeps its page-level mapping row, because that row is what
resolves its live order lines, while its SKUs are mapped one at a time. So both
grains coexist for one 商品管理番号 on purpose — and the moment they do, the query
this project used for three years returns more than one row.

That failure is not hypothetical and not quiet: `_lookup_mapping` runs on the
hourly order poll, `scalar_one_or_none()` raises MultipleResultsFound, and
`internal_jobs._run_job` turns it into an HTTP 500 plus a critical Slack alert.
These tests are the reason alembic 0015 may not ship without the resolver.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import ChannelSkuMapping, MasterSku
from app.services.mapping import resolve_master_sku_id

pytestmark = pytest.mark.integration

PAGE = "073"
GOLD_19 = "1435"
GOLD_20 = "1436"


async def _master(session, code: str) -> int:
    master = MasterSku(sku_code=code, name=code)
    session.add(master)
    await session.flush()
    return master.id


async def _mapping(
    session, master_id: int, *, variant: str | None, channel: str = "rakuten"
) -> None:
    session.add(
        ChannelSkuMapping(
            master_sku_id=master_id,
            channel=channel,
            channel_sku=PAGE,
            channel_variant_key=variant,
            is_active=True,
        )
    )
    await session.flush()


async def test_a_page_row_beside_two_variant_rows_still_resolves(db_session) -> None:
    """The exact state that used to raise. Three active rows for one 商品管理番号."""
    page = await _master(db_session, "073c")
    gold19 = await _master(db_session, "B07-gold-19")
    gold20 = await _master(db_session, "B07-gold-20")
    await _mapping(db_session, page, variant=None)
    await _mapping(db_session, gold19, variant=GOLD_19)
    await _mapping(db_session, gold20, variant=GOLD_20)

    # Ingest does not yet know the variant, so it asks without one and must get
    # the page-level row — the same row it got before the column existed.
    assert await resolve_master_sku_id(db_session, channel="rakuten", channel_sku=PAGE) == page
    # A caller that does know the variant gets the variant's own master.
    for variant, expected in [(GOLD_19, gold19), (GOLD_20, gold20)]:
        found = await resolve_master_sku_id(
            db_session, channel="rakuten", channel_sku=PAGE, variant_key=variant
        )
        assert found == expected


async def test_an_unmapped_variant_falls_back_to_the_page(db_session) -> None:
    """Variants are mapped one at a time, so a page is part-mapped for a while.
    An unmapped variant must keep resolving rather than become an alert."""
    page = await _master(db_session, "073c")
    gold19 = await _master(db_session, "B07-gold-19")
    await _mapping(db_session, page, variant=None)
    await _mapping(db_session, gold19, variant=GOLD_19)

    found = await resolve_master_sku_id(
        db_session, channel="rakuten", channel_sku=PAGE, variant_key="9999"
    )
    assert found == page


async def test_a_variant_row_alone_does_not_answer_a_keyless_lookup(db_session) -> None:
    """The dangerous direction. If the page row is gone, a keyless lookup must
    return nothing rather than pick one of the variants — otherwise an order line
    would consume stock from whichever size the database happened to return."""
    gold19 = await _master(db_session, "B07-gold-19")
    gold20 = await _master(db_session, "B07-gold-20")
    await _mapping(db_session, gold19, variant=GOLD_19)
    await _mapping(db_session, gold20, variant=GOLD_20)

    assert await resolve_master_sku_id(db_session, channel="rakuten", channel_sku=PAGE) is None


async def test_an_inactive_row_never_answers(db_session) -> None:
    page = await _master(db_session, "073c")
    session = db_session
    session.add(
        ChannelSkuMapping(
            master_sku_id=page,
            channel="rakuten",
            channel_sku=PAGE,
            channel_variant_key=None,
            is_active=False,
        )
    )
    await session.flush()

    assert await resolve_master_sku_id(session, channel="rakuten", channel_sku=PAGE) is None


async def test_the_same_variant_twice_is_refused_by_the_database(db_session) -> None:
    """The widened unique constraint is what lets the resolver keep
    scalar_one_or_none(). Without it the resolver would be guessing."""
    a = await _master(db_session, "B07-gold-19")
    b = await _master(db_session, "B07-gold-19-dup")
    await _mapping(db_session, a, variant=GOLD_19)

    with pytest.raises(IntegrityError):
        await _mapping(db_session, b, variant=GOLD_19)


async def test_two_page_level_rows_are_still_refused(db_session) -> None:
    """NULLS NOT DISTINCT: the four-column constraint must be exactly as strict
    as the three-column one for rows that carry no variant key. Under the
    PostgreSQL default this insert would succeed."""
    a = await _master(db_session, "073c")
    b = await _master(db_session, "073c-dup")
    await _mapping(db_session, a, variant=None)

    with pytest.raises(IntegrityError):
        await _mapping(db_session, b, variant=None)


async def test_a_blank_variant_key_is_refused(db_session) -> None:
    """ "" and NULL would be two spellings of "no variant", and only one resolves."""
    master = await _master(db_session, "073c")

    with pytest.raises(IntegrityError):
        await _mapping(db_session, master, variant="")


async def test_one_variant_key_may_repeat_across_pages(db_session) -> None:
    """132 SKU管理番号 are reused across different Rakuten products, which is why
    the key is the PAIR. The same variant key under a different page is a
    different thing and must be allowed."""
    first = await _master(db_session, "B07-gold-19")
    second = await _master(db_session, "B99-gold-19")
    await _mapping(db_session, first, variant=GOLD_19)
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=second,
            channel="rakuten",
            channel_sku="099",
            channel_variant_key=GOLD_19,
            is_active=True,
        )
    )
    await db_session.flush()

    assert (
        await resolve_master_sku_id(
            db_session, channel="rakuten", channel_sku="099", variant_key=GOLD_19
        )
        == second
    )


async def test_shopify_is_untouched_by_the_new_column(db_session) -> None:
    """Shopify's channel_sku is already the finest grain, so its rows keep a NULL
    variant key for ever and resolve exactly as before."""
    master = await _master(db_session, "N41gold")
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=master,
            channel="shopify",
            channel_sku="N41gold",
            is_active=True,
        )
    )
    await db_session.flush()

    assert (
        await resolve_master_sku_id(db_session, channel="shopify", channel_sku="N41gold") == master
    )


async def test_a_marketplace_id_resolves_instead_of_crashing_the_query(db_session) -> None:
    """A real `marketplace_id` must reach the WHERE clause as an equality.

    `marketplace_id.is_(value)` compiles to `marketplace_id IS $1`, which
    Postgres rejects as a syntax error — not a wrong answer, a query that cannot
    run. Every channel we had left `marketplace_id` unset, so the predicate was
    always `IS NULL` and the mistake could not surface. Wholesale arrived with
    発注者コード 0393 and took out all four call sites at once.
    """
    master = await _master(db_session, "B51silver")
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=master,
            channel="wholesale",
            channel_sku="2700002488353",
            marketplace_id="0393",
            is_active=True,
        )
    )
    await db_session.flush()

    assert (
        await resolve_master_sku_id(
            db_session,
            channel="wholesale",
            channel_sku="2700002488353",
            marketplace_id="0393",
        )
        == master
    )
    # And a different marketplace must NOT borrow another's mapping.
    assert (
        await resolve_master_sku_id(
            db_session,
            channel="wholesale",
            channel_sku="2700002488353",
            marketplace_id="0400",
        )
        is None
    )
    # Nor must "no marketplace" match a row that has one.
    assert (
        await resolve_master_sku_id(
            db_session,
            channel="wholesale",
            channel_sku="2700002488353",
        )
        is None
    )
