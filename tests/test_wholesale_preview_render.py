"""卸売取込の確認画面 (docs/29 §8).

The operator approves this screen before any stock moves, so what it refuses to
submit matters as much as what it shows. The gift bag is the awkward member:
it is a real line on a real voucher that records a sale and moves no stock, so
a screen that counts only stock movement decides there is nothing to import.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.ui.deps import templates

pytestmark = pytest.mark.unit


class _Plan:
    """Only the counters the submit control reads."""

    def __init__(self, *, movable: int = 0, unmapped: int = 0, unmanaged: int = 0) -> None:
        self.movable_line_count = movable
        self.unmapped_line_count = unmapped
        self.unmanaged_line_count = unmanaged
        self.voucher_conflicts: list[str] = []
        self.lines_on_cutover_count = 0
        self.recounted_line_count = 0
        self.vouchers: list[Any] = []
        self.excluded_line_count = 0
        self.beyond_line_count = 0
        self.negative_line_count = 0
        self.total_delta = 0
        self.kind = "order"


def _render(**plan_kw: int) -> str:
    return templates.env.get_template("wholesale_preview.html").render(
        operator="tester",
        version="test",
        flash=None,
        plan=_Plan(**plan_kw),
        reading=None,
        file_b64="",
        filename="x.csv",
        cutover="2026-11-11",
        basis_run=60,
    )


def _submit_disabled(html: str) -> bool:
    start = html.index('name="file_b64"')
    button = html[start : html.index("</button>", start)]
    return "disabled" in button.split('class="')[0]


def test_a_voucher_of_only_gift_bag_lines_can_still_be_imported() -> None:
    """The regression this file exists for.

    A gift-bag line is mapped (so not unmapped) onto a 在庫管理対象外 master (so
    not movable). Before the correspondence table carried the gift bag it was
    unmapped and the button stayed live by accident; mapping it on 2026-11-11
    turned every gift-bag-only voucher into 「取り込む明細がありません」 with the
    button greyed out — while the sale on it still needed recording.
    """
    html = _render(movable=0, unmapped=0, unmanaged=1)

    assert not _submit_disabled(html)
    assert "在庫管理対象外の明細のみです。売上のみ記録し、在庫は動きません" in html
    assert "取り込む明細がありません" not in html


def test_a_plan_with_nothing_at_all_still_refuses() -> None:
    """The guard must keep doing its job: an empty plan offers nothing to click."""
    html = _render(movable=0, unmapped=0, unmanaged=0)

    assert _submit_disabled(html)
    assert "取り込む明細がありません" in html


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"movable": 3}, "stock moves"),
        ({"unmapped": 2}, "the sale is recorded against a NULL master"),
        ({"movable": 3, "unmanaged": 1}, "a mixed voucher"),
    ],
)
def test_anything_worth_importing_leaves_the_button_live(kw: dict[str, int], why: str) -> None:
    html = _render(**kw)

    assert not _submit_disabled(html), why
    assert "在庫と売上を更新します" in html
