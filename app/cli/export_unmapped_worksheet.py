"""Export the marketplace unmapped-sales worksheet the client fills in.

The defect CSV mixes three 区分 and cannot be filled in and returned. What the
client actually needs is one sheet listing only the keys we cannot resolve, with
an empty column for the answer.

Scope is deliberately narrower than the defect report's 未マッピング実績 block.
That block lists every unresolved key, but some of those already HAVE an active
mapping and are unmapped only because the order predates it. Those need a remap
from us and nothing from the client, so asking about them wastes their time and
invites a confusing answer. `--all` includes them, for our own review only.

商品名 comes from mapping_alerts, which is where the Rakuten payload's itemName
is retained — order_items has no name column. It is a LEFT JOIN in effect: a key
with no alert still belongs on the sheet, just without a name.

最終受注日 earns its column. The question we are asking is "is this page still
live, or was it deleted?", and a key last sold eighteen months ago answers it
before the client has to look anything up.

Scope is also narrower than the 未マッピング figure on the analytics screen,
which counts every channel. 卸売 is the case that makes the difference matter:
its importer writes an order line for every voucher line, mapped or not
(`docs/29 §6`), so unmapped wholesale lines reach the same rollup and show up in
that figure — but a 取引先商品コード is resolved from the carrier's own files by
`build_wholesale_mapping`, not by asking the client, so it does not belong on
this sheet. The run reports any such revenue rather than leaving the sheet to be
read as the whole backlog.

Reads the database; writes one file.

Usage (via the Cloud SQL proxy):
    py -m app.cli.export_unmapped_worksheet
    py -m app.cli.export_unmapped_worksheet --all --out some/other.csv
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Numeric, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.csv_export import csv_body, write_csv
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MappingAlert, Order, OrderItem
from app.services.timeframe import to_jst_date

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

#: Channels the sheet can cover. Shopify belongs here too: after the 2026-09
#: re-resolution the remaining backlog was 177 keys across BOTH channels, and
#: a Rakuten-only sheet would have asked the client about part of it while
#: silently dropping the rest.
CHANNELS = ("rakuten", "shopify")
DEFAULT_OUT = Path("csv_file/phase2/unmapped_worksheet.csv")

HEADER = (
    "チャネル",
    # Not 商品管理番号: that is Rakuten's word for it, and this sheet now also
    # carries Shopify SKUs.
    "チャネルの商品コード",
    "商品名",
    "受注明細数",
    "数量合計",
    "金額合計",
    "初回受注日",
    "最終受注日",
    "正しい商品コード ご記入ください",
    "備考",
)


@dataclass(frozen=True, slots=True)
class WorksheetRow:
    channel: str
    manage_number: str
    product_name: str
    lines: int
    quantity: int
    amount: int
    first_date: str
    last_date: str

    def as_csv(self) -> tuple[str | int, ...]:
        # The last two are the client's to fill: 商品コード, and 備考 where
        # "対象外" marks a page they have since deleted.
        return (
            self.channel,
            self.manage_number,
            self.product_name,
            self.lines,
            self.quantity,
            self.amount,
            self.first_date,
            self.last_date,
            "",
            "",
        )


def _jst(moment: datetime | None) -> str:
    return to_jst_date(moment).isoformat() if moment else ""


@dataclass(frozen=True, slots=True)
class UncoveredChannel:
    """Unmapped revenue on a channel this sheet deliberately does not carry."""

    channel: str
    keys: int
    lines: int
    amount: int


@dataclass(frozen=True, slots=True)
class WorksheetData:
    """What one run read: the sheet's rows, and what the sheet leaves out."""

    rows: list[WorksheetRow]
    uncovered: list[UncoveredChannel]


