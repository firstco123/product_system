"""卸売EDI取込 — upload, check, confirm, run (P2-051, docs/29 §8).

Four steps, the same shape as every other upload screen:
upload → 検証 → 確認 → 実行. Nothing is written until the operator submits the
confirmation, because what they are approving is a set of stock movements whose
direction was decided by reading the file rather than by anything they chose.

WHY THIS IS NOT A TAB ON /admin/reconcile

docs/29 §8 expected to reuse the approval machinery, and the run row is reused —
but the lines are not, and cannot be. A 照合 or a stocktake SETS stock to a
counted absolute and parks its lines in `reconcile_diffs` for approval.
Wholesale is a movement: its lines live in `inventory_events`, keyed by 取引番号
and 取引明細番号, which is what makes a second upload of the same file a no-op.
`reconcile_diffs` is UNIQUE on (run, master_sku_id) and a single voucher may name
the same product twice, so putting these lines there would collapse them and
throw away that key.

So a `wholesale` run carries NO diffs. `ReconcileService.start_run` already marks
a diff-less run APPLIED and stamps `finished_at`, which is exactly right here:
the approval happened on the confirmation screen, before anything was written.
There is nothing left to approve, and `approve_diff` must never be called on one.

WHAT THE CONFIRMATION SCREEN HAS TO SHOW

Three numbers an operator must see before saying yes, none of which is an error:

* **カットオーバー前** — dropped as pre-cutover. Normally most of the file;
  the return file reaches back to June (docs/29 §7-2).
* **未マッピング** — the code resolves to no master. Stock does not move, but the
  sale is still recorded, so the money is not lost while the correspondence
  table is fixed.
* **マイナスになる** — will take a master below zero. Applied anyway, because the
  goods shipped (docs/29 §10-9). This is the number worth reading.
"""

from __future__ import annotations

import base64
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.models import ReconcileRun, ReconcileRunTypeEnum
from app.services.reconcile import ReconcileService, of_run_type
from app.services.wholesale_cutover import check_override, resolve_cutover
from app.services.wholesale_import import WholesaleImportService, WholesalePlan
from app.ui.auth import OperatorDep
from app.ui.deps import templates
from app.wholesale_bms import RULES, BmsReading, read_bms

router = APIRouter(prefix="/wholesale")

SOURCE = "wholesale"

#: Only this kind appears here; the reconcile screen owns two and the stocktake
#: screen the third. See `of_run_type` for why every query says which it means.
_RUN_TYPE = ReconcileRunTypeEnum.WHOLESALE

_FLASH = {
    "applied": ("ok", "卸売EDIファイルを取り込みました"),
    "badfile": ("error", "ファイルを読み取れませんでした。もう一度アップロードしてください"),
    "nocutover": ("error", "カットオーバー日が決まらないため取り込めません"),
}


def _flash(key: str | None) -> dict[str, str] | None:
    if not key or key not in _FLASH:
        return None
    kind, message = _FLASH[key]
    return {"kind": kind, "message": message}


#: How many lines of each worth-reading category the preview lists before it
#: just gives the count. A 1,887-line file would otherwise print a page nobody
#: reads, which is the same as printing nothing.
PREVIEW_LIMIT = 50


def _rows(plan: WholesalePlan) -> list[dict[str, object]]:
    """The lines that will move stock, worst first.

    Ordered by how far below zero each lands, so the rows that need a decision
    are the ones on screen rather than whichever voucher happens to be first.
    """
    movable = sorted(
        plan.movable_lines,
        key=lambda line: line.projected_qty if line.projected_qty is not None else 0,
    )
    return [
        {
            "voucher": line.line.voucher,
            "voucher_line": line.line.voucher_line,
            "product_code": line.line.product_code,
            "spec_kana": line.line.spec_kana,
            "sku_code": line.sku_code,
            "master_name": line.master_name,
            "quantity_delta": line.quantity_delta,
            "current_qty": line.current_qty,
            "projected_qty": line.projected_qty,
            "goes_negative": line.goes_negative,
        }
        for line in movable[:PREVIEW_LIMIT]
    ]


def _unmapped_rows(plan: WholesalePlan) -> list[dict[str, object]]:
    seen: dict[str, dict[str, object]] = {}
    for line in plan.unmapped_lines:
        row = seen.setdefault(
            line.line.product_code,
            {
                "product_code": line.line.product_code,
                "spec_kana": line.line.spec_kana,
                "product_name": line.line.product_name,
                "lines": 0,
            },
        )
        row["lines"] = int(row["lines"]) + 1  # type: ignore[call-overload]
    return sorted(seen.values(), key=lambda row: str(row["product_code"]))


# ---------------------------------------------------------------------------
# 取込  (upload -> 検証 -> 確認 -> 実行)
# ---------------------------------------------------------------------------


