"""ギフトセット registration — one sellable SKU consuming several others.

The Christmas 2026 case: B66 sold as a pair, B34 paired with B66. One order
line, two items off the shelf.

Two things make this worth testing hard. A set whose component is itself a set
would move no stock at all, because `resolve_consumption` expands one level and
stops — so that has to be refused rather than quietly flattened. And a partial
apply is worse than no apply: half a set still fans out, so every order
under-deducts and no screen says so. One bad row therefore refuses the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.cli.create_gift_sets import (
    SPEC,
    group,
    plan,
    quantity_fault,
    read_faults,
    read_file,
)
from app.csv_intake import iter_rows

pytestmark = pytest.mark.unit

HEADER = "セットSKU,セット商品名,構成品SKU,数量"


def _bytes(*lines: str, header: str = HEADER) -> bytes:
    return "\n".join((header, *lines)).encode("utf-8-sig")


def _rows(data: bytes) -> list[dict[str, str]]:
    return list(iter_rows(data, SPEC))


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Answers `plan`'s three reads in order: masters, snapshots, components."""

    def __init__(
        self,
        masters: list[tuple[str, int, bool, Any]],
        stock: list[tuple[int, int]] | None = None,
        components: list[tuple[int, int, int]] | None = None,
    ) -> None:
        self._answers: list[list[Any]] = [masters, stock or [], components or []]

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._answers.pop(0) if self._answers else [])


# (sku_code, id, is_bundle, archived_at)
B66S = ("B66goldS", 1, False, None)
B34S = ("B34silver19cm", 2, False, None)
PARENT = ("N108gold42", 3, True, None)
RETIRED = ("B66old", 4, False, "2026-01-01")


def _reasons(faults: Any) -> str:
    return " / ".join(f.reason for f in faults)


# ---------- reading ----------


def test_a_set_spanning_two_rows_becomes_one_entry() -> None:
    sets, faults = group(
        _rows(
            _bytes(
                "setA,B34とB66のセット,B34silver19cm,1",
                "setA,B34とB66のセット,B66goldS,1",
            )
        )
    )

    assert not faults, _reasons(faults)
    assert len(sets) == 1
    assert [p.component_sku for p in sets[0].parts] == ["B34silver19cm", "B66goldS"]


def test_a_pair_set_keeps_its_quantity() -> None:
    sets, faults = group(_rows(_bytes("setB,B66 2個セット,B66goldS,2")))

    assert not faults
    assert sets[0].parts[0].quantity == 2


def test_the_guidance_lines_on_the_template_are_not_read_as_data() -> None:
    """The file we hand the client opens with `#` notes and comes back edited,
    so the header is not line 1."""
    data = _bytes(
        "setA,B66 2個セット,B66goldS,2",
        header="# 下記にご記入ください\n# 2点セットは同じセットSKUで2行に分けてください\n" + HEADER,
    )

    assert read_faults(data) == []
    assert len(_rows(data)) == 1


def test_a_missing_column_refuses_the_file() -> None:
    faults = read_faults(_bytes("setA,B66goldS,2", header="セットSKU,構成品SKU,数量"))

    assert any("セット商品名" in f.reason for f in faults)


def test_a_blank_required_cell_refuses_the_file() -> None:
    """csv_intake would skip the row; here it is fatal, because a set that
    silently lost a component still fans out to the rest."""
    faults = read_faults(_bytes("setA,B66 2個セット,,2"))

    assert faults
    assert "行目" in faults[0].where


@pytest.mark.parametrize("raw", ["abc", "1.5", "0", "-1"])
def test_a_quantity_that_is_not_a_positive_whole_number_is_refused(raw: str) -> None:
    assert quantity_fault(raw) is not None


@pytest.mark.parametrize("raw", ["2", "2.0", "１"])  # noqa: RUF001
def test_a_quantity_that_means_a_count_of_items_is_accepted(raw: str) -> None:
    """Excel writes 2 as 2.0, and a fullwidth digit typed on a Japanese
    keyboard means the same number. Pinned because it reads like a defect."""
    assert quantity_fault(raw) is None


def test_one_set_under_two_different_names_is_refused() -> None:
    _, faults = group(_rows(_bytes("setA,セットX,B66goldS,1", "setA,セットY,B34silver19cm,1")))

    assert any("別のセット商品名" in f.reason for f in faults)


def test_the_same_component_twice_in_one_set_is_refused() -> None:
    """Two rows would become two bundle_components for one pair, and the UNIQUE
    constraint would refuse the second after staging the rest."""
    _, faults = group(
        _rows(_bytes("setA,B66 2個セット,B66goldS,1", "setA,B66 2個セット,B66goldS,1"))
    )

    assert any("重複" in f.reason for f in faults)