async def collect_uncovered(
    session: AsyncSession, *, channels: tuple[str, ...] = CHANNELS
) -> list[UncoveredChannel]:
    """Unmapped revenue per channel the sheet is NOT asking about.

    Counted with the same two predicates as the sheet itself — a NULL master on
    a live line — so the two numbers are comparable, and summarised per channel
    rather than per key because nobody is being asked to fill these in.
    """
    rows = await session.execute(
        select(
            Order.channel,
            func.count(func.distinct(OrderItem.channel_sku)).label("keys"),
            func.count().label("lines"),
            func.sum(OrderItem.quantity * OrderItem.unit_price)
            .cast(Numeric(14, 2))
            .label("amount"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.channel.not_in(channels), OrderItem.master_sku_id.is_(None))
        .group_by(Order.channel)
        .order_by(Order.channel)
    )
    return [
        UncoveredChannel(
            channel=row.channel,
            keys=int(row.keys or 0),
            lines=int(row.lines or 0),
            amount=int(row.amount or 0),
        )
        for row in rows.all()
    ]


async def collect_rows(
    session: AsyncSession, *, include_mapped_keys: bool, channels: tuple[str, ...] = CHANNELS
) -> list[WorksheetRow]:
    """One row per unresolved channel_sku, most recent activity first."""
    stmt = (
        select(
            Order.channel,
            OrderItem.channel_sku,
            func.count().label("lines"),
            func.sum(OrderItem.quantity).label("quantity"),
            func.sum(OrderItem.quantity * OrderItem.unit_price)
            .cast(Numeric(14, 2))
            .label("amount"),
            func.min(Order.ordered_at).label("first_at"),
            func.max(Order.ordered_at).label("last_at"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.channel.in_(channels), OrderItem.master_sku_id.is_(None))
        .group_by(Order.channel, OrderItem.channel_sku)
        .order_by(func.max(Order.ordered_at).desc())
    )
    if not include_mapped_keys:
        stmt = stmt.where(
            OrderItem.channel_sku.not_in(
                select(ChannelSkuMapping.channel_sku).where(
                    ChannelSkuMapping.channel.in_(channels),
                    ChannelSkuMapping.is_active.is_(True),
                )
            )
        )
    aggregated = (await session.execute(stmt)).all()
    if not aggregated:
        return []

    # Names in one query rather than one per key.
    name_rows = (
        await session.execute(
            select(MappingAlert.channel_sku, func.max(MappingAlert.product_name))
            .where(
                MappingAlert.channel.in_(channels),
                MappingAlert.channel_sku.in_([row.channel_sku for row in aggregated]),
            )
            .group_by(MappingAlert.channel_sku)
        )
    ).all()
    names: dict[str, str] = {key: value for key, value in name_rows if value}

    return [
        WorksheetRow(
            channel=row.channel,
            manage_number=row.channel_sku,
            product_name=names.get(row.channel_sku, ""),
            lines=int(row.lines or 0),
            quantity=int(row.quantity or 0),
            # No decimal point: these are yen, and Excel renders 12000.00 as a
            # price the client then has to mentally strip.
            amount=int(row.amount or 0),
            first_date=_jst(row.first_at),
            last_date=_jst(row.last_at),
        )
        for row in aggregated
    ]


async def fetch(
    *,
    include_mapped_keys: bool = False,
    session_factory: SessionFactory | None = None,
) -> WorksheetData:
    """The database half, kept separate so the coroutine does no file IO."""
    factory = session_factory or async_session_factory
    async with factory() as session:
        return WorksheetData(
            rows=await collect_rows(session, include_mapped_keys=include_mapped_keys),
            uncovered=await collect_uncovered(session),
        )


def write_worksheet(rows: list[WorksheetRow], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 + BOM: Excel decides a CSV's encoding by the BOM alone and mangles
    # Japanese without it, whatever we intended. See app/ui/csv_export.py.
    body = csv_body(HEADER, [row.as_csv() for row in rows])
    write_csv(out, body)


def run(
    *,
    out: Path,
    include_mapped_keys: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    data = asyncio.run(
        fetch(include_mapped_keys=include_mapped_keys, session_factory=session_factory)
    )
    rows = data.rows
    write_worksheet(rows, out)

    lines = sum(row.lines for row in rows)
    quantity = sum(row.quantity for row in rows)
    log.info(
        "unmapped_worksheet.written",
        path=str(out),
        keys=len(rows),
        lines=lines,
        quantity=quantity,
        include_mapped_keys=include_mapped_keys,
    )
    print(f"\n  {out}  ({len(rows)} 件)")
    if rows:
        print(f"  受注明細 {lines} 行 / 数量合計 {quantity}")
        print(f"  最終受注日 {rows[-1].last_date} 〜 {rows[0].last_date}")
        named = sum(1 for row in rows if row.product_name)
        print(f"  商品名あり {named} / {len(rows)}")
    for channel in data.uncovered:
        # Printed even though it is not actionable here: the analytics screen's
        # 未マッピング figure includes this revenue, so a sheet that omitted it
        # without saying so would read as the complete backlog.
        log.info(
            "unmapped_worksheet.uncovered_channel",
            channel=channel.channel,
            keys=channel.keys,
            lines=channel.lines,
            amount=channel.amount,
        )
        print(
            f"  [対象外] {channel.channel}: 未マッピング {channel.keys} コード / "
            f"{channel.lines} 行 / {channel.amount:,} 円 — このシートには含みません"
        )
    if data.uncovered:
        print("  卸売は build_wholesale_mapping で対応表を作成してください。docs/29 §5 を参照。")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Export the unmapped-sales worksheet")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument(
        "--all",
        dest="include_mapped_keys",
        action="store_true",
        help="Include keys that already have a mapping (we remap those; do not send).",
    )
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(run(out=args.out, include_mapped_keys=args.include_mapped_keys))


if __name__ == "__main__":
    main()
