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

WITHDRAWING AN IMPORT

`--reverse` undoes what a past run of this same command wrote. It is the
recovery path for the cutover, which is set once and cannot be clicked back:
import against a date earlier than the real count and the count's own movements
are replayed on top of it, which on the client's history measures 3,045 units of
inflated stock (§7-2).

The withdrawal is anchored on the FILE, because nothing records which vouchers
an import touched. So it must be given the same file AND the same cutover the
import used; `--cutover` is the normal path here, not the exception. Hand it a
different cutover and the plan names a different voucher set — refused rather
than applied, see 取込済みで対象外 below.

A WITHDRAWN LINE CANNOT BE IMPORTED AGAIN

`reverse` compensates the import's events rather than deleting them, so their
`uq_inventory_event_source` identity stays taken and there is no second
identity to write the same line under. `apply` refuses such a file outright
instead of writing nothing and reporting success.

That is why correcting a cutover is a WINDOW, not a withdraw-and-redo.
`--reverse-through` closes the scope at the top, so a cutover set too early is
fixed by undoing exactly the vouchers between the wrong date and the right one
and leaving the rest of the import alone:

    # imported with --cutover 2026-10-05; the count really ended 2026-11-15
    powershell -File scripts/run_cli.ps1 -Cli import_wholesale_file ^
        -Args "--file <same file> --cutover 2026-10-05 ^
               --reverse --reverse-through 2026-11-15 --dry-run"

Without `--reverse-through` the withdrawal runs to the end of the file, which
is what you want only when the whole import was wrong.

    取込済みで対象外    lines a past import applied that THIS cutover excludes.
                      Non-zero means the cutover is not the one the import
                      used, and the withdrawal would be partial — refused
    取り消す対象        lines that will be put back
    取込済みでない      in scope, but the import never moved them (unmapped at
                      the time, or the gift bag). Nothing to undo
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
from app.services.wholesale_cutover import (
    applied_stocktakes,
    check_override,
    counted_through,
    resolve_cutover,
)
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
        f"取消済み            {shape['withdrawn_line_count']}明細 … 再取込できません",
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


def _describe_reverse(reading: BmsReading, plan: WholesalePlan) -> list[str]:
    """The same plan read backwards.

    Every count that matters to an import means something else to a withdrawal,
    so the labels cannot be shared: 既に取込済み is what an import SKIPS and
    exactly what a reversal TARGETS. Showing the import's wording for a
    withdrawal would have the operator approve the opposite of what they read.

    マイナスになる and 棚卸日と同日 are left out for the same reason — both
    describe what applying the file would do, and neither is true of undoing it.
    """
    shape = plan.as_dict()
    # Counted the way `reverse` itself counts, so the preview and the result
    # cannot disagree. Note neither is gated on `mapped`: the reversal reads the
    # events, which carry their own master, so whether the code resolves today
    # does not decide what can be undone.
    targets = plan.already_applied_lines
    never = [
        line for line in plan.all_lines if not line.already_applied and not line.already_withdrawn
    ]
    lines = [
        f"種別              {RULES[plan.kind].label} の取り消し",
        f"カットオーバー      {shape['cutover_on']}  (取り込み時と同じ日付であること)",
        f"取り消す範囲の上限  {shape['through_on'] or '指定なし (ファイルの最後まで)'}",
        f"上限より後        {shape['beyond_voucher_count']}伝票 "
        f"{shape['beyond_line_count']}明細 … 取り消しません",
        f"ファイル全体        {reading.total_rows}行 / 有効 {reading.valid_rows}行"
        f" / 数量0 {reading.zero_quantity_rows}行",
        f"取込済みで対象外    {shape['stranded_applied_line_count']}明細 / "
        f"{shape['stranded_voucher_count']}伝票",
        f"明細が足りない伝票  {shape['truncated_voucher_count']}伝票",
        f"取り消す対象        {len(targets)}明細 "
        f"(数量 {-sum(line.quantity_delta for line in targets):+d})",
        f"既に取消済み        {shape['withdrawn_line_count']}明細",
        f"取込済みでない      {len(never)}明細 … 取り消すものがありません",
        f"未マッピング        {shape['unmapped_line_count']}明細",
        f"在庫管理対象外      {shape['unmanaged_line_count']}明細",
    ]
    if plan.stranded_applied_lines:
        lines.append("取込済みで対象外の明細 (先頭20件):")
        lines.extend(
            f"    {line.sku_code or line.line.product_code}"
            f"  ({line.line.voucher}-{line.line.voucher_line})"
            for line in plan.stranded_applied_lines[:20]
        )
    for label, vouchers in (
        ("取込済みで対象外", plan.stranded_vouchers),
        ("明細が足りない", plan.truncated_vouchers),
    ):
        if vouchers:
            lines.append(f"{label}の伝票 (先頭20件):")
            lines.extend(
                f"    {voucher.number}  ({voucher.occurred_on.isoformat()})"
                for voucher in vouchers[:20]
            )
    return lines


