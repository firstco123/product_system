"""Hold a master's automatic channel stock writes, without disabling the master.

Client decision of 2026-11-19: for the Christmas selling period they will set the
48 gift sets' Shopify quantities BY HAND, and asked whether that breaks what we
built. It does not, but only because the two halves of the feature are separate
mechanisms:

    引き落とし   a set sells -> its components' stock goes down
    書き込み     the figure derived from those components -> the channel page

They want the second one held and the first one kept. This CLI is that lever: it
sets `master_skus.stock_push_paused_at`, which `sku_scope.auto_push_conditions`
removes from `all_bundle_ids` and `masters_mapped_on` — the two populations the
unattended jobs push — and nothing else.

WHY NOT `mark_non_inventory` / `is_stock_managed = false`

Because it reads like the right flag and is the one way to get this exactly
backwards. `InventoryService.resolve_consumption` tests `is_stock_managed` on the
PARENT and returns an empty list before it ever reads `bundle_components`, so a
set flagged that way stops deducting its components on every sale. The shop would
go on selling sets all season and the bracelets would never leave stock on paper.
That is the half the client wants kept.

WHY NOT DEACTIVATING THE MAPPING ROWS

`ingest._lookup_mapping` resolves an incoming Shopify line through exactly those
rows. Deactivate them and every set order parks in `pending_mapping`, deducting
nothing — the same failure by a different route, plus a MappingAlert per order.

WHAT A PAUSE DOES NOT STOP

Operator-driven writes, on purpose. The 同期エラー screen's 再実行 button and an
approved reconcile adjustment are a person asking for one write with the number
in front of them. A pause protects against the unattended hourly job; it is not
a lock against the operator.

Usage (always via scripts/run_cli.ps1 — there is no local product_system DB):
    # 48件を確認する (読み取りのみ)
    scripts\run_cli.ps1 -Cli pause_stock_push -Args "--csv <gift set CSV>"
    # 停止する (-Apply が --apply を付けるので --apply は書かない)
    scripts\run_cli.ps1 -Cli pause_stock_push -Apply -Args "--csv <csv>"
    # いま停止中のものを一覧する
    scripts\run_cli.ps1 -Cli pause_stock_push -Args "--list"
    # 1月に再開する
    scripts\run_cli.ps1 -Cli pause_stock_push -Apply -Args "--csv <csv> --resume"

`--csv` reads one column of SKU codes and ignores every other column, so the
gift-set registration file itself is a valid input: its セットSKU column names
the 48 sets and `csv_intake` skips its `#` header lines. Duplicate codes — that
file has one row per component — collapse to one master.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.csv_intake import ColumnSpec, CsvSpec, inspect, iter_rows
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MasterSku

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

DEFAULT_REASON = "手動運用のため自動書き込みを停止"

SKU_CSV = CsvSpec(
    columns=(
        ColumnSpec(
            canonical="sku_code",
            aliases=("セットSKU", "set_sku", "SKUコード", "SKU", "商品コード", "sku"),
        ),
    )
)


@dataclass(slots=True)
class PauseResult:
    requested: int = 0
    changed: int = 0
    already: int = 0
    not_found: list[str] = field(default_factory=list)
    #: "01gift-...=shopify x1" per master, so the report names what stops being
    #: written rather than only how many rows moved. A pause on a master with no
    #: active mapping changes nothing observable and is worth seeing as such.
    items: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)


def read_csv(path: pathlib.Path) -> list[str]:
    """The SKU codes in the file, de-duplicated, order preserved.

    `inspect` runs first and its fatal findings are raised rather than skipped:
    `iter_rows` silently drops a row it cannot read, and silently pausing 47 of
    48 sets is the failure that would not be noticed until the shop oversold the
    48th.
    """
    data = path.read_bytes()
    report = inspect(data, SKU_CSV)
    if report.fatal:
        raise ValueError(" / ".join(report.fatal))
    if report.row_issues:
        lines = ", ".join(str(issue.get("line")) for issue in report.row_issues[:20])
        raise ValueError(f"読み取れない行があります (行 {lines})")
    seen: dict[str, None] = {}
    for row in iter_rows(data, SKU_CSV):
        seen.setdefault(row["sku_code"], None)
    return list(seen)


async def _active_mappings(session: AsyncSession, master_ids: list[int]) -> dict[int, list[str]]:
    """master_sku_id -> the channels it is actively mapped on."""
    if not master_ids:
        return {}
    rows = (
        await session.execute(
            select(ChannelSkuMapping.master_sku_id, ChannelSkuMapping.channel)
            .where(
                ChannelSkuMapping.master_sku_id.in_(master_ids),
                ChannelSkuMapping.is_active.is_(True),
            )
            .distinct()
        )
    ).all()
    channels: dict[int, list[str]] = {}
    for master_sku_id, channel in rows:
        channels.setdefault(master_sku_id, []).append(channel)
    return {mid: sorted(names) for mid, names in channels.items()}


async def run(
    *,
    sku_codes: list[str],
    apply: bool,
    resume: bool = False,
    reason: str = DEFAULT_REASON,
    session_factory: SessionFactory | None = None,
) -> int:
    result = PauseResult(requested=len(sku_codes))
    factory = session_factory or async_session_factory

    async with factory() as session, session.begin():
        rows = (
            await session.execute(
                select(MasterSku).where(MasterSku.sku_code.in_(sku_codes or [""]))
            )
        ).scalars()
        found = {master.sku_code: master for master in rows}
        result.not_found = sorted(set(sku_codes) - set(found))
        channels = await _active_mappings(session, [m.id for m in found.values()])

        now = datetime.now(UTC)
        for sku_code in sku_codes:
            master = found.get(sku_code)
            if master is None:
                continue
            is_paused = master.stock_push_paused_at is not None
            target_paused = not resume
            if is_paused is target_paused:
                # Already in the target state. A second pause must NOT refresh
                # the timestamp: "paused since" is the only thing that makes a
                # hold left on past its season visible, and a re-run of the
                # command would quietly reset the clock.
                result.already += 1
                continue
            result.changed += 1
            mapped_on = channels.get(master.id, [])
            if mapped_on:
                result.items.append(f"{sku_code}={'+'.join(mapped_on)}")
            else:
                result.unmapped.append(sku_code)
            if not apply:
                continue
            if resume:
                master.stock_push_paused_at = None
                master.stock_push_paused_reason = None
            else:
                master.stock_push_paused_at = now
                master.stock_push_paused_reason = reason

        log.info(
            "pause_stock_push.report" if not apply else "pause_stock_push.done",
            mode="resume" if resume else "pause",
            requested=result.requested,
            would_change=result.changed if not apply else None,
            changed=result.changed if apply else None,
            already_in_state=result.already,
            reason=None if resume else reason,
            items=result.items[:60],
            unmapped=result.unmapped[:60],
            not_found=result.not_found[:30],
        )
    return 1 if result.not_found else 0


async def show_paused(*, session_factory: SessionFactory | None = None) -> int:
    """Everything currently held, oldest hold first.

    Oldest first because the finding this exists for is a hold nobody lifted:
    a set still manual in March deducts correctly and shows a figure on every
    screen, so nothing else in the system will ever complain about it.
    """
    factory = session_factory or async_session_factory
    async with factory() as session:
        rows = (
            await session.execute(
                select(MasterSku)
                .where(MasterSku.stock_push_paused_at.is_not(None))
                .order_by(MasterSku.stock_push_paused_at, MasterSku.sku_code)
            )
        ).scalars()
        items = [
            {
                "sku_code": master.sku_code,
                "paused_at": master.stock_push_paused_at.isoformat()
                if master.stock_push_paused_at
                else None,
                "reason": master.stock_push_paused_reason,
            }
            for master in rows
        ]
    log.info("pause_stock_push.list", paused=len(items), items=items[:100])
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Hold automatic channel stock writes per master")
    p.add_argument("--csv", type=pathlib.Path, help="a CSV with a SKU-code column")
    p.add_argument("--sku", action="append", default=[], help="a SKU code (repeatable)")
    p.add_argument("--apply", action="store_true", help="write (default: report only)")
    p.add_argument("--resume", action="store_true", help="lift the hold instead of taking it")
    p.add_argument("--reason", default=DEFAULT_REASON)
    p.add_argument("--list", action="store_true", dest="list_paused", help="show current holds")
    args = p.parse_args()
    configure_logging("INFO")

    if args.list_paused:
        sys.exit(asyncio.run(show_paused()))
    codes = read_csv(args.csv) if args.csv else []
    codes += [code for code in args.sku if code not in set(codes)]
    if not codes:
        p.error("--csv または --sku のいずれかが必要です")
    sys.exit(
        asyncio.run(run(sku_codes=codes, apply=args.apply, resume=args.resume, reason=args.reason))
    )


if __name__ == "__main__":
    main()
