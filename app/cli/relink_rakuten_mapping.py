"""Point an existing Rakuten mapping at a different master, one SKU at a time.

The case that prompted this: page 10099 (B46) sells one length only, but its two
SKU管理番号 were mapped to the BRACELET masters instead of the ANKLET ones. The
page carries no length axis, so nothing in the Rakuten data distinguishes the two
— the client spotted it from the listing itself. Mapping a Rakuten SKU to the
wrong one of a shared-stock pair is not a cosmetic mislabel:

* the bracelet master is `is_bundle`, so it holds no stock of its own and the
  pushed quantity is `compute_bundle_available()` over its components — **0 when
  it has none** (`app/services/inventory.py`), which writes 在庫切れ to a live page;
* sales land on a product the shop did not sell, under the very per-colour/size
  breakdown P2-011/P2-042 exist to report.

WHAT THIS DOES
Changes `master_sku_id` on the rows you name, and nothing else. It does not
create mappings, delete them, flip `is_active`, or touch stock. Re-running it is
a no-op once the rows already point at the target.

WHY A REPOINT CANNOT COLLIDE
`uq_channel_sku_mapping` is on (channel, channel_sku, marketplace_id,
channel_variant_key) and does NOT include `master_sku_id`. So for any one of
those tuples there is exactly one row in the table, and moving which master it
names cannot conflict with anything. That is why this tool updates in place
rather than deleting and re-inserting — the latter would briefly leave the page
unmapped and would lose `created_at`.

WHAT IT REFUSES
Every problem is reported in one run, and a single blocker means nothing is
written — fixing them one per run makes each repeat attempt look like a new
failure. It refuses an `is_bundle` target outright: that is exactly the defect
above, so the tool that repairs it must not be able to recreate it.

Usage (via the Cloud SQL proxy), report first, then the same line with --apply:
    powershell -File scripts/run_cli.ps1 -Cli relink_rakuten_mapping ^
        -Args "--move 10099/10099-gold=B46goldanklet --move 10099/10099-silver=B46silveranklet"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MasterSku

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

CHANNEL = "rakuten"

#: Refusal text for the defect this tool exists to repair. Named so the test
#: cannot drift from the sentence the operator actually reads.
BUNDLE_TARGET = "マスタ {sku} は共有在庫の参照側のため、在庫の書き込み先にできません"

#: Refusal text for the 57-master shape — one master holding several SKU管理番号 of
#: one page, which `push_safety.screen_targets` refuses to write at all.
COARSE_TARGET = (
    "マスタ {sku} が商品管理番号 {page} の SKU管理番号 {keys} を"
    "同時に持つことになり、在庫を書き込めなくなります"
)


@dataclass(frozen=True, slots=True)
class Move:
    """One requested repoint, as parsed from the command line."""

    channel_sku: str
    #: Rakuten's SKU管理番号, or None for the page-level row (商品管理番号 alone).
    variant_key: str | None
    target_sku: str

    @property
    def label(self) -> str:
        return f"{self.channel_sku}/{self.variant_key}" if self.variant_key else self.channel_sku


@dataclass(frozen=True, slots=True)
class Repoint:
    """A move that can be carried out."""

    move: Move
    mapping_id: int
    target_id: int
    current_sku: str
    #: Already pointing at the target. Kept in the plan so a re-run reports it as
    #: done rather than omitting it, which reads as "not found".
    already_done: bool


@dataclass(frozen=True, slots=True)
class Blocker:
    label: str
    reason: str


@dataclass(frozen=True, slots=True)
class Plan:
    repoints: list[Repoint]
    blockers: list[Blocker]

    def ready(self) -> list[Repoint]:
        return [r for r in self.repoints if not r.already_done]


def parse_move(spec: str) -> Move:
    """`channel_sku[/variant_key]=master_sku_code` -> Move.

    The slash separates 商品管理番号 from SKU管理番号 because a Rakuten mapping is
    only unique as that pair: 132 of 886 SKU管理番号 are reused across pages, so a
    bare SKU管理番号 names more than one product.
    """
    sku_part, _, target = spec.partition("=")
    if not target or not sku_part:
        raise ValueError(f"--move の書式が正しくありません: {spec!r}")
    channel_sku, slash, variant_key = sku_part.partition("/")
    if not channel_sku or (slash and not variant_key):
        raise ValueError(f"--move の書式が正しくありません: {spec!r}")
    return Move(channel_sku, variant_key if slash else None, target)


async def plan(session: AsyncSession, moves: list[Move]) -> Plan:
    """Decide what can be repointed. Reads only."""
    targets = await session.execute(
        select(MasterSku.id, MasterSku.sku_code, MasterSku.is_bundle, MasterSku.archived_at).where(
            MasterSku.sku_code.in_({m.target_sku for m in moves})
        )
    )
    found = {code: (mid, is_bundle, archived) for mid, code, is_bundle, archived in targets.all()}

    repoints: list[Repoint] = []
    blockers: list[Blocker] = []
    #: mapping id -> the move that already claimed it. Two moves resolving to one
    #: row would both be planned, both be reported as done, and `apply` would let
    #: the last one win — so the operator's record of a production repair would be
    #: wrong in two directions at once. Registered for every resolved row,
    #: including `already_done` ones: otherwise `X=A X=B` against a row already on
    #: A slips through as "変更済み" plus a write to B.
    claimed: dict[int, Move] = {}
    for move in moves:
        target = found.get(move.target_sku)
        if target is None:
            blockers.append(Blocker(move.label, f"マスタ {move.target_sku} が存在しません"))
            continue
        target_id, target_is_bundle, archived_at = target
        if archived_at is not None:
            blockers.append(Blocker(move.label, f"マスタ {move.target_sku} は使用停止です"))
            continue
        if target_is_bundle:
            # The B46 defect itself: a derived master has no stock of its own, so
            # the push sends the clamped bundle figure (0 with no components) and
            # the sale is recorded against a product that was not sold.
            blockers.append(Blocker(move.label, BUNDLE_TARGET.format(sku=move.target_sku)))
            continue

        stmt = select(ChannelSkuMapping, MasterSku.sku_code).join(
            MasterSku, MasterSku.id == ChannelSkuMapping.master_sku_id
        )
        stmt = stmt.where(
            ChannelSkuMapping.channel == CHANNEL,
            ChannelSkuMapping.channel_sku == move.channel_sku,
            ChannelSkuMapping.channel_variant_key.is_(None)
            if move.variant_key is None
            else ChannelSkuMapping.channel_variant_key == move.variant_key,
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            blockers.append(Blocker(move.label, "対応づけが見つかりません"))
            continue
        if len(rows) > 1:
            # uq_channel_sku_mapping makes this unreachable for one marketplace_id;
            # if it ever fires, the constraint is gone and guessing would be worse.
            blockers.append(Blocker(move.label, f"対応づけが {len(rows)} 件あり特定できません"))
            continue

        mapping, current_sku = rows[0]
        if not mapping.is_active:
            # Repointing an inactive row would quietly resurrect it the next time
            # something reactivates mappings in bulk.
            blockers.append(Blocker(move.label, "対応づけが無効化されています"))
            continue

        prior = claimed.get(mapping.id)
        if prior is not None:
            blockers.append(Blocker(move.label, f"同じ対応づけを {prior.label} でも指定しています"))
            continue
        claimed[mapping.id] = move

        repoints.append(
            Repoint(
                move=move,
                mapping_id=mapping.id,
                target_id=target_id,
                current_sku=current_sku,
                already_done=mapping.master_sku_id == target_id,
            )
        )

    blockers.extend(await _coarse_target_blockers(session, repoints))
    return Plan(repoints, blockers)


async def _coarse_target_blockers(session: AsyncSession, repoints: list[Repoint]) -> list[Blocker]:
    """Refuse any move that would leave one master holding two SKU管理番号 of one page.

    That shape is the 57-master problem this whole exercise exists to undo:
    `push_safety.screen_targets` refuses such a page outright (COARSE_MASTER),
    because no single quantity is correct to send to several SKUs — so the page
    would silently stop receiving stock altogether. Sales have no equivalent
    guard, so both colours would also accrue to one master. The likeliest way to
    cause it here is copying a --move line and changing only the variant key.

    Checked against the state the run would LEAVE: the targets' existing active
    rows, minus the rows this run moves away, plus the rows it moves in.
    """
    if not repoints:
        return []

    moving = {r.mapping_id for r in repoints}
    rows = await session.execute(
        select(
            ChannelSkuMapping.id,
            ChannelSkuMapping.master_sku_id,
            ChannelSkuMapping.channel_sku,
            ChannelSkuMapping.channel_variant_key,
        ).where(
            ChannelSkuMapping.channel == CHANNEL,
            ChannelSkuMapping.is_active,
            ChannelSkuMapping.master_sku_id.in_({r.target_id for r in repoints}),
        )
    )

    # (master, page) -> the SKU管理番号 that master would address on that page.
    # A NULL variant key addresses the page as a whole and never collides.
    after: dict[tuple[int, str], set[str]] = {}
    for mapping_id, master_id, channel_sku, variant_key in rows.all():
        if mapping_id in moving or variant_key is None:
            continue
        after.setdefault((master_id, channel_sku), set()).add(variant_key)
    for repoint in repoints:
        if repoint.move.variant_key is not None:
            key = (repoint.target_id, repoint.move.channel_sku)
            after.setdefault(key, set()).add(repoint.move.variant_key)

    blockers: list[Blocker] = []
    for repoint in repoints:
        if repoint.move.variant_key is None:
            continue
        keys = after.get((repoint.target_id, repoint.move.channel_sku), set())
        if len(keys) > 1:
            blockers.append(
                Blocker(
                    repoint.move.label,
                    COARSE_TARGET.format(
                        sku=repoint.move.target_sku,
                        page=repoint.move.channel_sku,
                        keys="・".join(sorted(keys)),
                    ),
                )
            )
    return blockers


async def apply(session: AsyncSession, result: Plan) -> None:
    """Repoint the mappings. Updates `master_sku_id` only."""
    for repoint in result.ready():
        mapping = await session.get(ChannelSkuMapping, repoint.mapping_id)
        if mapping is not None:
            mapping.master_sku_id = repoint.target_id


async def run(
    *,
    specs: list[str],
    apply_changes: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    try:
        moves = [parse_move(s) for s in specs]
    except ValueError as exc:
        print(f"\n  {exc}")
        return 2

    factory = session_factory or async_session_factory
    async with factory() as session:
        result = await plan(session, moves)

        print("\n--- 楽天マッピングの付け替え ---")
        for blocker in result.blockers:
            print(f"  {blocker.label:28} 実行できません: {blocker.reason}")

        if result.blockers:
            log.error("relink_rakuten_mapping.blocked", blockers=len(result.blockers))
            print("\n  問題のある指定があるため、何も変更していません")
            return 1

        ready = result.ready()
        if apply_changes and ready:
            # NOT `async with session.begin()`: the planning reads above have
            # already autobegun a transaction on this session, and committing
            # that same one is what makes plan-and-apply atomic.
            await apply(session, result)
            await session.commit()

        # Printed AFTER the write so the tense is the truth.
        for repoint in result.repoints:
            if repoint.already_done:
                state = "変更済み"
            else:
                state = "付け替えました" if apply_changes else "付け替えます"
            arrow = f"{repoint.current_sku} -> {repoint.move.target_sku}"
            print(f"  {repoint.move.label:28} {arrow:40} {state}")

        if not apply_changes:
            log.info("relink_rakuten_mapping.report", ready=len(ready))
            print(f"\n  {len(ready)}件を付け替えられます  ※ 変更していません (--apply で実行)")
            return 0

    log.info("relink_rakuten_mapping.applied", repointed=len(ready))
    print(f"\n  {len(ready)}件を付け替えました")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Repoint Rakuten mappings at other masters")
    p.add_argument(
        "--move",
        action="append",
        default=[],
        required=True,
        metavar="SKU[/VARIANT]=MASTER",
        help="商品管理番号[/SKU管理番号]=付け替え先のマスタSKUコード",
    )
    p.add_argument("--apply", action="store_true", help="write (default: report only)")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(specs=args.move, apply_changes=args.apply)))


if __name__ == "__main__":
    main()
