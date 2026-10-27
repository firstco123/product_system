"""分析ダッシュボード — 概要 (P2-009 / P2-011 / P2-012).

Everything on this page is read from `daily_kpi_snapshots` and friends, never
computed live from events. That is what makes a 365-day window answerable, and
it is also why the provenance banner exists: a materialised number is only as
current as the job that wrote it, and a stale dashboard looks exactly like a
fresh one.

Three states get their own band at the top rather than a footnote:

* **snapshot drift** — a stock figure here disagrees with the event log it
  derives from. Red, because nothing else on the page can be relied on until it
  is resolved.
* **stale rollup** — the numbers are real but old.
* **materially unmapped revenue** — above 5% of the total, the channel shares
  stop describing the business, and the fix is Rakuten SKU maintenance rather
  than anything on this screen.

The period ends YESTERDAY, never today. A partial day in a trend line reads as
a collapse in sales every single morning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.csv_export import csv_body, csv_response
from app.db import get_session
from app.services.analytics_query import (
    Delta,
    Kpis,
    SalesFilter,
    bucketed_sales,
    category_sales,
    channel_share,
    channels_in_period,
    daily_sales,
    period_kpis,
    provenance,
    sku_profile,
    sku_series,
    top_skus,
)
from app.services.analytics_query import provenance as load_provenance
from app.services.categories import load_overview
from app.services.sku_links import components_of, parents_of, search_skus
from app.services.timeframe import PRESET_DAYS, Period, resolve_period, to_jst_date
from app.services.velocity import (
    CONFIDENCE_LABELS,
    DEFAULT_COVER_DAYS,
    StockoutRisk,
    stockout_risks,
)
from app.ui import charts
from app.ui.auth import OperatorDep
from app.ui.deps import channel_label, templates

router = APIRouter(prefix="/analytics")

#: Current period vs the one immediately before it. Distinct colours rather
#: than one solid and one dashed: the comparison line is read as often as the
#: current one, and dashes disappear at this stroke width.
CURRENT_COLOR = "#4F46E5"
PREVIOUS_COLOR = "#CBD5E1"


def _series_for(period: Period, sales: dict) -> list[float]:  # type: ignore[type-arg]
    """One value per day in the window, with absent days as 0.

    A gap is rendered as zero HERE rather than skipped, because the two periods
    are drawn against a shared x-axis and a shorter series would silently
    compress the comparison line.
    """
    return [float(sales.get(day, 0)) for day in period.dates()]


async def _labelled_shares(session: AsyncSession, period: Period) -> list[tuple[str, Decimal]]:
    """Channel composition, with each channel named as the reader sees it.

    `channel_share` returns the stored keys alongside an already-Japanese
    unmapped row, so labelling here keeps the screen and its CSV from printing
    `rakuten` in the same column as `未マッピング`.
    """
    return [(channel_label(key), value) for key, value in await channel_share(session, period)]


@router.get("")
async def analytics_overview(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    period: str | None = None,
) -> Response:
    now = datetime.now(UTC)
    current = resolve_period(period, now=now)
    previous = current.previous()

    kpis = await period_kpis(session, current)
    prior = await period_kpis(session, previous)
    source = await provenance(session, now=now)
    shares = await _labelled_shares(session, current)

    current_sales = await daily_sales(session, current)
    previous_sales = await daily_sales(session, previous)

    # Both lines share the CURRENT period's labels. The comparison window is the
    # same length by construction, so position n in each is the nth day of its
    # own period — which is the comparison a reader wants.
    labels = [day.strftime("%m/%d") for day in current.dates()]
    trend = charts.line_chart(
        labels,
        [
            ("当期間", _series_for(current, current_sales), CURRENT_COLOR),
            ("前期間", _series_for(previous, previous_sales), PREVIOUS_COLOR),
        ],
    )

    return templates.TemplateResponse(
        request,
        "analytics_overview.html",
        {
            "operator": operator,
            "version": __version__,
            "period": current,
            "previous": previous,
            "presets": list(PRESET_DAYS),
            "kpis": kpis,
            "source": source,
            "trend": trend,
            "shares": charts.share_bars([(label, float(v)) for label, v in shares]),
            "deltas": overview_deltas(kpis, prior, previous_days=previous.days),
        },
    )


def overview_deltas(kpis: Kpis, prior: Kpis, *, previous_days: int) -> dict[str, Delta]:
    """Each change is computed on the number its tile shows.

    The 売上高 tile shows sales INCLUDING unmapped lines, but its change was
    computed on mapped sales only, so the percentage under it described a
    different figure — and one that moves whenever a mapping is resolved,
    although no sale changed. Found 2026-10-02 preparing the 検収.

    No change at all when the comparison period is not fully covered by data.
    The 365-day view compared a year against the eleven days of the previous
    year that the data reaches back to, and printed +620466.7% (production,
    2026-10-12). A percentage over a partial period is not a smaller truth; it
    is a different question, so the tile shows 「—」 instead.
    """
    if prior.days_with_data < previous_days:
        return {
            key: Delta(float(current), 0.0, None)
            for key, current in (
                ("sales", kpis.total_with_unmapped_jpy),
                ("quantity", kpis.sold_quantity),
                ("orders", kpis.order_count),
            )
        }
    return {
        "sales": Delta.of(kpis.total_with_unmapped_jpy, prior.total_with_unmapped_jpy),
        "quantity": Delta.of(kpis.sold_quantity, prior.sold_quantity),
        "orders": Delta.of(kpis.order_count, prior.order_count),
    }


#: Bucket sizes the detail screen offers. A month bucket over a 7-day window is
#: one bar, which is not wrong but is not a chart either — the template says so
#: rather than silently rejecting the combination, because the reader may be
#: switching presets and about to widen the window.
GRANULARITIES = (("day", "日次"), ("week", "週次"), ("month", "月次"))

#: The query-string value that means "SKUs with no category". Not an id, and
#: not the absence of the parameter, which means "no category filter" — see
#: SalesFilter on why those are three states and not two.
UNCLASSIFIED = "none"


@router.get("/sales")
async def analytics_sales(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    period: str | None = None,
    granularity: str = "day",
    channel: str | None = None,
    category: str | None = None,
    q: str = "",
) -> Response:
    now = datetime.now(UTC)
    current = resolve_period(period, now=now)

    if granularity not in {key for key, _ in GRANULARITIES}:
        # Same posture as resolve_period: these arrive from bookmarks and hand
        # edits, and a 500 is a worse answer than the default view.
        granularity = "day"

    unclassified = category == UNCLASSIFIED
    category_id: int | None = None
    if category and not unclassified and category.isdigit():
        category_id = int(category)

    where = SalesFilter(
        channel=channel or None,
        category_id=category_id,
        unclassified_only=unclassified,
    )

    buckets = await bucketed_sales(session, current, granularity=granularity, where=where)
    rows = await top_skus(session, current, where=where)
    channels = await channels_in_period(session, current)
    categories = await load_overview(session)

    labels = [b.bucket.strftime("%m/%d") for b in buckets]
    trend = charts.line_chart(
        labels,
        [("売上高", [float(b.gross_sales_jpy) for b in buckets], CURRENT_COLOR)],
    )

    return templates.TemplateResponse(
        request,
        "analytics_sales.html",
        {
            "operator": operator,
            "version": __version__,
            "period": current,
            "presets": list(PRESET_DAYS),
            "granularity": granularity,
            "granularities": GRANULARITIES,
            "channels": channels,
            "categories": categories,
            "filter": where,
            "selected_category": category or "",
            "unclassified_value": UNCLASSIFIED,
            "buckets": buckets,
            # Finding a product's page, sets included — the stock screens no
            # longer list sets (acceptance Q&A, 2026-10-12). Does not narrow
            # the chart or the table; it only lists matches to open.
            "q": q,
            "hits": await search_skus(session, q) if q.strip() else [],
            "rows": rows,
            "trend": trend,
            "period_quantity": sum(b.quantity for b in buckets),
            "period_sales": sum((b.gross_sales_jpy for b in buckets), start=Decimal(0)),
        },
    )


def _resolve_filter(channel: str | None, category: str | None) -> SalesFilter:
    """The query string as a SalesFilter. Shared by the screen and its exports
    so a download can never describe a different slice than the page it came
    from — the failure nobody notices until two numbers are compared in a
    meeting."""
    unclassified = category == UNCLASSIFIED
    category_id: int | None = None
    if category and not unclassified and category.isdigit():
        category_id = int(category)
    return SalesFilter(
        channel=channel or None,
        category_id=category_id,
        unclassified_only=unclassified,
    )


def _slice_note(period: Period, where: SalesFilter, granularity: str | None = None) -> list[str]:
    """A header row naming what the file contains.

    Every export here is a SLICE — a window, sometimes a channel, sometimes one
    category — and a bare table of numbers in a spreadsheet loses all of that
    the moment it is forwarded. Without it, two exports of the same screen taken
    a week apart are indistinguishable.
    """
    parts = [f"期間: {period.first_day} 〜 {period.last_day}"]
    if granularity:
        parts.append(f"粒度: {granularity}")
    parts.append(f"チャネル: {channel_label(where.channel) if where.channel else 'すべて'}")
    if where.unclassified_only:
        parts.append("カテゴリ: 未分類のみ")
    elif where.category_id is not None:
        parts.append(f"カテゴリID: {where.category_id}")
    else:
        parts.append("カテゴリ: すべて")
    return parts


@router.get("/sales/export.csv")
async def analytics_sales_export(
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    period: str | None = None,
    channel: str | None = None,
    category: str | None = None,
) -> Response:
    """The SKU table, with the screen's filters and WITHOUT its top-50 cut."""
    current = resolve_period(period, now=datetime.now(UTC))
    where = _resolve_filter(channel, category)
    rows = await top_skus(session, current, limit=None, where=where)

    body = csv_body(
        ["SKUコード", "商品名", "カテゴリ", "販売数量", "売上高(円)"],
        [
            [r.sku_code, r.name, r.category_name or "未分類", r.quantity, int(r.gross_sales_jpy)]
            for r in rows
        ],
    )
    note = csv_body(["# " + " / ".join(_slice_note(current, where))], [])
    name = f"sales_by_sku_{current.first_day}_{current.last_day}.csv"
    return csv_response(note + body, filename=name)


