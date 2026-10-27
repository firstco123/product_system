"""Import one 流通BMS file: read, plan, show, apply (docs/29 §4, §6, §7, §8).

The operational entry point for the wholesale import, and the thing the §8
screen will call. Runs the same four steps that screen will:
upload → 検証 → 確認 → 実行. Here 確認 is `--dry-run`, which prints the plan and
writes nothing; dropping the flag applies it.

THE CUTOVER IS NOT ASKED FOR

`--cutover` exists but is not the normal path. By default the date is derived
from the applied stocktake (`app.services.wholesale_cutover`), because that date
is the one input that can silently inflate stock: the history the client already
sent covers 受注 8/16–9/14 and 返品 6/30–9/9, and importing it would leave stock
3,045 units too high (docs/29 §7-2). A parameter someone types once a month is a
parameter that will eventually be typed wrong, and the result reads like
ordinary stock.

An explicit `--cutover` is still allowed — the multi-date stocktake case needs
one — but it is checked against the same October floor and logged as an
override.

WHAT TO READ IN THE OUTPUT

    対象              vouchers and lines that will move
    カットオーバー前    dropped for being dated before the cutover (§7-2)
    既に取込済み        already on record; a re-upload is a no-op, not an import
    未マッピング        the code resolves to no master. Stock does not move, but
                      the sale is still recorded — fix the correspondence table
                      and re-run to attach it (§10-19)
    在庫管理対象外      mapped, but holds no stock. The gift bag (§5-4)
    マイナスになる      will take a master below zero. Applied anyway, because the
                      goods shipped — §10-9 says why, and this is the number to
                      look at before approving
    棚卸日と同日        dated on the cutover day itself, so possibly counted
                      already (§10-10, a question still open with the client)

    powershell -File scripts/run_cli.ps1 -Cli import_wholesale_file ^
        -Args "--file csv_file/phase2/source/受注伝票_20261116.csv --dry-run"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.services.wholesale_cutover import check_override, resolve_cutover
from app.services.wholesale_import import WholesaleImportService, WholesalePlan
from app.wholesale_bms import RULES, BmsReading, read_bms

log = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


def parse_cutover(text: str) -> date:
    return datetime.strptime(text.strip(), "%Y-%m-%d").date()


def _describe(reading: BmsReading, plan: WholesalePlan) -> list[str]:
    shape = plan.as_dict()
    lines = [
        f"種別              {RULES[plan.kind].label}",
        f"カットオーバー      {shape['cutover_on']}",
        f"ファイル全体        {reading.total_rows}行 / 有効 {reading.valid_rows}行"
        f" / 数量0 {reading.zero_quantity_rows}行",
        f"対象              {shape['voucher_count']}伝票 "
        f"{shape['movable_line_count']}明細 (数量 {shape['total_delta']:+d})",
        f"カットオーバー前    {shape['excluded_voucher_count']}伝票 "
        f"{shape['excluded_line_count']}明細 … 取り込みません",
        f"既に取込済み        {shape['already_applied_line_count']}明細",
        f"未マッピング        {shape['unmapped_line_count']}明細",
        f"在庫管理対象外      {shape['unmanaged_line_count']}明細",
        f"マイナスになる      {shape['negative_line_count']}明細",
        f"棚卸日と同日        {shape['lines_on_cutover_count']}明細",
    ]
    if shape["unmapped_codes"]:
        lines.append("未マッピングの商品コード:")
        lines.extend(
            f"    {code} … {count}明細" for code, count in sorted(shape["unmapped_codes"].items())
        )
    if plan.negative_lines:
        lines.append("マイナスになる明細 (先頭20件):")
        lines.extend(
            f"    {line.sku_code} {line.current_qty} → {line.projected_qty}"
            f"  ({line.line.voucher}-{line.line.voucher_line})"
            for line in plan.negative_lines[:20]
        )
    return lines


async def run(
    *,
    data: bytes,
    dry_run: bool,
    operator: str,
    cutover_on: date | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    reading = read_bms(data)
    if reading.fatal:
        for message in reading.fatal:
            log.error("wholesale_file.unreadable", reason=message)
        print("取込を中止しました:")
        for message in reading.fatal:
            print(f"    {message}")
        return 1
    for issue in reading.row_issues:
        log.warning("wholesale_file.row_issue", **issue)

    factory = session_factory or async_session_factory
    async with factory() as session:
        if cutover_on is not None:
            refusal = check_override(cutover_on)
            if refusal is not None:
                log.error("wholesale_file.cutover_refused", reason=refusal)
                print(refusal)
                return 1
            log.warning(
                "wholesale_file.cutover_overridden",
                cutover_on=cutover_on.isoformat(),
                operator=operator,
            )
            resolved = cutover_on
        else:
            resolution = await resolve_cutover(session)
            if not resolution.ok:
                log.error(
                    "wholesale_file.no_cutover",
                    reason=resolution.reason,
                    counted_on=[day.isoformat() for day in resolution.counted_on],
                )
                print(resolution.reason)
                if resolution.counted_on:
                    days = " / ".join(day.isoformat() for day in resolution.counted_on)
                    print(f"確定済みの棚卸日: {days}")
                return 1
            assert resolution.cutover_on is not None  # narrowed by `ok`
            resolved = resolution.cutover_on
            log.info("wholesale_file.cutover_resolved", cutover_on=resolved.isoformat())

        plan = await WholesaleImportService(session).plan(reading, cutover_on=resolved)
        for text in _describe(reading, plan):
            print(text)

        if dry_run:
            print("--dry-run のため何も書いていません。")
            return 0

        result = await WholesaleImportService(session).apply(plan, operator=operator)
        await session.commit()

    print(
        f"適用しました: 在庫 {result.applied_lines}明細 "
        f"(イベント {result.events_written}件, 数量 {result.total_delta:+d}) / "
        f"売上 {result.orders_written}伝票 {result.order_lines_written}明細 "
        f"(master補完 {result.order_lines_repaired}明細) / "
        f"対象外 {result.unmanaged_lines}明細 / 重複 {result.duplicate_lines}明細"
    )
    log.info(
        "wholesale_file.applied",
        operator=operator,
        cutover_on=resolved.isoformat(),
        applied_lines=result.applied_lines,
        events_written=result.events_written,
        total_delta=result.total_delta,
        orders_written=result.orders_written,
        order_lines_written=result.order_lines_written,
        order_lines_repaired=result.order_lines_repaired,
        unmanaged_lines=result.unmanaged_lines,
        duplicate_lines=result.duplicate_lines,
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Import one 流通BMS wholesale file")
    p.add_argument("--file", required=True, help="受注伝票 or 受領・返品 as the carrier sent it")
    p.add_argument("--dry-run", action="store_true", help="show the plan and write nothing")
    p.add_argument("--operator", default="cli", help="recorded on the import")
    p.add_argument(
        "--cutover",
        default=None,
        help=(
            "YYYY-MM-DD, overriding the date derived from the applied stocktake. "
            "Needed when the stocktake spans several days; refused before "
            "2026-10-01 either way (docs/29 §7-2)"
        ),
    )
    args = p.parse_args()
    configure_logging("INFO")
    path = Path(args.file)
    if not path.exists():
        log.error("wholesale_file.missing", path=str(path))
        sys.exit(1)
    sys.exit(
        asyncio.run(
            run(
                data=path.read_bytes(),
                dry_run=args.dry_run,
                operator=args.operator,
                cutover_on=parse_cutover(args.cutover) if args.cutover else None,
            )
        )
    )


if __name__ == "__main__":
    main()
