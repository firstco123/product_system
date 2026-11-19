"""実地棚卸 (P2-028) — counting in instalments without emptying the shelf.

The client confirmed on 2026-09-22 that they will count by category: RING is
265 SKUs, KEYRING is 3, and a single day covers one of them. So every upload is
a PARTIAL count, and the defining property of this module is what it does with
the SKUs that are not on the sheet.

It does nothing with them. An absent row is not a count of zero, and neither is
a blank cell. Reading either as zero would, on the first RING day, propose
emptying every necklace, bracelet, pierce, anklet and key ring in the
catalogue — and each proposal would arrive in the approval queue looking like a
routine correction, because a queue of proposed corrections is exactly what it
is.

That is the same rule the Shopify audit calls "absent is not zero". It is worth
restating in its own tests because here the absent rows are the majority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.services.stocktake import (
    CountedRow,
    StocktakePlan,
    parse,
    plan_from_rows,
    read_excluded_skus,
    rows_from,
    scope_note,
)

pytestmark = pytest.mark.unit

HEADER = "sku_code,counted_qty\n"


def _csv(body: str, header: str = HEADER) -> bytes:
    return (header + body).encode("utf-8-sig")


# --- parsing --------------------------------------------------------------


def test_a_well_formed_sheet_parses() -> None:
    inspection = parse(_csv("N108gold,12\nR34silverus5,3\n"))
    assert inspection.fatal == []
    assert inspection.valid_rows == 2


def test_the_japanese_headers_the_sheet_ships_with_are_accepted() -> None:
    """The sheet goes to Excel and comes back however the counter left it."""
    inspection = parse(_csv("N108gold,12\n", header="SKUコード,実数\n"))
    assert inspection.fatal == []
    assert inspection.valid_rows == 1


def test_a_blank_count_is_skipped_not_read_as_zero() -> None:
    """A row the counter did not reach. Treating it as zero would empty that
    SKU on approval — the same mistake as an absent row, one cell smaller."""
    rows, _ = rows_from(_csv("N108gold,\nR34silverus5,3\n"))
    assert [r.sku_code for r in rows] == ["R34silverus5"]


def test_a_counted_zero_is_kept() -> None:
    """Zero written by a person means the shelf was empty, and that IS a
    finding. Only an absent or blank value is "not counted"."""
    rows, _ = rows_from(_csv("N108gold,0\n"))
    assert rows == [CountedRow(sku_code="N108gold", counted_qty=0)]


def test_a_non_numeric_count_is_reported_with_its_line() -> None:
    inspection = parse(_csv("N108gold,twelve\n"))
    assert inspection.row_issues
    assert inspection.valid_rows == 0


def test_an_empty_file_is_fatal() -> None:
    assert parse(b"").fatal


def test_a_missing_column_is_fatal_rather_than_a_silent_skip() -> None:
    assert parse(_csv("N108gold\n", header="sku_code\n")).fatal


# --- duplicates -----------------------------------------------------------


def test_the_same_sku_counted_twice_is_reported_not_summed() -> None:
    """Two lines usually means two people counted one shelf. Adding them
    doubles the stock, silently, in the direction that hides a shortage."""
    rows, duplicates = rows_from(_csv("N108gold,5\nN108gold,7\n"))
    assert duplicates == ["N108gold"]
    assert [(r.sku_code, r.counted_qty) for r in rows] == [("N108gold", 5)]


# --- the plan -------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Answers `plan_from_rows`: masters by sku_code, then their snapshots."""

    def __init__(self, masters: dict[str, int], on_hand: dict[int, int]) -> None:
        self._answers = [list(masters.items()), list(on_hand.items())]

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._answers.pop(0) if self._answers else [])


async def test_only_the_counted_skus_are_touched() -> None:
    """THE rule. Masters 2 and 3 exist and hold stock; they are not on the
    sheet, so they produce no diff and nothing will zero them."""
    session = _FakeSession({"A": 1}, {1: 10, 2: 40, 3: 7})
    plan = await plan_from_rows(session, [CountedRow("A", 4)])  # type: ignore[arg-type]
    assert [(d.master_sku_id, d.current_qty, d.target_qty) for d in plan.diffs] == [(1, 10, 4)]