@router.get("/sales/trend.csv")
async def analytics_trend_export(
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    period: str | None = None,
    granularity: str = "day",
    channel: str | None = None,
    category: str | None = None,
) -> Response:
    """The trend series behind the chart, at the granularity on screen."""
    current = resolve_period(period, now=datetime.now(UTC))
    if granularity not in {key for key, _ in GRANULARITIES}:
        granularity = "day"
    where = _resolve_filter(channel, category)
    buckets = await bucketed_sales(session, current, granularity=granularity, where=where)

    body = csv_body(
        ["期間開始", "受注件数", "販売数量", "売上高(円)"],
        [
            [b.bucket.isoformat(), b.order_count, b.quantity, int(b.gross_sales_jpy)]
            for b in buckets
        ],
    )
    note = csv_body(["# " + " / ".join(_slice_note(current, where, granularity))], [])
    name = f"sales_trend_{current.first_day}_{current.last_day}.csv"
    return csv_response(note + body, filename=name)


@router.get("/export.csv")
async def analytics_overview_export(
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    period: str | None = None,
) -> Response:
    """Channel composition, unmapped row included.

    Included rather than dropped for the same reason it is on the screen: the
    parts have to sum to the headline total, and a spreadsheet is exactly where
    someone will add them up.
    """
    current = resolve_period(period, now=datetime.now(UTC))
    shares = await _labelled_shares(session, current)
    total = sum((value for _, value in shares), start=Decimal(0))

    body = csv_body(
        ["チャネル", "売上高(円)", "構成比(%)"],
        [
            [label, int(value), f"{(float(value) / float(total) * 100):.1f}" if total else "0.0"]
            for label, value in shares
        ],
    )
    note = csv_body([f"# 期間: {current.first_day} 〜 {current.last_day}"], [])
    name = f"channel_share_{current.first_day}_{current.last_day}.csv"
    return csv_response(note + body, filename=name)