@router.get("/upload")
async def upload_form(
    request: Request,
    operator: OperatorDep,
    flash: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The form, with the cutover shown before a file is chosen.

    Shown up front because the commonest outcome of an upload is "almost all of
    it was before the cutover", and an operator who does not know the date reads
    that as the file having failed.
    """
    resolution = await resolve_cutover(session)
    return templates.TemplateResponse(
        request,
        "wholesale_upload.html",
        {
            "operator": operator,
            "version": __version__,
            "flash": _flash(flash),
            "cutover": resolution.cutover_on,
            "cutover_reason": resolution.reason,
            "counted_on": [day.isoformat() for day in resolution.counted_on],
        },
    )


async def _plan_for(
    session: AsyncSession,
    data: bytes,
    cutover_text: str,
) -> tuple[BmsReading, WholesalePlan | None, str]:
    """Read the file and plan it, or explain why there is no plan.

    Returns (reading, plan, refusal). A refusal is a sentence for the operator,
    not an exception: both "this file cannot be read" and "there is no cutover"
    are ordinary states of the upload screen.
    """
    reading = read_bms(data)
    if reading.fatal or reading.kind is None:
        return reading, None, ""

    if cutover_text:
        try:
            chosen = date.fromisoformat(cutover_text)
        except ValueError:
            return (
                reading,
                None,
                "カットオーバー日の形式が正しくありません。YYYY-MM-DD で入力してください。",
            )
        refusal = check_override(chosen)
        if refusal is not None:
            return reading, None, refusal
        cutover = chosen
    else:
        resolution = await resolve_cutover(session)
        if not resolution.ok:
            return reading, None, resolution.reason
        assert resolution.cutover_on is not None  # narrowed by `ok`
        cutover = resolution.cutover_on

    plan = await WholesaleImportService(session).plan(reading, cutover_on=cutover)
    return reading, plan, ""


@router.post("/upload")
async def upload_preview(
    request: Request,
    operator: OperatorDep,
    file: Annotated[UploadFile, File()],
    cutover: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
) -> Response:
    data = await file.read()
    reading, plan, refusal = await _plan_for(session, data, cutover)
    return templates.TemplateResponse(
        request,
        "wholesale_preview.html",
        {
            "operator": operator,
            "version": __version__,
            "filename": file.filename or "wholesale.csv",
            "reading": reading.as_dict(),
            "kind_label": RULES[reading.kind].label if reading.kind is not None else "",
            "plan": plan.as_dict() if plan is not None else None,
            "rows": _rows(plan) if plan is not None else [],
            "unmapped": _unmapped_rows(plan) if plan is not None else [],
            "preview_limit": PREVIEW_LIMIT,
            "refusal": refusal,
            "cutover": cutover,
            # Carried through the confirmation rather than kept server-side, the
            # same way the stocktake screen does it: the plan is re-derived from
            # these bytes on execute, so the two steps cannot disagree.
            "file_b64": base64.b64encode(data).decode("ascii") if plan is not None else "",
        },
    )


@router.post("/execute")
async def upload_execute(
    operator: OperatorDep,
    file_b64: Annotated[str, Form()],
    filename: Annotated[str, Form()] = "wholesale.csv",
    cutover: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        data = base64.b64decode(file_b64)
    except (ValueError, TypeError):
        return RedirectResponse(url="/admin/wholesale/upload?flash=badfile", status_code=303)

    # Re-read and re-plan rather than trusting anything from the form. The only
    # thing carried across is the file itself.
    _reading, plan, refusal = await _plan_for(session, data, cutover)
    if plan is None:
        flash = "nocutover" if refusal else "badfile"
        return RedirectResponse(url=f"/admin/wholesale/upload?flash={flash}", status_code=303)

    result = await WholesaleImportService(session).apply(plan, operator=operator)

    # A run with no diffs: start_run marks it APPLIED and stamps finished_at,
    # which is what this is — the record of an import already confirmed on the
    # previous screen. See this module's docstring on why the lines are not here.
    run = await ReconcileService(session).start_run(
        source=SOURCE,
        triggered_by=operator,
        diffs=iter(()),
        csv_filename=filename,
        run_type=_RUN_TYPE,
        counted_on=plan.cutover_on,
        scope_note=(
            f"{RULES[plan.kind].label} / 対象 {len(plan.vouchers)}伝票 "
            f"{len(plan.movable_lines)}明細 / "
            f"カットオーバー前 {len(plan.excluded_vouchers)}伝票 除外"
        ),
    )
    run.applied_count = result.applied_lines
    run.notes = (
        f"在庫 {result.applied_lines}明細 (イベント {result.events_written}件, "
        f"数量 {result.total_delta:+d}) / 売上 {result.orders_written}伝票 "
        f"{result.order_lines_written}明細 / 対象外 {result.unmanaged_lines}明細 / "
        f"重複 {result.duplicate_lines}明細"
    )

    # Explicit commit, NOT `async with session.begin()`: everything above has
    # already read through this session, which autobegins a transaction, and
    # `begin()` would then raise "A transaction is already begun". Same trap the
    # stocktake execute route documents.
    await session.commit()
    return RedirectResponse(url="/admin/wholesale?flash=applied", status_code=303)


# ---------------------------------------------------------------------------
# 履歴
# ---------------------------------------------------------------------------


@router.get("")
async def wholesale_list(
    request: Request,
    operator: OperatorDep,
    flash: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Which files have been imported, when and by whom.

    The only answer to "did we already load Monday's shipments?" — the lines
    themselves are idempotent, so a second import is harmless, but an operator
    should not have to find that out by trying.
    """
    rows = await session.execute(
        select(ReconcileRun)
        .where(of_run_type(_RUN_TYPE))
        .order_by(ReconcileRun.started_at.desc())
        .limit(100)
    )
    return templates.TemplateResponse(
        request,
        "wholesale_list.html",
        {
            "operator": operator,
            "version": __version__,
            "flash": _flash(flash),
            "runs": list(rows.scalars().all()),
        },
    )
