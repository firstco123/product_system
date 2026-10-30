"""Push master stock to Rakuten SKU管理番号 level. Phase 2, P2-039/041.

Until this existed there was NO path that wrote stock to Rakuten.
`push_bundle_availability` builds a Shopify adapter and selects bundles only;
`_adapters.py` had no Rakuten builder; the sole code able to reach Rakuten's
inventory API was the retry button on the sync-errors screen. So P2-041, the
production verification, had nothing to run.

WHY IT TAKES AN EXPLICIT SKU LIST

`--sku` is the normal mode and `--all` has to be asked for. This is the first
system that has ever written stock to this shop's live pages, and a Rakuten page
shows its SKU stock to customers immediately. A mistaken quantity on one page is
a correction; the same mistake across 419 pages is an incident. `--limit` exists
for the same reason.

WHAT IT REFUSES, AND WHY THAT IS THE POINT

Targets go through `push_safety.screen_targets`, so two things never reach the
API: a page-level mapping, which has no address on a 項目選択肢別在庫 shop, and a
coarse master — one of ours standing in for several of Rakuten's SKUs on one
page, where no single quantity is correct to send. 57 of 141 mapped masters are
in that second shape, measured 2026-10-30. Those need splitting in the
catalogue; this CLI names them and writes nothing for them.

`--dry-run` resolves exactly the same plan through the same function and prints
it without writing, so the preview cannot disagree with the push.

    powershell -File scripts/run_cli.ps1 -Cli push_rakuten_stock -WithRakuten `
        -Args "--sku N55gold --dry-run"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.base import ChannelAdapter
from app.cli._adapters import build_rakuten_adapter
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import MasterSku, SyncAttemptStatusEnum
from app.notifications.slack import get_slack_notifier
from app.services import BundlePushService
from app.services.bundle_push import PushPlan

log = get_logger(__name__)

CHANNEL = "rakuten"

SessionFactory = async_sessionmaker[AsyncSession]

EXIT_OK = 0
EXIT_PUSH_FAILED = 1
EXIT_NOTHING_SELECTED = 2


async def _ids_for(session: AsyncSession, sku_codes: list[str]) -> tuple[list[int], list[str]]:
    """Master ids for these sku_codes, plus the codes that matched nothing.

    An unknown code is reported rather than ignored: `--sku N55gold` silently
    pushing nothing because the code is `N55Gold` would read as "the push ran
    and Rakuten did not change", which is the hardest kind of result to chase.
    """
    result = await session.execute(
        select(MasterSku.id, MasterSku.sku_code).where(MasterSku.sku_code.in_(sku_codes))
    )
    found = {code: mid for mid, code in result.all()}
    missing = [c for c in sku_codes if c not in found]
    return [found[c] for c in sku_codes if c in found], missing


def _print_plan(plans: list[PushPlan], *, labels: dict[int, str]) -> tuple[int, int]:
    """Print what will be written and what will not. Returns (writes, blocked)."""
    writes = blocked = 0
    for plan in plans:
        label = labels.get(plan.master_sku_id, str(plan.master_sku_id))
        for target in plan.screened.targets:
            print(f"  書き込み  {label:<16} {target.label:<28} 在庫 {plan.available}")
            writes += 1
        for refusal in plan.screened.blocked_pages:
            print(f"  中止      {label:<16} {refusal.channel_sku}")
            print(f"            {refusal.message}")
            blocked += 1
        for refusal in plan.screened.skipped:
            print(f"  対象外    {label:<16} {refusal.channel_sku} はページ単位の行です")
    return writes, blocked


async def run(
    *,
    sku_codes: list[str],
    push_all: bool,
    limit: int | None,
    dry_run: bool,
    triggered_by: str,
    session_factory: SessionFactory | None = None,
    adapter: ChannelAdapter | None = None,
) -> int:
    notifier = get_slack_notifier()
    factory = session_factory or async_session_factory
    # One transaction for the whole run, opened up front like
    # `push_bundle_availability`. Reading first and calling `session.begin()`
    # afterwards raises: the reads have already begun one implicitly.
    async with factory() as session, session.begin():
        svc = BundlePushService(session, notifier)

        if push_all:
            master_ids = await svc.masters_mapped_on(CHANNEL)
        else:
            master_ids, missing = await _ids_for(session, sku_codes)
            for code in missing:
                print(f"  [警告] 商品コード {code} は見つかりませんでした。")

        if limit is not None:
            master_ids = master_ids[:limit]
        if not master_ids:
            print("\n  対象がありません。--sku の指定をご確認ください。")
            return EXIT_NOTHING_SELECTED

        rows = await session.execute(
            select(MasterSku.id, MasterSku.sku_code).where(MasterSku.id.in_(master_ids))
        )
        labels: dict[int, str] = dict(rows.all())  # type: ignore[arg-type]

        plans = await svc.resolve_push_plan(channel=CHANNEL, master_ids=master_ids)
        print(f"\n--- 楽天 在庫反映{' (dry-run)' if dry_run else ''} ---")
        print(f"  対象マスタ {len(master_ids)}件 / 対応づけのあるマスタ {len(plans)}件\n")
        writes, blocked = _print_plan(plans, labels=labels)
        print(f"\n  書き込み {writes}件 / 中止 {blocked}ページ")
        if blocked:
            print("  中止したページは、マスタをSKU単位に分割するまで在庫を反映できません。")

        if dry_run:
            print("\n  dry-run のため書き込んでいません。")
            log.info(
                "push_rakuten_stock.dry_run",
                masters=len(master_ids),
                writes=writes,
                blocked_pages=blocked,
            )
            return EXIT_OK
        if not writes:
            log.info("push_rakuten_stock.nothing_writable", blocked_pages=blocked)
            return EXIT_OK

        # `push_bundles` resolves the plan again rather than taking the one above.
        # Inside this transaction it sees the same rows, and keeping it
        # self-contained means the scheduled Shopify job and this CLI cannot
        # diverge in how a push is assembled.
        if adapter is None:
            async with build_rakuten_adapter(purpose="push stock to Rakuten") as built:
                attempts = await svc.push_bundles(
                    built, master_ids, triggered_by=triggered_by, dry_run=False
                )
        else:
            # Injected by the tests, which own its lifecycle.
            attempts = await svc.push_bundles(
                adapter, master_ids, triggered_by=triggered_by, dry_run=False
            )
        failed = [a for a in attempts if a.status == SyncAttemptStatusEnum.FAILED.value]
        print(f"\n  送信 {len(attempts)}件 / 失敗 {len(failed)}件")
        for attempt in failed:
            print(f"  [失敗] {attempt.payload.get('channel_sku')} {attempt.error_message}")
        log.info(
            "push_rakuten_stock.done",
            masters=len(master_ids),
            pushed=len(attempts),
            failed=len(failed),
            blocked_pages=blocked,
        )
        return EXIT_PUSH_FAILED if failed else EXIT_OK


def main() -> None:
    p = argparse.ArgumentParser(description="Push master stock to Rakuten SKU管理番号 level")
    p.add_argument(
        "--sku",
        action="append",
        default=[],
        dest="sku_codes",
        help="master sku_code to push; repeatable",
    )
    p.add_argument(
        "--all",
        action="store_true",
        dest="push_all",
        help="every live stock-managed master mapped on Rakuten. Prefer --sku.",
    )
    p.add_argument("--limit", type=int, default=None, help="cap the number of masters")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--triggered-by", default="cli", dest="triggered_by")
    args = p.parse_args()
    if not args.sku_codes and not args.push_all:
        p.error("give --sku at least once, or --all")
    configure_logging("INFO")
    sys.exit(
        asyncio.run(
            run(
                sku_codes=args.sku_codes,
                push_all=args.push_all,
                limit=args.limit,
                dry_run=args.dry_run,
                triggered_by=args.triggered_by,
            )
        )
    )


if __name__ == "__main__":
    main()