def test_a_set_listing_itself_as_a_component_is_refused() -> None:
    _, faults = group(_rows(_bytes("setA,セットA,setA,1")))

    assert any("セット自身" in f.reason for f in faults)


# ---------- planning against the catalogue ----------


async def test_a_new_set_over_existing_components_is_registerable() -> None:
    result = await plan(
        _FakeSession([B34S, B66S]),  # type: ignore[arg-type]
        _bytes(
            "setA,B34とB66のセット,B34silver19cm,1",
            "setA,B34とB66のセット,B66goldS,1",
        ),
    )

    assert not result.faults, _reasons(result.faults)
    assert [s.set_sku for s in result.ready()] == ["setA"]


def test_a_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    data, faults = read_file(tmp_path / "nope.csv")

    assert data == b""
    assert any("見つかりません" in f.reason for f in faults)


async def test_a_component_that_is_itself_a_set_is_refused() -> None:
    """`resolve_consumption` expands one level only, so the shelf would never
    move: the event would land on a bundle parent that holds no snapshot."""
    result = await plan(
        _FakeSession([B66S, PARENT]),  # type: ignore[arg-type]
        _bytes("setA,セットA,B66goldS,1", "setA,セットA,N108gold42,1"),
    )

    assert any("既にセット商品" in f.reason for f in result.faults)


async def test_a_component_with_no_master_is_refused() -> None:
    result = await plan(
        _FakeSession([B66S]),  # type: ignore[arg-type]
        _bytes("setA,セットA,B66goldS,1", "setA,セットA,NOSUCH,1"),
    )

    assert any("マスタがありません" in f.reason for f in result.faults)


async def test_an_archived_component_is_refused() -> None:
    result = await plan(
        _FakeSession([RETIRED]),  # type: ignore[arg-type]
        _bytes("setA,セットA,B66old,1"),
    )

    assert any("廃止" in f.reason for f in result.faults)


async def test_rerunning_an_already_registered_set_registers_nothing() -> None:
    """Idempotent: re-running after a partial session must not add a second set
    of component rows."""
    result = await plan(
        _FakeSession(
            [("setA", 9, True, None), B34S, B66S],
            components=[(9, 2, 1), (9, 1, 1)],
        ),  # type: ignore[arg-type]
        _bytes(
            "setA,B34とB66のセット,B34silver19cm,1",
            "setA,B34とB66のセット,B66goldS,1",
        ),
    )

    assert not result.faults, _reasons(result.faults)
    assert result.sets[0].already_registered
    assert result.ready() == []


async def test_a_set_already_registered_with_other_components_is_refused() -> None:
    """Changing a live set's contents mid-sale is not something to do silently."""
    result = await plan(
        _FakeSession(
            [("setA", 9, True, None), B34S, B66S],
            components=[(9, 2, 1)],
        ),  # type: ignore[arg-type]
        _bytes("setA,B34とB66のセット,B66goldS,1"),
    )

    assert any("別の構成" in f.reason for f in result.faults)


async def test_a_quantity_change_alone_is_refused() -> None:
    """Same components, different count: still a change to a live set."""
    result = await plan(
        _FakeSession(
            [("setA", 9, True, None), B66S],
            components=[(9, 1, 1)],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2"),
    )

    assert any("別の構成" in f.reason for f in result.faults)


async def test_a_set_sku_already_holding_stock_is_refused() -> None:
    """Becoming a set means this master's own snapshot stops being consulted,
    so linking one with stock on the shelf makes it vanish from every screen."""
    result = await plan(
        _FakeSession(
            [("setA", 9, False, None), B66S],
            stock=[(9, 40)],
        ),  # type: ignore[arg-type]
        _bytes("setA,セットA,B66goldS,1"),
    )

    assert any("自身の在庫が 40" in f.reason for f in result.faults)


async def test_one_bad_row_leaves_the_whole_file_unregisterable() -> None:
    """A partial apply is the dangerous outcome: half a set still fans out, so
    every order under-deducts and nothing on screen says so."""
    result = await plan(
        _FakeSession([B34S, B66S]),  # type: ignore[arg-type]
        _bytes(
            "setA,B34とB66のセット,B34silver19cm,1",
            "setA,B34とB66のセット,B66goldS,1",
            "setB,壊れた行,B66goldS,ゼロ",
        ),
    )

    assert result.faults
    assert result.ready() == []