#: Stock is a level and sales are a flow, so they never share an axis. Two
#: charts stacked, not one with a second scale: a dual axis lets any pair of
#: unrelated series be made to look correlated by choosing the scaling.
STOCK_COLOR = "#0EA5E9"


@router.get("/sku/{master_sku_id}")
async def analytics_sku(
    request: Request,
    operator: OperatorDep,
    master_sku_id: int,
    session: AsyncSession = Depends(get_session),
    period: str | None = None,
) -> Response:
    current = resolve_period(period, now=datetime.now(UTC))
    profile = await sku_profile(session, master_sku_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="SKU not found")

    days = await sku_series(session, master_sku_id, current)
    labels = [d.stat_date.strftime("%m/%d") for d in days]

    # A day with no stock row carries None. Carrying the last known level
    # forward would draw a line through a period we have no record of; zero
    # would invent an out-of-stock day. Neither is honest, so the gap is drawn
    # at the level it was last seen and the footnote says how many days lack data.
    # A set and its components link to each other: a set's page is where the
    # client lands from search, and its stock lives in the components.
    components = (await components_of(session, [master_sku_id])).get(master_sku_id, [])
    used_by = (await parents_of(session, [master_sku_id])).get(master_sku_id, [])

    last_known = 0.0
    stock_values: list[float] = []
    for day in days:
        if day.on_hand_qty is not None:
            last_known = float(day.on_hand_qty)
        stock_values.append(last_known)

    return templates.TemplateResponse(
        request,
        "analytics_sku.html",
        {
            "operator": operator,
            "version": __version__,
            "period": current,
            "presets": list(PRESET_DAYS),
            "profile": profile,
            "days": days,
            "missing_stock_days": sum(1 for d in days if d.on_hand_qty is None),
            "stock_chart": charts.line_chart(
                labels, [("在庫数", stock_values, STOCK_COLOR)] if profile.tracks_stock else []
            ),
            "sales_chart": charts.line_chart(
                labels, [("売上高", [float(d.gross_sales_jpy) for d in days], CURRENT_COLOR)]
            ),
            "period_quantity": sum(d.quantity for d in days),
            "period_sales": sum((d.gross_sales_jpy for d in days), start=Decimal(0)),
            "components": components,
            "used_by": used_by,
        },
    )