async def _withdraw(
    session: AsyncSession,
    plan: WholesalePlan,
    *,
    operator: str,
    dry_run: bool,
    resolved: date,
) -> int:
    """The `--reverse` tail: refuse a partial withdrawal, otherwise undo it.

    Every refusal is checked here as well as inside `reverse`, which raises for
    them. An operator who typed the wrong cutover needs the sentence that says
    which flag to change, not a traceback.
    """
    for label, count, fix in (
        (
            "取込済みで対象外",
            len(plan.stranded_applied_lines) + len(plan.stranded_vouchers),
            "取り込み時と同じカットオーバーを --cutover で指定してください。",
        ),
        (
            "明細が足りない伝票",
            len(plan.truncated_vouchers),
            "取り込みに使ったファイルと同じものを指定してください。"
            "一部の明細が欠けたファイルでは、欠けた分の在庫が戻りません。",
        ),
    ):
        if count:
            log.error(
                "wholesale_file.reverse_refused",
                reason=label,
                lines=count,
                cutover_on=resolved.isoformat(),
            )
            print(
                f"  [中止] {label}が{count}件あります。"
                f"このまま取り消すと一部だけが戻ることになります。{fix}"
            )
            return 1

    if dry_run:
        print("--dry-run のため何も書いていません。")
        return 0

    result = await WholesaleImportService(session).reverse(plan, operator=operator)
    await session.commit()
    print(
        f"取り消しました: 在庫 {result.reversed_lines}明細 "
        f"(イベント {result.events_written}件, 数量 {result.total_delta:+d}) / "
        f"売上 {result.orders_cancelled}伝票を対象外に / "
        f"取込済みでない {result.never_applied_lines}明細 / "
        f"既に取消済み {result.already_reversed_lines}明細 / "
        f"照合できず {result.unreconciled_lines}明細"
    )
    log.info(
        "wholesale_file.reversed",
        operator=operator,
        cutover_on=resolved.isoformat(),
        reversed_lines=result.reversed_lines,
        already_reversed_lines=result.already_reversed_lines,
        never_applied_lines=result.never_applied_lines,
        unreconciled_lines=result.unreconciled_lines,
        events_written=result.events_written,
        total_delta=result.total_delta,
        orders_cancelled=result.orders_cancelled,
    )
    return 0