async def test_a_sku_counted_at_its_recorded_value_produces_no_diff() -> None:
    """Approving a no-op would still write a stocktake event. The count is
    recorded as `matched` so the screen can say what was checked."""
    session = _FakeSession({"A": 1}, {1: 10})
    plan = await plan_from_rows(session, [CountedRow("A", 10)])  # type: ignore[arg-type]
    assert plan.diffs == []
    assert plan.matched == 1


async def test_a_sku_with_no_snapshot_is_compared_against_zero() -> None:
    """A master registered but never stocked — the 26 added on 2026-09-17 are
    exactly this. Counting 5 of them is a real correction from nothing."""
    session = _FakeSession({"A": 1}, {})
    plan = await plan_from_rows(session, [CountedRow("A", 5)])  # type: ignore[arg-type]
    assert [(d.current_qty, d.target_qty) for d in plan.diffs] == [(0, 5)]


async def test_an_unknown_sku_is_reported_rather_than_dropped() -> None:
    """A typo on a count sheet is a real shelf count that would otherwise
    vanish without anyone being told."""
    session = _FakeSession({"A": 1}, {1: 10})
    plan = await plan_from_rows(session, [CountedRow("A", 4), CountedRow("TYPO", 9)])  # type: ignore[arg-type]
    assert [r.sku_code for r in plan.unknown] == ["TYPO"]
    assert len(plan.diffs) == 1


async def test_counting_nothing_plans_nothing() -> None:
    plan = await plan_from_rows(_FakeSession({}, {}), [])  # type: ignore[arg-type]
    assert plan.diffs == []
    assert plan.has_differences is False


async def test_a_negative_recorded_balance_is_corrected_upward() -> None:
    """The 26 new masters go negative until counted. A stocktake is the only
    thing that can bring them back, and the diff must be the full distance."""
    session = _FakeSession({"A": 1}, {1: -4})
    plan = await plan_from_rows(session, [CountedRow("A", 6)])  # type: ignore[arg-type]
    assert [(d.current_qty, d.target_qty) for d in plan.diffs] == [(-4, 6)]


# --- scope ----------------------------------------------------------------


def test_the_scope_note_records_what_was_counted() -> None:
    """A run stores only the SKUs that DIFFERED, so a category counted and
    found entirely correct leaves no trace. Without this, "did we ever count
    the pierces?" has no answer."""
    note = scope_note(["リング", "ピアス"], 321)
    assert "リング・ピアス" in note
    assert "321" in note


def test_an_unscoped_count_says_so_rather_than_reading_as_everything() -> None:
    assert "指定なし" in scope_note([], 10)


def test_the_plan_totals_what_was_counted() -> None:
    plan = StocktakePlan(counted=[CountedRow("A", 4), CountedRow("B", 6)])
    assert plan.total_counted_qty == 10


@pytest.mark.unit
def test_the_real_sheet_exclusion_file_loads() -> None:
    path = Path("csv_file/phase2/source/stocktake_excluded_skus.csv")
    if not path.exists():
        pytest.skip("the exclusion file is not in this checkout")
    excluded, problems = read_excluded_skus(path)
    assert problems == []
    assert "073c" in excluded
    # The gift-set parents belong on this list, and the count alone would not
    # say so: `sheet_rows` uses operational_conditions(include_bundles=True), so
    # all 48 parents would print on the next count sheet reading 0;
    # `plan_from_rows` has no is_bundle guard and `_lock_or_create_snapshot`
    # creates a snapshot for any master id handed to it, so a counted gift box
    # writes phantom stock onto a parent that no screen reads.
    assert "01gift-B66silverS-B66silverS" in excluded
    assert len(excluded) == 62


@pytest.mark.unit
def test_a_missing_sheet_exclusion_file_excludes_nothing() -> None:
    excluded, problems = read_excluded_skus(Path("no/such/file.csv"))
    assert excluded == set()
    assert problems == []


@pytest.mark.unit
def test_an_exclusion_file_without_the_column_is_reported() -> None:
    """A silently empty exclusion list puts the superseded masters back on the
    sheet, where the client counts one shelf twice."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.csv"
        path.write_text("code,reason\n073c,x\n", encoding="utf-8")
        excluded, problems = read_excluded_skus(path)

    assert excluded == set()
    assert problems and "sku_code" in problems[0]
