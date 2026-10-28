"""Where the wholesale cutover date comes from (docs/29 §7-1, §7-2, §10-27).

`WholesaleImportService.plan` takes `cutover_on` as a plain parameter and
filters on it, which is the right shape for a service — but it means the date is
whatever the caller typed. That is the one input that cannot be allowed to be a
guess: the history the client has already sent us covers 受注 8/16–9/14 (856
units) and 返品 6/30–9/9 (3,901 units), and importing it would leave stock
**3,045 units too high**. A cutover a month early does exactly that, silently,
and the resulting numbers look like ordinary stock.

So the operational entry points resolve the date from what a stocktake actually
recorded instead of asking for it.

THE CUTOVER IS THE DAY AFTER THE COUNT

Confirmed by the client on 2026-10-28, answering the question docs/29 §10-10 had
left open. The 82 wholesale-relevant masters are counted over about four days,
and the figures submitted are aligned by hand to **the final day's close of
business**: each item's shipments after its own count are subtracted and its
returns added before the sheet is sent. The count therefore already contains
everything that moved up to and including that day, so the first day whose
movements we have to add is the day after it.

`plan` treats `cutover_on` as a start date and keeps same-day vouchers, so this
is `counted_on + 1 day` and nothing else changes. Returning `counted_on` itself
would subtract the basis day's shipments a second time.

WHICH STOCKTAKE DATES IT

Not "the stocktake" — there are five. The catalogue is counted category by
category over the weekends of 10/30–11/01, 11/06–11/08, 11/13–11/15, 11/20–11/22
and 11/27–11/29, while the wholesale masters are counted together at the start
(docs/29 §10-27). Wholesale lines only ever move those masters, so the run that
counted THEM dates the cutover and the four later weekends do not.

Which run that was cannot be read from the data: a finalised stocktake keeps
only the SKUs that differed, so "which SKU was counted when" has no answer
afterwards (docs/29 §10-22). It can be read from the operator, who was there —
hence `basis_run_id`, and a resolution that carries the candidates so a screen
can offer them instead of asking for a date to be typed. Before this, a second
counting weekend made `resolve_cutover` refuse for good, leaving a free-text date
field as the only way in: the one input this module exists to keep out of
anyone's hands.

WHEN THIS REFUSES, AND WHY THAT IS THE POINT

* **No applied stocktake.** There is no date at which our stock is known to be
  right, so there is no wholesale movement that can be added to it. This is
  docs/29 §7-2 as a check rather than a paragraph.
* **A date before October.** The agreement names 10月の棚卸実施日, so anything
  earlier is a mistyped override or a stocktake of something else.
* **Several count dates and no basis chosen.** Resolvable, but only by someone
  who knows which instalment covered the wholesale masters.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

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


@dataclass(frozen=True, slots=True)
class StocktakeBasis:
    """One applied stocktake, as a candidate for dating the cutover."""

    run_id: int
    counted_on: date
    scope_note: str | None = None
    counted_sku_count: int | None = None

    @property
    def cutover_on(self) -> date:
        """The first day whose wholesale movements are not already counted."""
        return self.counted_on + timedelta(days=1)

    @property
    def label(self) -> str:
        """How a screen names this instalment to someone choosing between them.

        `scope_note` is the only record of what an instalment covered — a run
        holds just the SKUs that differed — so it is what makes "the wholesale
        one" recognisable in a list of five.
        """
        parts = [self.counted_on.isoformat()]
        if self.scope_note:
            parts.append(self.scope_note)
        elif self.counted_sku_count:
            parts.append(f"{self.counted_sku_count}SKUを計数")
        return " / ".join(parts)


@dataclass(frozen=True, slots=True)
class CutoverResolution:
    """The resolved cutover, or the reason there is not one."""

    cutover_on: date | None
    #: Every distinct `counted_on` found on an applied stocktake run, in order.
    counted_on: tuple[date, ...]
    reason: str = ""
    #: Every applied stocktake that could date the cutover, oldest first, so a
    #: screen can offer them rather than demanding a typed date.
    candidates: tuple[StocktakeBasis, ...] = ()
    #: The one the cutover was derived from, when there is one.
    basis: StocktakeBasis | None = None

    @property
    def ok(self) -> bool:
        return self.cutover_on is not None


def _several_dates_note(candidates: Sequence[StocktakeBasis]) -> str:
    listed = "、".join(basis.label for basis in candidates)
    return (
        "棚卸が複数日に分かれています。卸売の起点になるのは卸売商品を数えた回で、"
        "どの回がそれかはデータから判別できません（docs/29 §10-22）。"
        "画面で棚卸を選ぶか、--basis-run で棚卸を、"
        "または --cutover で日付を直接指定してください。"
        f"確定済みの棚卸: {listed}"
    )


async def applied_stocktakes(session: AsyncSession) -> tuple[StocktakeBasis, ...]:
    """Every applied stocktake carrying a count date, oldest first."""
    rows = await session.execute(
        select(
            ReconcileRun.id,
            ReconcileRun.counted_on,
            ReconcileRun.scope_note,
            ReconcileRun.counted_sku_count,
        )
        .where(
            # Through the shared predicate, not a hand-rolled comparison:
            # `reconcile_runs` has held four kinds of run since migration 0012,
            # and `tests/test_reconcile_run_type.py` fails the build over any
            # query that does not say which it means. A CROSS MALL comparison is
            # not a physical count and must not date the cutover.
            of_run_type(ReconcileRunTypeEnum.STOCKTAKE),
            ReconcileRun.status == str(ReconcileRunStatusEnum.APPLIED),
            ReconcileRun.counted_on.is_not(None),
        )
        .order_by(ReconcileRun.counted_on, ReconcileRun.id)
    )
    return tuple(
        StocktakeBasis(
            run_id=row.id,
            counted_on=row.counted_on,
            scope_note=row.scope_note,
            counted_sku_count=row.counted_sku_count,
        )
        for row in rows.all()
        if row.counted_on is not None
    )


def counted_through(candidates: Sequence[StocktakeBasis], *, cutover_on: date) -> date | None:
    """The last day a stocktake counted anything at or after the cutover.

    This is the §10-28 trap: a physical count sets stock absolutely, so a voucher
    dated on or before a stocktake that has since been applied is already inside
    that count, and adding it subtracts it twice. Returns the date to compare
    against, or None when the basis count is the most recent one — the ordinary
    state, and the one where there is nothing to warn about.
    """
    later = [basis.counted_on for basis in candidates if basis.counted_on >= cutover_on]
    return max(later) if later else None


async def resolve_cutover(
    session: AsyncSession, *, basis_run_id: int | None = None
) -> CutoverResolution:
    """Derive the cutover from the applied stocktake runs.

    `basis_run_id` names the instalment that counted the wholesale masters. With
    one count date on record it is unnecessary; with several it is the only thing
    that can decide, so leaving it out refuses rather than guesses.
    """
    candidates = await applied_stocktakes(session)
    counted = tuple(sorted({basis.counted_on for basis in candidates}))

    if not candidates:
        return CutoverResolution(
            cutover_on=None,
            counted_on=(),
            reason=(
                "確定済みの棚卸がまだありません。在庫が正しいと言える日が無いため、"
                "卸売の動きを足す起点がありません（docs/29 §7-2）。"
                "棚卸を確定してから取り込んでください。"
            ),
        )

    if basis_run_id is not None:
        chosen = next((basis for basis in candidates if basis.run_id == basis_run_id), None)
        if chosen is None:
            return CutoverResolution(
                cutover_on=None,
                counted_on=counted,
                candidates=candidates,
                reason=(
                    f"棚卸 #{basis_run_id} は確定済みの棚卸として見つかりません。"
                    "確定済みで、かつ棚卸日が記録されている回を選んでください。"
                ),
            )
    elif len(counted) == 1:
        # Several instalments on one day are normal — the client counts by
        # category — and every one of them gives the same cutover, so there is
        # nothing to disambiguate.
        chosen = candidates[0]
    else:
        return CutoverResolution(
            cutover_on=None,
            counted_on=counted,
            candidates=candidates,
            reason=_several_dates_note(candidates),
        )

    if chosen.counted_on < CUTOVER_FLOOR:
        return CutoverResolution(
            cutover_on=None,
            counted_on=counted,
            candidates=candidates,
            reason=(
                f"棚卸日 {chosen.counted_on.isoformat()} は合意済みの開始時期（"
                f"{CUTOVER_FLOOR.isoformat()} 以降の10月の棚卸）より前です。"
                "別の棚卸を参照している可能性があります。"
            ),
        )

    log.info(
        "wholesale.cutover_resolved",
        run_id=chosen.run_id,
        counted_on=chosen.counted_on.isoformat(),
        cutover_on=chosen.cutover_on.isoformat(),
        candidate_count=len(candidates),
    )
    return CutoverResolution(
        cutover_on=chosen.cutover_on,
        counted_on=counted,
        candidates=candidates,
        basis=chosen,
    )


def check_override(cutover_on: date) -> str | None:
    """Why a hand-given cutover must not be used, or None when it may be.

    An override is legitimate — someone who knows the count schedule better than
    the data does may need one — but it is the input that can quietly inflate
    stock, so it gets the same floor the derived date does. Note that this is a
    CUTOVER, not a count date: the day after the count is what belongs here.
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
    "CutoverResolution",
    "StocktakeBasis",
    "applied_stocktakes",
    "check_override",
    "counted_through",
    "resolve_cutover",
]
