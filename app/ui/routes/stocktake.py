"""実地棚卸 — count sheet upload, review and bulk approval (P2-028..030).

Its own screen rather than a tab on `/admin/reconcile`, for one reason: a
stocktake is planned work that arrives in instalments, while the daily external
check is a stream. The client counts one category per day, so a RING run and a
PIERCE run sit side by side in a history someone reads to answer "have we
counted the anklets yet?". Mixing them into the queue of daily audits buries
both.

The approval machinery underneath is shared — `ReconcileService`, the same
approve/skip/finalize the reconcile screen uses — because that is the only code
that deliberately overwrites a snapshot and there is one of it.

WHAT THIS SCREEN RECORDS THAT THE OTHERS DO NOT

A run stores only the SKUs that DIFFERED. A category counted and found entirely
correct therefore leaves no trace at all, and "did we already count the pierces?"
becomes unanswerable. So the execute step demands the counting date, who counted
and what was in scope, and writes them onto the run.
"""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.csv_export import csv_body, csv_response
from app.db import get_session
from app.models import (
    MasterSku,
    ProductCategory,
    ReconcileDiff,
    ReconcileDiffDecisionEnum,
    ReconcileRun,
    ReconcileRunStatusEnum,
    ReconcileRunTypeEnum,
)
from app.services.reconcile import ReconcileService, of_run_type
from app.services.stocktake import build_plan, scope_note
from app.services.timeframe import to_jst_date
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter(prefix="/stocktake")

SOURCE = "stocktake"

#: Only this kind appears here. The reconcile screen owns the other two; see
#: `of_run_type` on why every query naming one kind has to say so.
_RUN_TYPE = ReconcileRunTypeEnum.STOCKTAKE

_FLASH = {
    "created": ("ok", "棚卸を登録しました。差分を確認して承認してください"),
    "badcsv": ("error", "CSVを読み取れませんでした。もう一度アップロードしてください"),
    "nodate": ("error", "棚卸実施日を入力してください"),
    "approved": ("ok", "選択した差分を承認しました"),
    "finalized": ("ok", "棚卸を確定しました"),
    "cancelled": ("ok", "棚卸を取り消しました"),
    "notfound": ("error", "指定された棚卸が見つかりません"),
    "pendingdiffs": (
        "error",
        "未承認の差分が残っています。すべて承認または見送りにしてから確定してください",
    ),
    "cancelfailed": ("error", "承認済みの差分があるため取り消せません"),
}


def _flash(key: str | None) -> dict[str, str] | None:
    """The banner, in the shape `base.html` renders for every screen.

    `kind`, not `level`. This screen used to return `level` and draw its own
    banner too, so every message appeared twice — and the shared one, finding no
    `kind`, coloured errors green. Found while preparing the 2026-10-07 検収,
    where the stocktake screen is one the client operates by hand.
    """
    if not key or key not in _FLASH:
        return None
    kind, message = _FLASH[key]
    return {"kind": kind, "message": message}


# ---------------------------------------------------------------------------
# 取込  (upload -> preview -> execute)
# ---------------------------------------------------------------------------


