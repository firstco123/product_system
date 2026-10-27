"""Where the wholesale cutover date comes from (docs/29 §7-1, §7-2).

`WholesaleImportService.plan` takes `cutover_on` as a plain parameter and
filters on it, which is the right shape for a service — but it means the date is
whatever the caller typed. That is the one input that cannot be allowed to be a
guess: the history the client has already sent us covers 受注 8/16–9/14 (856
units) and 返品 6/30–9/9 (3,901 units), and importing it would leave stock
**3,045 units too high**. A cutover a month early does exactly that, silently,
and the resulting numbers look like ordinary stock.

So the operational entry points resolve the date from what the stocktake
actually recorded instead of asking for it:

    在庫計算の開始日（カットオーバー） = 10月の棚卸実施日     docs/29 §7-1

`reconcile_runs.counted_on` on an APPLIED run of `run_type='stocktake'` is that
date, recorded by the count itself. Deriving it means the import and the count
cannot disagree about when our stock figures became true.

WHEN THIS REFUSES, AND WHY THAT IS THE POINT

* **No applied stocktake.** Which is the state today. There is no date at which
  our stock is known to be right, so there is no wholesale movement that can be
  added to it. This is docs/29 §7-2 as a check rather than a paragraph.
* **A date before October.** The agreement names 10月の棚卸実施日, so anything
  earlier is a mistyped override or a stocktake of something else.
* **Counts on several dates.** This is the case docs/29 does not cover, and it
  cannot be resolved here — see `MULTI_DATE_NOTE`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models import ReconcileRun, ReconcileRunStatusEnum, ReconcileRunTypeEnum
from app.services.reconcile import of_run_type

log = get_logger(__name__)

#: Nothing before this can be the cutover. The client agreed on 2026-09-17 that
#: the start date is 10月の棚卸実施日 (docs/29 §1), so an earlier date is a slip,
#: and the cost of honouring a slip is the 3,045 units in §7-2. A floor rather
#: than an exact date because the count day was not fixed when this was written.
CUTOVER_FLOOR = date(2026, 10, 1)

MULTI_DATE_NOTE = (
    "棚卸が複数日に分かれています。1つのカットオーバー日では正しくなりません: "
    "先に数えたSKUは、その日以降の卸売の動きを足す必要があり、"
    "後に数えたSKUは、その分がすでに棚卸の数に入っています。"
    "どちらに寄せるかは運用の判断なので、--cutover で明示してください。"
)


@dataclass(frozen=True, slots=True)
class CutoverResolution:
    """The resolved cutover, or the reason there is not one."""

    cutover_on: date | None
    #: Every distinct `counted_on` found on an applied stocktake run, in order.
    counted_on: tuple[date, ...]
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.cutover_on is not None


async def resolve_cutover(session: AsyncSession) -> CutoverResolution:
    """Derive the cutover from the applied stocktake runs."""
    rows = await session.execute(
        select(ReconcileRun.counted_on)
        .where(
            # Through the shared predicate, not a hand-rolled comparison:
            # `reconcile_runs` has held three kinds of run since migration 0012,
            # and `tests/test_reconcile_run_type.py` fails the build over any
            # query that does not say which it means. A CROSS MALL comparison is
            # not a physical count and must not date the cutover.
            of_run_type(ReconcileRunTypeEnum.STOCKTAKE),
            ReconcileRun.status == str(ReconcileRunStatusEnum.APPLIED),
            ReconcileRun.counted_on.is_not(None),
        )
        .distinct()
        .order_by(ReconcileRun.counted_on)
    )
    counted = tuple(day for day in rows.scalars().all() if day is not None)

    if not counted:
        return CutoverResolution(
            cutover_on=None,
            counted_on=(),
            reason=(
                "確定済みの棚卸がまだありません。在庫が正しいと言える日が無いため、"
                "卸売の動きを足す起点がありません（docs/29 §7-2）。"
                "棚卸を確定してから取り込んでください。"
            ),
        )

    if len(counted) > 1:
        return CutoverResolution(cutover_on=None, counted_on=counted, reason=MULTI_DATE_NOTE)

    only = counted[0]
    if only < CUTOVER_FLOOR:
        return CutoverResolution(
            cutover_on=None,
            counted_on=counted,
            reason=(
                f"棚卸日 {only.isoformat()} は合意済みの開始時期（"
                f"{CUTOVER_FLOOR.isoformat()} 以降の10月の棚卸）より前です。"
                "別の棚卸を参照している可能性があります。"
            ),
        )
    return CutoverResolution(cutover_on=only, counted_on=counted)


def check_override(cutover_on: date) -> str | None:
    """Why a hand-given cutover must not be used, or None when it may be.

    An override is legitimate — the multi-date case needs one — but it is the
    input that can quietly inflate stock, so it gets the same floor the derived
    date does.
    """
    if cutover_on < CUTOVER_FLOOR:
        return (
            f"指定されたカットオーバー {cutover_on.isoformat()} は "
            f"{CUTOVER_FLOOR.isoformat()} より前です。"
            "提供済みの履歴（受注 8/16〜9/14・返品 6/30〜9/9）を取り込むと"
            "在庫が3,045点過大になります（docs/29 §7-2）。"
        )
    return None


__all__ = [
    "CUTOVER_FLOOR",
    "MULTI_DATE_NOTE",
    "CutoverResolution",
    "check_override",
    "resolve_cutover",
]