@router.get("/categories")
async def analytics_categories(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    period: str | None = None,
    channel: str | None = None,
) -> Response:
    current = resolve_period(period, now=datetime.now(UTC))
    where = SalesFilter(channel=channel or None)
    rows = await category_sales(session, current, where=where)
    channels = await channels_in_period(session, current)

    total = sum((r.total_sales_jpy for r in rows), start=Decimal(0))
    return templates.TemplateResponse(
        request,
        "analytics_categories.html",
        {
            "operator": operator,
            "version": __version__,
            "period": current,
            "presets": list(PRESET_DAYS),
            "channels": channels,
            "filter": where,
            "rows": rows,
            "total": total,
            "shares": charts.share_bars([(r.name, float(r.total_sales_jpy)) for r in rows]),
        },
    )


def risk_counts(risks: list[StockoutRisk]) -> dict[str, int]:
    """The three counters above the risk table, over EVERY at-risk SKU."""
    return {
        "out_of_stock": sum(1 for r in risks if r.on_hand_qty <= 0),
        "below_threshold": sum(1 for r in risks if r.is_below_threshold),
        "unforecastable": sum(1 for r in risks if r.days_remaining is None),
    }


#: How many rows the risk screen shows. Triage, not an inventory listing — a
#: page of 600 rows sorted by urgency is read as far as the first screenful
#: either way, and the CSV carries the rest.
RISK_LIMIT = 100


@router.get("/stockout-risk")
async def analytics_stockout_risk(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    period: str | None = None,
    cover: int = DEFAULT_COVER_DAYS,
) -> Response:
    """欠品リスク一覧 (P2-018).

    Ordered by urgency rather than by SKU: already out, then soonest to run out,
    then everything with no forecast. The ones with no forecast stay on the page
    — a SKU has no days-remaining precisely BECAUSE it has already run out, and
    dropping them would take the most urgent rows off a triage screen.
    """
    now = datetime.now(UTC)
    current = resolve_period(period, now=now)
    cover_days = max(1, min(90, cover))

    # The whole list, then the page. The counters were computed from the 100
    # rows shown, so with 100+ SKUs out of stock all three read exactly 100
    # (2026-10-03 responsive review). The full list is built anyway before
    # any slicing; counting it costs nothing extra.
    every_risk = await stockout_risks(
        session, current, today=to_jst_date(now), cover_days=cover_days
    )
    risks = every_risk[:RISK_LIMIT]
    source = await load_provenance(session, now=now)

    return templates.TemplateResponse(
        request,
        "analytics_stockout_risk.html",
        {
            "operator": operator,
            "version": __version__,
            "period": current,
            "presets": list(PRESET_DAYS),
            "cover_days": cover_days,
            "risks": risks,
            "source": source,
            "labels": CONFIDENCE_LABELS,
            "limit": RISK_LIMIT,
            **risk_counts(every_risk),
        },
    )


@router.get("/stockout-risk/export.csv")
async def analytics_stockout_risk_export(
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    period: str | None = None,
    cover: int = DEFAULT_COVER_DAYS,
) -> Response:
    """Every at-risk SKU, not the screen's first hundred."""
    now = datetime.now(UTC)
    current = resolve_period(period, now=now)
    cover_days = max(1, min(90, cover))
    risks = await stockout_risks(session, current, today=to_jst_date(now), cover_days=cover_days)

    body = csv_body(
        [
            "SKUコード",
            "商品名",
            "在庫数",
            "販売速度(個/日)",
            "データ十分性",
            "低在庫閾値",
            "残日数",
            "欠品予測日",
        ],
        [
            [
                r.sku_code,
                r.name,
                r.on_hand_qty,
                f"{r.velocity.per_day:.2f}",
                CONFIDENCE_LABELS[r.velocity.confidence],
                r.threshold,
                f"{r.days_remaining:.1f}" if r.days_remaining is not None else "",
                r.stockout_on.isoformat() if r.stockout_on else "",
            ]
            for r in risks
        ],
    )
    note = csv_body(
        [
            f"# 期間: {current.first_day} 〜 {current.last_day}"
            f" / 補充カバー日数: {cover_days}日"
            " / 残日数が空欄のSKUは販売実績またはデータ期間が不足しています"
        ],
        [],
    )
    name = f"stockout_risk_{current.first_day}_{current.last_day}.csv"
    return csv_response(note + body, filename=name)
