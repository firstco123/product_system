"""棚卸の画面 (P2-028..030).

Two things this screen must say out loud, because the operator is about to
overwrite stock and cannot see what the code does.

**A SKU not on the sheet is untouched.** Every upload is a partial count — the
client counts one category a day — so the reassurance has to be on the upload
page itself, not in a manual nobody opens mid-count.

**A duplicate is not summed.** Two lines for one SKU means two people counted
one shelf. The screen says the first was kept, so the operator knows to check
rather than assuming the number they see is a total.

The bulk approve is the other risk. There is no "approve everything" endpoint:
the form always posts an explicit list of ids, and the buttons stay disabled at
zero selected so a click that would do nothing cannot read as a broken screen.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.reconcile import DiffInput
from app.services.stocktake import CountedRow, StocktakePlan
from app.ui.deps import templates

pytestmark = pytest.mark.unit


def _render(name: str, **ctx: Any) -> str:
    base = {"operator": "tester", "version": "test", "flash": None}
    return templates.env.get_template(name).render(**{**base, **ctx})


# --- upload ---------------------------------------------------------------


def _upload(**over: Any) -> str:
    ctx = {
        "categories": [("RING", "リング"), ("NECKLACE", "ネックレス")],
        "today": date(2026, 10, 12),
    }
    ctx.update(over)
    return _render("stocktake_upload.html", **ctx)


def test_the_upload_page_promises_untouched_skus_stay_untouched() -> None:
    """The reassurance that makes a partial count safe to run. On a RING day
    449 SKUs are absent from the file, and the operator has to know that is
    expected rather than a mistake they are about to make."""
    html = _upload()
    assert "シートに無いSKUは変更されません" in html
    assert "ゼロになったり変更されたりすることはありません" in html


def test_a_blank_count_cell_is_explained_as_uncounted() -> None:
    assert "実数欄が空欄の行も「未計数」" in _upload()


def test_the_counting_date_is_distinguished_from_the_upload_date() -> None:
    """A sheet counted on Friday and entered on Monday would otherwise be filed
    three days late, and the history exists to answer when it was counted."""
    html = _upload()
    assert "実際に数えた日" in html
    assert "取込日ではありません" in html


def test_the_scope_checkboxes_say_they_do_not_filter() -> None:
    """They record what was covered. Reading them as a filter would make an
    operator think unticking a box protects those SKUs."""
    html = _upload()
    assert "取込対象を絞るわけではありません" in html


def test_the_categories_come_from_the_database() -> None:
    assert "リング" in _upload()


# --- preview --------------------------------------------------------------


def _plan(**over: Any) -> StocktakePlan:
    plan = StocktakePlan(
        counted=[CountedRow("K01", 64), CountedRow("K02", 71)],
        diffs=[DiffInput(master_sku_id=210, current_qty=77, target_qty=71)],
        matched=1,
    )
    for key, value in over.items():
        setattr(plan, key, value)
    return plan


def _preview(**over: Any) -> str:
    ctx: dict[str, Any] = {
        "filename": "stocktake_KEYRING.csv",
        "inspection": {"fatal": [], "row_issues": [], "total_rows": 3, "valid_rows": 3},
        "plan": _plan(),
        "rows": [
            {
                "master_sku_id": 210,
                "sku_code": "K02",
                "name": "316L anchor keyring",
                "current_qty": 77,
                "target_qty": 71,
                "delta": -6,
            }
        ],
        "counted_on": "2026-10-12",
        "counted_by": "tester",
        "categories": ["キーリング"],
        "csv_b64": "AAA=",
    }
    ctx.update(over)
    return _render("stocktake_preview.html", **ctx)


def test_the_preview_shows_the_difference_and_what_matched() -> None:
    """ "8 differ" alone leaves the rest ambiguous — were they checked or not
    even read?"""
    html = _preview()
    assert "K02" in html
    assert "-6" in html
    assert "計数したSKU" in html


def test_a_fatal_file_shows_the_reason_and_no_execute_button() -> None:
    broken = {
        "fatal": ["ヘッダー行がありません。"],
        "row_issues": [],
        "total_rows": 0,
        "valid_rows": 0,
    }
    html = _preview(inspection=broken)
    assert "このファイルは取り込めません" in html
    assert "この内容で登録する" not in html


def test_unknown_skus_are_listed_with_their_counts() -> None:
    """A typo is a real shelf count that would otherwise vanish silently."""
    html = _preview(plan=_plan(unknown=[CountedRow("TYPOO", 9)]))
    assert "商品が見つからない行" in html
    assert "TYPOO" in html


def test_duplicates_say_the_first_was_kept_and_nothing_was_summed() -> None:
    html = _preview(plan=_plan(duplicates=["K01"]))
    assert "合算はしていません" in html
    assert "二重に数えた" in html


def test_no_differences_is_stated_as_a_result_not_an_empty_table() -> None:
    html = _preview(rows=[], plan=_plan(diffs=[], matched=2))
    assert "差分はありません" in html


def test_the_preview_says_nothing_has_changed_yet() -> None:
    html = _preview()
    assert "在庫は変更されていません" in html


# --- detail / bulk approve ------------------------------------------------


class _Run:
    def __init__(self, **kw: Any) -> None:
        self.id = kw.get("id", 7)
        self.status = kw.get("status", "pending_approval")
        self.counted_on = kw.get("counted_on", date(2026, 10, 12))
        self.counted_by = kw.get("counted_by", "店長")
        self.scope_note = kw.get("scope_note", "キーリング / 3SKUを計数")
        self.counted_sku_count = kw.get("counted_sku_count", 3)
        self.diff_count = kw.get("diff_count", 2)
        self.applied_count = kw.get("applied_count", 0)
        self.started_at = kw.get("started_at", datetime(2026, 10, 12, tzinfo=UTC))


def _diff(**kw: Any) -> dict[str, Any]:
    base = {
        "id": 1,
        "master_sku_id": 210,
        "sku_code": "K02",
        "name": "keyring",
        "current_qty": 77,
        "target_qty": 71,
        "delta": -6,
        "decision": "pending",
    }
    base.update(kw)
    return base


def _detail(**over: Any) -> str:
    diffs = over.pop("diffs", [_diff(), _diff(id=2, sku_code="K03", delta=5, decision="approved")])
    pending = [d for d in diffs if d["decision"] == "pending"]
    ctx: dict[str, Any] = {
        "run": _Run(**over.pop("run", {})),
        "diffs": diffs,
        "pending_count": len(pending),
        "increase": sum(1 for d in pending if d["delta"] > 0),
        "decrease": sum(1 for d in pending if d["delta"] < 0),
    }
    ctx.update(over)
    return _render("stocktake_detail.html", **ctx)


def test_the_detail_page_renders_with_a_checkbox_per_pending_row() -> None:
    html = _detail()
    assert 'name="diff_ids"' in html
    assert "選択を承認" in html


def test_an_already_decided_row_has_no_checkbox() -> None:
    """Approving twice is idempotent in the service, but offering the action
    implies it is outstanding."""
    html = _detail(diffs=[_diff(decision="approved")])
    assert 'name="diff_ids"' not in html
    assert "承認済" in html


def test_the_bulk_buttons_start_disabled() -> None:
    """Zero selected posts nothing. An enabled button that does nothing reads
    as a broken screen."""
    html = _detail()
    assert "disabled" in html


def test_approval_is_described_as_overwriting_and_reversible() -> None:
    html = _detail()
    assert "実数で上書き" in html
    assert "手動調整で戻せます" in html


def test_the_scope_is_shown_because_matched_skus_leave_no_row() -> None:
    html = _detail()
    assert "キーリング" in html


def test_a_finalised_run_offers_no_approval_actions() -> None:
    html = _detail(run={"status": "applied"}, diffs=[_diff(decision="approved")])
    assert "この棚卸を確定する" not in html


def test_an_empty_diff_list_explains_itself() -> None:
    html = _detail(diffs=[])
    assert "差分はありません" in html


# --- list -----------------------------------------------------------------


def test_the_history_lists_runs_with_their_scope() -> None:
    html = _render(
        "stocktake_list.html",
        runs=[_Run()],
        pending_runs=1,
    )
    assert "キーリング" in html
    assert "承認待ち" in html


def test_an_empty_history_tells_the_operator_what_to_do() -> None:
    html = _render("stocktake_list.html", runs=[], pending_runs=0)
    assert "棚卸の履歴はまだありません" in html
    assert "カウントシートを出力" in html


def test_the_list_explains_why_matched_skus_are_absent() -> None:
    """Otherwise "差分 0" on a counted category looks like nothing happened."""
    html = _render("stocktake_list.html", runs=[_Run()], pending_runs=0)
    assert "一致した商品は記録に残らない" in html


def test_the_count_date_is_not_prefilled_with_today() -> None:
    """A prefilled 棚卸日 is the one wrong default nobody notices.

    The client counts on Tuesday and the figures reach us on Thursday; a field
    already holding Thursday files the count two days late, and the route's own
    comment says it must not be defaulted for exactly that reason. In wholesale
    the cost is worse than a mislabelled history: the cutover is 棚卸日 + 1 day,
    so a date one day late silently drops that day's vouchers out of the import
    and their stock never moves.
    """
    html = _upload()
    assert 'name="counted_on"' in html
    assert 'value="2026-10-12"' not in html
    # Still required, so an empty field cannot be submitted by accident...
    assert 'name="counted_on" required' in html
    # ...and a future date cannot be chosen, since nobody counted tomorrow.
    assert 'max="2026-10-12"' in html


def test_the_count_date_says_it_sets_the_wholesale_cutover() -> None:
    """The operator cannot be expected to know that this field moves the
    wholesale import's scope unless the field says so."""
    html = _upload()
    assert "取込日ではありません" in html
    assert "翌日" in html