async def run(
    *,
    data: bytes,
    dry_run: bool,
    operator: str,
    reverse: bool = False,
    through_on: date | None = None,
    cutover_on: date | None = None,
    basis_run_id: int | None = None,
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
            candidates = await applied_stocktakes(session)
        else:
            resolution = await resolve_cutover(session, basis_run_id=basis_run_id)
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
            candidates = resolution.candidates
            if resolution.basis is not None:
                print(
                    f"カットオーバー: {resolved.isoformat()} "
                    f"（棚卸 {resolution.basis.counted_on.isoformat()} の翌日）"
                )
            else:
                print(f"カットオーバー: {resolved.isoformat()}")

        through = counted_through(candidates, cutover_on=resolved)
        plan = await WholesaleImportService(session).plan(
            reading, cutover_on=resolved, counted_through=through, through_on=through_on
        )
        for text in (_describe_reverse if reverse else _describe)(reading, plan):
            print(text)
        if through is not None and plan.recounted_vouchers:
            # Not a refusal: which SKUs a finalised stocktake covered cannot be
            # read back (docs/29 §10-22), so filtering here would drop real
            # movements. Saying it is what lets the operator import before the
            # next counting weekend instead of after it.
            #
            # It applies to BOTH directions, in opposite signs. A count sets
            # stock absolutely, so a voucher dated at or before one that has
            # since been applied is already inside that figure: importing it
            # subtracts the movement twice, and withdrawing it ADDS BACK
            # something the count never had out. Suppressing this for --reverse
            # was wrong — undoing an import the count absorbed is not the remedy
            # for the double subtraction, it is the same error upwards.
            lines = sum(len(v.lines) for v in plan.recounted_vouchers)
            doubled = "二重に戻されます" if reverse else "二重に引かれます"
            print(
                f"  [警告] {through.isoformat()} までの明細 "
                f"{lines}件は、その日までに確定した棚卸に含まれている可能性があります。"
                f"該当するSKUがあれば{doubled}（docs/29 §10-28）。"
            )

        if plan.blocked:
            # Before --dry-run is consulted: a blocked plan is not a thing to
            # preview and approve, it is a file that cannot be imported either
            # way. `apply` refuses it too, but by then it is a traceback rather
            # than a sentence.
            for conflict in plan.voucher_conflicts:
                print(f"  [中止] {conflict.message}")
            return 1

        if reverse:
            return await _withdraw(
                session, plan, operator=operator, dry_run=dry_run, resolved=resolved
            )

        if plan.withdrawn_lines:
            # Also before --dry-run, and also a sentence rather than the
            # traceback `apply` would raise. Nothing here can be imported, so
            # offering a preview to approve would be offering a decision that
            # does not exist.
            log.error(
                "wholesale_file.withdrawn_lines",
                lines=len(plan.withdrawn_lines),
                cutover_on=resolved.isoformat(),
            )
            print(
                f"  [中止] このファイルには取消済みの明細が{len(plan.withdrawn_lines)}件"
                "あります。一度取り消した明細は取り込み直せません。"
                "カットオーバーを直す場合は、取り消す範囲を --reverse-through で"
                "区切って取り消してください（取り消し後に取り込み直すのではありません）。"
            )
            return 1

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
            "This is the START date, i.e. the day AFTER the count; refused before "
            "2026-10-01 either way (docs/29 §7-2). Prefer --basis-run"
        ),
    )
    p.add_argument(
        "--basis-run",
        type=int,
        default=None,
        help=(
            "reconcile_runs.id of the stocktake that counted the wholesale "
            "masters. Needed once the catalogue has been counted on more than "
            "one date, since only the operator knows which instalment that was "
            "(docs/29 §10-22, §10-27)"
        ),
    )
    p.add_argument(
        "--reverse",
        action="store_true",
        help=(
            "withdraw a past import of this same file instead of importing it. "
            "Give it the SAME --cutover the import used; a different one names a "
            "different voucher set and is refused rather than half applied"
        ),
    )
    p.add_argument(
        "--reverse-through",
        default=None,
        help=(
            "YYYY-MM-DD, inclusive: with --reverse, stop the withdrawal at this "
            "voucher date instead of running to the end of the file. This is how "
            "a cutover set too early is corrected — undo the window that was "
            "wrong and leave the rest of the import standing, because a withdrawn "
            "line cannot be imported again"
        ),
    )
    args = p.parse_args()
    configure_logging("INFO")
    if args.reverse_through and not args.reverse:
        p.error("--reverse-through only means something with --reverse")
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
                reverse=args.reverse,
                through_on=(parse_cutover(args.reverse_through) if args.reverse_through else None),
                cutover_on=parse_cutover(args.cutover) if args.cutover else None,
                basis_run_id=args.basis_run,
            )
        )
    )


if __name__ == "__main__":
    main()