@router.get("/upload")
async def upload_form(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    categories = await session.execute(
        select(ProductCategory.code, ProductCategory.name)
        .where(ProductCategory.parent_id.is_(None))
        .order_by(ProductCategory.sort_order, ProductCategory.name)
    )
    return templates.TemplateResponse(
        request,
        "stocktake_upload.html",
        {
            "operator": operator,
            "version": __version__,
            "categories": categories.all(),
            "today": to_jst_date(datetime.now(UTC)),
            "flash": _flash(request.query_params.get("flash")),
        },
    )


async def _preview_rows(session: AsyncSession, plan: Any) -> list[dict[str, Any]]:
    """Diffs with their SKU codes, largest change first.

    Sorted by magnitude rather than by code: a count that moves a SKU by 200 is
    the row somebody needs to look at, and burying it alphabetically among
    single-unit corrections is how it gets approved without being read.
    """
    if not plan.diffs:
        return []
    ids = [d.master_sku_id for d in plan.diffs]
    rows = await session.execute(
        select(MasterSku.id, MasterSku.sku_code, MasterSku.name).where(MasterSku.id.in_(ids))
    )
    by_id = {mid: (code, name) for mid, code, name in rows.all()}
    out = [
        {
            "master_sku_id": d.master_sku_id,
            "sku_code": by_id.get(d.master_sku_id, ("?", ""))[0],
            "name": by_id.get(d.master_sku_id, ("", ""))[1],
            "current_qty": d.current_qty,
            "target_qty": d.target_qty,
            "delta": d.target_qty - d.current_qty,
        }
        for d in plan.diffs
    ]
    out.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return out


@router.post("/upload")
async def upload_preview(
    request: Request,
    operator: OperatorDep,
    file: Annotated[UploadFile, File()],
    counted_on: Annotated[str, Form()] = "",
    counted_by: Annotated[str, Form()] = "",
    categories: Annotated[list[str], Form()] = [],  # noqa: B006
    session: AsyncSession = Depends(get_session),
) -> Response:
    data = await file.read()
    inspection, plan = await build_plan(session, data)
    return templates.TemplateResponse(
        request,
        "stocktake_preview.html",
        {
            "operator": operator,
            "version": __version__,
            "filename": file.filename or "stocktake.csv",
            "inspection": inspection.as_dict(),
            "plan": plan,
            "rows": await _preview_rows(session, plan),
            "counted_on": counted_on,
            "counted_by": counted_by or operator,
            "categories": categories,
            "csv_b64": base64.b64encode(data).decode("ascii") if not inspection.fatal else "",
        },
    )


@router.post("/execute")
async def upload_execute(
    operator: OperatorDep,
    csv_b64: Annotated[str, Form()],
    counted_on: Annotated[str, Form()],
    counted_by: Annotated[str, Form()] = "",
    categories: Annotated[list[str], Form()] = [],  # noqa: B006
    filename: Annotated[str, Form()] = "stocktake.csv",
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        data = base64.b64decode(csv_b64)
    except (ValueError, TypeError):
        return RedirectResponse(url="/admin/stocktake/upload?flash=badcsv", status_code=303)

    try:
        counted_date = date.fromisoformat(counted_on)
    except ValueError:
        # Required, and deliberately not defaulted to today: a sheet counted on
        # Friday and entered on Monday would be filed three days late, and the
        # history exists precisely to answer when something was counted.
        return RedirectResponse(url="/admin/stocktake/upload?flash=nodate", status_code=303)

    inspection, plan = await build_plan(session, data)
    if inspection.fatal:
        return RedirectResponse(url="/admin/stocktake/upload?flash=badcsv", status_code=303)

    # Explicit commit, NOT `async with session.begin()`. Everything above
    # already read through this session, which autobegins a transaction —
    # `begin()` then opens a second one and raises
    # `InvalidRequestError: A transaction is already begun`. It took the
    # production smoke test on 2026-09-23 to find: the same mistake had been
    # fixed in three CLIs weeks earlier, and the guard written for it only
    # looked at app/cli.
    run = await ReconcileService(session).start_run(
        source=SOURCE,
        triggered_by=operator,
        diffs=iter(plan.diffs),
        csv_filename=filename,
        run_type=_RUN_TYPE,
        counted_by=counted_by or operator,
        counted_on=counted_date,
        scope_note=scope_note(categories, len(plan.counted)),
        counted_sku_count=len(plan.counted),
    )
    run_id = run.id
    await session.commit()
    return RedirectResponse(url=f"/admin/stocktake/{run_id}?flash=created", status_code=303)


# ---------------------------------------------------------------------------
# 履歴と承認  (P2-029 / P2-030)
# ---------------------------------------------------------------------------


@router.get("")
async def stocktake_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    runs = (
        (
            await session.execute(
                select(ReconcileRun)
                .where(of_run_type(_RUN_TYPE))
                .order_by(ReconcileRun.started_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(ReconcileRun)
        .where(
            of_run_type(_RUN_TYPE),
            ReconcileRun.status == ReconcileRunStatusEnum.PENDING_APPROVAL.value,
        )
    )
    return templates.TemplateResponse(
        request,
        "stocktake_list.html",
        {
            "operator": operator,
            "version": __version__,
            "runs": runs,
            "pending_runs": pending or 0,
            "flash": _flash(request.query_params.get("flash")),
        },
    )


async def _load_run(session: AsyncSession, run_id: int) -> ReconcileRun | None:
    """A run of ANOTHER kind is treated as not found.

    The same guard the reconcile screen carries, in the opposite direction: a
    screen that will not display a Shopify audit must not mutate one either,
    and these POSTs are reachable by id alone.
    """
    run = await session.get(ReconcileRun, run_id)
    if run is None or run.run_type != _RUN_TYPE.value:
        return None
    return run


@router.get("/{run_id}")
async def stocktake_detail(
    request: Request,
    operator: OperatorDep,
    run_id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    run = await _load_run(session, run_id)
    if run is None:
        return RedirectResponse(url="/admin/stocktake?flash=notfound", status_code=303)

    diffs = (
        (
            await session.execute(
                select(
                    ReconcileDiff.id,
                    ReconcileDiff.master_sku_id,
                    ReconcileDiff.current_qty,
                    ReconcileDiff.target_qty,
                    ReconcileDiff.delta,
                    ReconcileDiff.decision,
                    MasterSku.sku_code,
                    MasterSku.name,
                )
                .join(MasterSku, MasterSku.id == ReconcileDiff.master_sku_id)
                .where(ReconcileDiff.reconcile_run_id == run_id)
                .order_by(func.abs(ReconcileDiff.delta).desc())
            )
        )
        .mappings()
        .all()
    )
    pending = [d for d in diffs if d["decision"] == ReconcileDiffDecisionEnum.PENDING.value]
    # The 増 / 減 card counts the stock this run moved, or still stands to move --
    # NOT what is left to decide, which the 未承認の差分 card beside it already
    # says. Counting only the pending rows made every finished run read 0 / 0
    # above a 差分一覧 full of changes, which an operator reads as "the count did
    # nothing" on the one screen they open to confirm that it did something.
    #
    # A cancelled run is left out rather than counted: its diffs are all still
    # pending, because `cancel_run` refuses once any were approved, so counting
    # them would promise stock changes that were never written.
    still_applies = run.status == ReconcileRunStatusEnum.PENDING_APPROVAL.value
    moving = [
        d
        for d in diffs
        if d["decision"] == ReconcileDiffDecisionEnum.APPROVED.value
        or (still_applies and d["decision"] == ReconcileDiffDecisionEnum.PENDING.value)
    ]
    return templates.TemplateResponse(
        request,
        "stocktake_detail.html",
        {
            "operator": operator,
            "version": __version__,
            "run": run,
            "diffs": diffs,
            "pending_count": len(pending),
            "increase": sum(1 for d in moving if d["delta"] > 0),
            "decrease": sum(1 for d in moving if d["delta"] < 0),
            "flash": _flash(request.query_params.get("flash")),
        },
    )


@router.post("/{run_id}/approve")
async def approve_selected(
    operator: OperatorDep,
    run_id: int,
    diff_ids: Annotated[list[int], Form()] = [],  # noqa: B006
    session: AsyncSession = Depends(get_session),
) -> Response:
    """一括承認 (P2-029).

    Approves ONLY the ids submitted. A "select all" checkbox on the page ticks
    the boxes, so what arrives here is always an explicit list — there is no
    "approve everything" endpoint that could be hit with an empty form and
    apply a hundred corrections nobody looked at.
    """
    run = await _load_run(session, run_id)
    if run is None:
        return RedirectResponse(url="/admin/stocktake?flash=notfound", status_code=303)
    if not diff_ids:
        return RedirectResponse(url=f"/admin/stocktake/{run_id}", status_code=303)

    service = ReconcileService(session)
    for diff_id in diff_ids:
        await service.approve_diff(run_id=run_id, diff_id=diff_id, approved_by=operator)
    await session.commit()
    return RedirectResponse(url=f"/admin/stocktake/{run_id}?flash=approved", status_code=303)


@router.post("/{run_id}/skip")
async def skip_selected(
    operator: OperatorDep,
    run_id: int,
    diff_ids: Annotated[list[int], Form()] = [],  # noqa: B006
    session: AsyncSession = Depends(get_session),
) -> Response:
    run = await _load_run(session, run_id)
    if run is None:
        return RedirectResponse(url="/admin/stocktake?flash=notfound", status_code=303)
    if not diff_ids:
        return RedirectResponse(url=f"/admin/stocktake/{run_id}", status_code=303)

    service = ReconcileService(session)
    for diff_id in diff_ids:
        await service.skip_diff(diff_id=diff_id, approved_by=operator)
    await session.commit()
    return RedirectResponse(url=f"/admin/stocktake/{run_id}?flash=approved", status_code=303)


@router.post("/{run_id}/finalize")
async def finalize(
    operator: OperatorDep,
    run_id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    run = await _load_run(session, run_id)
    if run is None:
        return RedirectResponse(url="/admin/stocktake?flash=notfound", status_code=303)
    # `finalize_run` REFUSES while any diff is still pending, so this needs the
    # same guard the reconcile screen has had since W6. Without it the operator
    # gets an HTTP 500 — on the one screen the client drives by hand, during the
    # count the whole Rakuten SKU migration was built for.
    try:
        await ReconcileService(session).finalize_run(run_id=run_id, approved_by=operator)
    except (RuntimeError, ValueError):
        await session.rollback()
        return RedirectResponse(
            url=f"/admin/stocktake/{run_id}?flash=pendingdiffs", status_code=303
        )
    await session.commit()
    return RedirectResponse(url=f"/admin/stocktake/{run_id}?flash=finalized", status_code=303)


@router.post("/{run_id}/cancel")
async def cancel(
    operator: OperatorDep,
    run_id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    run = await _load_run(session, run_id)
    if run is None:
        return RedirectResponse(url="/admin/stocktake?flash=notfound", status_code=303)
    # `cancel_run` refuses once anything has been approved, for the same reason:
    # those stock changes are already written and cancelling cannot unwrite them.
    try:
        await ReconcileService(session).cancel_run(run_id=run_id, cancelled_by=operator)
    except (RuntimeError, ValueError):
        await session.rollback()
        return RedirectResponse(
            url=f"/admin/stocktake/{run_id}?flash=cancelfailed", status_code=303
        )
    await session.commit()
    return RedirectResponse(url=f"/admin/stocktake/{run_id}?flash=cancelled", status_code=303)


@router.get("/{run_id}/export.csv")
async def export_run(
    operator: OperatorDep,
    run_id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    run = await _load_run(session, run_id)
    if run is None:
        return RedirectResponse(url="/admin/stocktake?flash=notfound", status_code=303)
    rows = (
        (
            await session.execute(
                select(
                    MasterSku.sku_code,
                    MasterSku.name,
                    ReconcileDiff.current_qty,
                    ReconcileDiff.target_qty,
                    ReconcileDiff.delta,
                    ReconcileDiff.decision,
                )
                .join(MasterSku, MasterSku.id == ReconcileDiff.master_sku_id)
                .where(ReconcileDiff.reconcile_run_id == run_id)
                .order_by(func.abs(ReconcileDiff.delta).desc())
            )
        )
        .mappings()
        .all()
    )
    body = csv_body(
        ["SKUコード", "商品名", "システム在庫数", "実数", "差分", "判定"],
        [
            [r["sku_code"], r["name"], r["current_qty"], r["target_qty"], r["delta"], r["decision"]]
            for r in rows
        ],
    )
    header = (
        f"# 棚卸日 {run.counted_on} / 実施者 {run.counted_by or '-'} / 範囲 {run.scope_note or '-'}"
    )
    note = csv_body([header], [])
    return csv_response(note + body, filename=f"stocktake_{run_id}.csv")
