"""ギフトセット registration — one sellable SKU consuming several others.

The Christmas 2026 case: B66 sold as a pair, B34 paired with B66. One order
line, two items off the shelf.

Three things make this worth testing hard. A set whose component is itself a set
would move no stock at all, because `resolve_consumption` expands one level and
stops — so that has to be refused rather than quietly flattened. A partial apply
is worse than no apply: half a set still fans out, so every order under-deducts
and no screen says so, which is why one bad row refuses the file. And the
Rakuten mapping GRAIN is the (商品管理番号, SKU管理番号) pair: written at any
other grain the order resolves to nothing and the availability push silently
drops the target, while the command reports success.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.cli.create_gift_sets import (
    SPEC,
    GiftSet,
    group,
    plan,
    quantity_fault,
    read_faults,
    read_file,
    state_of,
)
from app.csv_intake import iter_rows
from app.services.mapping import ChannelTarget

pytestmark = pytest.mark.unit

HEADER = "セットSKU,セット商品名,構成品SKU,数量"
#: The same file with the optional channel columns the mappings come from. Both
#: headers are exercised: files written before the columns existed must still
#: import. Rakuten takes TWO columns, 商品管理番号 then SKU管理番号, because that
#: pair is the address `channel_sku_mappings` is keyed on.
HEADER_CHANNELS = HEADER + ",ShopifySKU,楽天商品管理番号,楽天SKU"
#: The fullwidth number sign, by code point rather than written as itself:
#: RUF001 is on for this project and `ruff format` rewrites a \uXXXX escape
#: back into the character. It is what a Japanese keyboard in 全角 mode
#: produces for `#`.
FULLWIDTH_HASH = chr(0xFF03)


def _bytes(*lines: str, header: str = HEADER) -> bytes:
    return "\n".join((header, *lines)).encode("utf-8-sig")


def _rows(data: bytes) -> list[dict[str, str]]:
    return list(iter_rows(data, SPEC))


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def tuples(self) -> _Result:
        """`plan` reads the conflicting-master names through `.tuples()`, for
        mypy's sake. Same rows either way."""
        return self


class _FakeSession:
    """Answers `plan`'s five reads IN ORDER: masters, snapshots, components,
    channel mappings, then the sku_codes of every master the file does not name
    but a message has to: the holders of a conflicting mapping AND the
    components a set is already registered with. Order is the whole contract —
    adding a read to `plan` without adding its answer here silently feeds the
    next one's rows to it, which is what
    `test_plan_reads_exactly_what_this_fake_answers` is here to catch."""

    #: How many reads `plan` is expected to make. Pinned, not inferred.
    READS = 5

    def __init__(
        self,
        masters: list[tuple[str, int, bool, bool, Any]],
        stock: list[tuple[int, int]] | None = None,
        components: list[tuple[int, int, int]] | None = None,
        mappings: list[tuple[str, str, str | None, int, bool]] | None = None,
        conflict_names: list[tuple[int, str]] | None = None,
    ) -> None:
        self._answers: list[list[Any]] = [
            masters,
            stock or [],
            components or [],
            mappings or [],
            conflict_names or [],
        ]
        self.reads = 0

    async def execute(self, _stmt: Any) -> _Result:
        self.reads += 1
        return _Result(self._answers.pop(0) if self._answers else [])


# (sku_code, id, is_bundle, is_stock_managed, archived_at)
B66S = ("B66goldS", 1, False, True, None)
B34S = ("B34silver19cm", 2, False, True, None)
PARENT = ("N108gold42", 3, True, True, None)
RETIRED = ("B66old", 4, False, True, "2026-01-01")
#: 在庫管理対象外 — a gift bag or a box, the most likely extra line on a
#: Christmas set and the one `resolve_consumption` would fan out to anyway.
GIFTBAG = ("GSbag", 5, False, False, None)


def _shopify(channel_sku: str, master_id: int, *, active: bool = True) -> tuple[Any, ...]:
    """One `channel_sku_mappings` row as `plan` reads it. Shopify's variant key
    is NULL for ever — the Shopify adapter never sets one."""
    return ("shopify", channel_sku, None, master_id, active)


def _rakuten(
    page: str, variant_key: str | None, master_id: int, *, active: bool = True
) -> tuple[Any, ...]:
    """The Rakuten pair: `channel_sku` is 商品管理番号, the key is SKU管理番号."""
    return ("rakuten", page, variant_key, master_id, active)


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


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1e3", "1_0"])
def test_a_quantity_float_would_have_accepted_or_choked_on_is_refused(raw: str) -> None:
    """The five spellings the old `float()` probe got wrong.

    'nan' raised an uncaught ValueError and 'inf' an OverflowError, turning a
    plan-then-apply refusal into a traceback that said nothing about any other
    row. '1e3' passed as 1000 and '1_0' as 10 — and '1_0' is the one that bites:
    the client writes underscores in the 楽天SKU column (202612b_9), so a file
    saved one column out of line puts an underscored token where 数量 is read.
    """
    assert quantity_fault(raw) is not None


def test_a_row_that_lost_its_trailing_empty_fields_is_refused_not_dropped() -> None:
    """`csv_intake` drops a row no wider than the widest resolved column, with
    no row issue and no count. Adding the trailing channel columns made 楽天SKU
    the widest index, so a row whose empty trailing cells had been trimmed —
    Excel and several CSV writers trim them — used to vanish entirely: the set
    registered with a component missing and nothing on screen said so.

    Measured before the guard: this exact file yielded one part and zero faults.
    """
    data = _bytes(
        "setA,セットA,B66goldS,1,setA,,",
        "setA,セットA,B34silver19cm,1,setA",
        header=HEADER_CHANNELS,
    )

    faults = read_faults(data)

    assert any("列が足りません" in f.reason for f in faults), _reasons(faults)
    assert any("3行目" in f.where for f in faults), [f.where for f in faults]


def test_a_full_width_row_is_not_reported_as_ragged() -> None:
    """The guard refuses only what `csv_intake` would have dropped: a row that
    carries every resolved column is fine, trailing blanks and all."""
    data = _bytes("setA,セットA,B66goldS,1,,,", header=HEADER_CHANNELS)

    assert read_faults(data) == []


def test_a_memo_line_below_the_header_does_not_refuse_the_file() -> None:
    """`csv_intake` applies comment_prefix only ABOVE the header — filtering
    `#` globally would drop a real data row — and then drops a post-header `#`
    line in silence as a short row. The ragged guard uses the same width test,
    so a trailing 「# 以上です」 refused the whole file: 「3行目 列が足りません:
    7列必要ですが 1列しかありません」. The template the client edits and returns
    is exactly where that line appears.
    """
    data = _bytes(
        "setA,セットA,B66goldS,1,setA,,",
        "# 以上です",
        header=HEADER_CHANNELS,
    )

    assert read_faults(data) == []
    assert len(_rows(data)) == 1


def test_an_excel_padded_memo_line_is_not_reported_as_an_empty_column() -> None:
    """Excel pads every row out to the used-range width when it saves, so the
    memo comes back as seven fields and `csv_intake.inspect` — which applies
    comment_prefix only above the header — reported it as a data row:
    「3行目 セット商品名が空です」, telling the operator a column is empty on a
    line the template invited them to write.
    """
    data = _bytes(
        "setA,セットA,B66goldS,1,setA,gift2026a,202612a-1",
        "# 以上です,,,,,,",
        header=HEADER_CHANNELS,
    )

    assert read_faults(data) == [], _reasons(read_faults(data))
    assert len(_rows(data)) == 1


@pytest.mark.parametrize("memo", ["{h} 以上です", "{h} 以上です,,,,,,", "{h}"])
def test_a_fullwidth_hash_memo_line_is_ignored_like_an_ascii_one(memo: str) -> None:
    """A Japanese keyboard in 全角 mode produces the fullwidth number sign, and
    the operator who typed it meant a comment — the same reading
    `quantity_fault` already gives a fullwidth digit. Untrimmed such a line was
    refused as 「列が足りません」 and padded as 「セット商品名が空です」.
    """
    data = _bytes(
        "setA,セットA,B66goldS,1,setA,gift2026a,202612a-1",
        memo.format(h=FULLWIDTH_HASH),
        header=HEADER_CHANNELS,
    )

    assert read_faults(data) == [], _reasons(read_faults(data))
    assert len(_rows(data)) == 1


def test_a_bare_hash_line_is_ignored() -> None:
    """A spacer the client leaves behind after deleting the guidance text."""
    data = _bytes("setA,セットA,B66goldS,1,setA,,", "#", header=HEADER_CHANNELS)

    assert read_faults(data) == [], _reasons(read_faults(data))
    assert len(_rows(data)) == 1


def test_a_data_row_wearing_a_hash_is_refused_not_ignored() -> None:
    """The hole that ignoring every `#` line below the header would open.

    "#S1,セット,B,1" is a data row someone commented out, not prose: it is too
    short for the seven-column header, so `csv_intake` drops it in silence and
    the memo skip would drop it too — the set would register one component
    short, every sale would under-deduct and no screen would say so. Measured
    before the count test: this exact file yielded no fault at all.
    """
    data = _bytes(
        "setA,セットA,B66goldS,1,setA,gift2026a,202612a-1",
        "#S1,セット,B,1",
        header=HEADER_CHANNELS,
    )

    faults = read_faults(data)

    assert any("コメント行にデータ" in f.reason for f in faults), _reasons(faults)
    assert any("3行目" in f.where for f in faults), [f.where for f in faults]


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


async def test_a_component_that_is_not_stock_managed_is_refused() -> None:
    """`resolve_consumption` tests 在庫管理対象外 on the PARENT only, then fans
    out and writes one event per component without testing it again — so a gift
    bag or a box in a set would collect real stock events against a master that
    holds no inventory and drive its snapshot negative. None of the 14
    components in the Christmas file is affected; the next file is the one that
    will carry a gift bag."""
    result = await plan(
        _FakeSession([B66S, GIFTBAG]),  # type: ignore[arg-type]
        _bytes("setA,セットA,B66goldS,1", "setA,セットA,GSbag,1"),
    )

    assert any("在庫管理対象外" in f.reason and "GSbag" in f.reason for f in result.faults), (
        _reasons(result.faults)
    )


async def test_rerunning_an_already_registered_set_registers_nothing() -> None:
    """Idempotent: re-running after a partial session must not add a second set
    of component rows."""
    result = await plan(
        _FakeSession(
            [("setA", 9, True, True, None), B34S, B66S],
            components=[(9, 2, 1), (9, 1, 1)],
        ),  # type: ignore[arg-type]
        _bytes(
            "setA,B34とB66のセット,B34silver19cm,1",
            "setA,B34とB66のセット,B66goldS,1",
        ),
    )

    assert not result.faults, _reasons(result.faults)
    assert result.sets[0].components_registered
    assert result.ready() == []


async def test_a_set_already_registered_with_other_components_is_refused() -> None:
    """Changing a live set's contents mid-sale is not something to do silently."""
    result = await plan(
        _FakeSession(
            [("setA", 9, True, True, None), B34S, B66S],
            components=[(9, 2, 1)],
        ),  # type: ignore[arg-type]
        _bytes("setA,B34とB66のセット,B66goldS,1"),
    )

    assert any("別の構成" in f.reason for f in result.faults)


async def test_a_set_sku_that_is_not_stock_managed_is_refused() -> None:
    """`resolve_consumption` tests is_stock_managed on the PARENT first and
    returns [] before it ever reads bundle_components, so this set would deduct
    nothing on any sale however correct its components are — and
    `BundlePushService.all_bundle_ids` filters is_stock_managed.is_(True), so
    its availability would never be pushed either. An operator reasoning
    「セット自身は在庫を持たない」 sets exactly this flag, and the shop already uses
    it for the wholesale gift bag."""
    result = await plan(
        _FakeSession([("setA", 9, True, False, None), B66S]),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2"),
    )

    assert any("在庫管理対象外" in f.reason for f in result.faults), _reasons(result.faults)


async def test_the_other_components_message_names_what_the_csv_never_mentions() -> None:
    """「既に別の構成で登録されています」 filtered its component list through a
    `code_of` built from the FILE, so an existing component the CSV does not
    name vanished: with one such component the message printed 「現在: 」 and
    nothing after the colon, and here it printed 「現在: B66goldS」 — naming the
    one component that is the SAME as the file's, so the message asserted a
    difference and then hid it. The 数量 is included for the same reason: x9
    against the file's x2 is the difference."""
    result = await plan(
        _FakeSession(
            [("setA", 9, True, True, None), B66S],
            components=[(9, 55, 1), (9, 1, 9)],
            conflict_names=[(55, "GSbox")],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2"),
    )

    assert any("現在: B66goldS x9, GSbox x1" in f.reason for f in result.faults), _reasons(
        result.faults
    )


async def test_an_unresolvable_component_falls_back_to_its_id() -> None:
    """The id is the fallback, not the first choice — the same way the
    channel-conflict message does it. A component whose master row has gone is
    still better named by its id than dropped from the list."""
    result = await plan(
        _FakeSession(
            [("setA", 9, True, True, None), B66S],
            components=[(9, 55, 1)],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2"),
    )

    assert any("現在: master_sku_id 55 x1" in f.reason for f in result.faults), _reasons(
        result.faults
    )


async def test_a_quantity_change_alone_is_refused() -> None:
    """Same components, different count: still a change to a live set."""
    result = await plan(
        _FakeSession(
            [("setA", 9, True, True, None), B66S],
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
            [("setA", 9, False, True, None), B66S],
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


# ---------- channel SKUs -> channel_sku_mappings ----------
#
# The set master's own sku_code is the Shopify SKU for the Christmas sets, which
# is exactly why a blank cell must NOT be filled in from it: the first
# Rakuten-only set has a sku_code that is a Rakuten SKU, and a default would map
# it onto Shopify under a code Shopify has never heard of.


async def test_both_channel_skus_become_mappings_to_write() -> None:
    """And at the grain each channel is addressed by: Shopify is the SKU with no
    variant key, Rakuten is the (商品管理番号, SKU管理番号) pair."""
    result = await plan(
        _FakeSession([B66S]),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,setA,gift2026a,202612a-1", header=HEADER_CHANNELS),
    )

    assert not result.faults, _reasons(result.faults)
    assert result.sets[0].channel_skus == {
        "shopify": ChannelTarget(channel_sku="setA"),
        "rakuten": ChannelTarget(channel_sku="gift2026a", variant_key="202612a-1"),
    }
    assert result.sets[0].pending_channels == ["shopify", "rakuten"]


async def test_a_rakuten_address_never_has_a_null_variant_key() -> None:
    """The row this CLI is forbidden to create. A page-level rakuten row is a
    WILDCARD: `resolve_master_sku_id`'s second step hands it every unmapped SKU
    on that 商品管理番号, so one gift set would absorb the sales of everything
    else sold from the same page."""
    result = await plan(
        _FakeSession([B66S]),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,setA,gift2026a,202612a-1", header=HEADER_CHANNELS),
    )

    rakuten = result.sets[0].channel_skus["rakuten"]
    assert rakuten.variant_key is not None
    assert rakuten.channel_sku == "gift2026a"


def test_a_rakuten_sku_without_its_page_is_refused() -> None:
    """A 楽天SKU alone is not an address: 132 SKU管理番号 are reused across
    different products in this shop. The order would park in pending_mapping and
    `push_safety.screen_targets` would drop the target, while the command
    printed 登録しました."""
    _, faults = group(
        _rows(_bytes("setA,セットA,B66goldS,1,setA,,202612a-1", header=HEADER_CHANNELS))
    )

    assert any("楽天商品管理番号" in f.reason and "空です" in f.reason for f in faults), _reasons(
        faults
    )


def test_a_rakuten_page_without_its_sku_is_refused() -> None:
    """The other half, and the worse one: see the WILDCARD test above."""
    _, faults = group(
        _rows(_bytes("setA,セットA,B66goldS,1,setA,gift2026a,", header=HEADER_CHANNELS))
    )

    assert any("楽天SKU" in f.reason and "空です" in f.reason for f in faults), _reasons(faults)


def test_a_half_pair_on_a_later_row_of_the_set_is_refused() -> None:
    """The half-pair check ran on a set's FIRST row only. With both Rakuten
    cells blank there and 楽天商品管理番号 filled in on a later row,
    `_row_channels` dropped the half pair from both rows' `channels`, so the
    disagreement check compared EQUAL, the reason was thrown away, and the file
    applied with no fault at all — the operator's value silently discarded.

    Measured before the fix: 0 faults, channel_skus == {'shopify': ('setA',
    None)}.
    """
    _, faults = group(
        _rows(
            _bytes(
                "setA,セットA,B66goldS,1,setA,,",
                "setA,セットA,B34silver19cm,1,setA,gift2026a,",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert any("楽天SKU" in f.reason and "空です" in f.reason for f in faults), _reasons(faults)


def test_a_half_pair_on_a_later_row_is_refused_the_other_way_round_too() -> None:
    """The mirror case, which behaved the same way: a 楽天SKU with no page is an
    address Rakuten never asks about."""
    _, faults = group(
        _rows(
            _bytes(
                "setA,セットA,B66goldS,1,setA,,",
                "setA,セットA,B34silver19cm,1,setA,,202612a-1",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert any("楽天商品管理番号" in f.reason and "空です" in f.reason for f in faults), _reasons(
        faults
    )


def test_one_half_pair_repeated_on_every_row_is_reported_once() -> None:
    """Checking every row must not make a four-part set say the same thing four
    times: the dedupe is by reason, so one mistake reads as one mistake."""
    _, faults = group(
        _rows(
            _bytes(
                "setA,セットA,B66goldS,1,setA,gift2026a,",
                "setA,セットA,B34silver19cm,1,setA,gift2026a,",
                "setA,セットA,B66silverS,1,setA,gift2026a,",
                "setA,セットA,B66goldM,1,setA,gift2026a,",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert len([f for f in faults if "空です" in f.reason]) == 1, _reasons(faults)


def test_one_disagreement_across_four_rows_is_reported_once() -> None:
    """The disagreement is deduped per set like the half pair, because it is
    one mistake too.

    A set whose FIRST row leaves the Rakuten cells blank disagrees with each of
    the other three rows, and one fault per row made that read as four. On the
    real 48-set file an operator who fills the channel columns on the component
    rows but not on each set's first row got roughly 40 repeated lines, burying
    whatever else was wrong.
    """
    _, faults = group(
        _rows(
            _bytes(
                "setA,セットA,B66goldS,1,setA,,",
                "setA,セットA,B34silver19cm,1,setA,gift2026a,202612a-1",
                "setA,セットA,B66silverS,1,setA,gift2026a,202612a-1",
                "setA,セットA,B66goldM,1,setA,gift2026a,202612a-1",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert len([f for f in faults if "一致していません" in f.reason]) == 1, _reasons(faults)


def test_the_disagreement_names_the_cell_that_actually_differs() -> None:
    """Named from the RAW cells, so the operator is pointed at 楽天商品管理番号
    when that is the column that differs — COLUMN_OF_CHANNEL would always have
    said 楽天SKU."""
    _, faults = group(
        _rows(
            _bytes(
                "setA,セットA,B66goldS,1,setA,gift2026a,202612a-1",
                "setA,セットA,B34silver19cm,1,setA,gift2026b,202612a-1",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert any("楽天商品管理番号が一致していません" in f.reason for f in faults), _reasons(faults)


async def test_a_blank_channel_sku_is_not_filled_in_from_the_set_sku() -> None:
    """Blank means 「このチャネルでは売らない」. A default would make a mistyped
    header read as a file full of successful mappings."""
    result = await plan(
        _FakeSession([B66S]),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,,gift2026a,202612a-1", header=HEADER_CHANNELS),
    )

    assert not result.faults, _reasons(result.faults)
    assert result.sets[0].channel_skus == {
        "rakuten": ChannelTarget(channel_sku="gift2026a", variant_key="202612a-1")
    }
    assert result.sets[0].pending_channels == ["rakuten"]


async def test_a_file_without_the_channel_columns_still_imports() -> None:
    """`iter_rows` yields no key at all for an absent optional column, so the
    columns are read with `.get`: the pre-mapping files must keep working."""
    result = await plan(
        _FakeSession([B66S]),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2"),
    )

    assert not result.faults, _reasons(result.faults)
    assert result.sets[0].channel_skus == {}
    assert result.sets[0].pending_channels == []


def test_two_rows_of_one_set_disagreeing_on_a_channel_sku_is_refused() -> None:
    _, faults = group(
        _rows(
            _bytes(
                "setA,セットA,B66goldS,1,setA,gift2026a,202612a-1",
                "setA,セットA,B34silver19cm,1,setA,gift2026a,202612a-9",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert any("楽天SKU" in f.reason and "一致していません" in f.reason for f in faults)


def test_a_channel_sku_on_only_some_rows_of_one_set_is_refused() -> None:
    """The channel columns describe the SET, so a half-filled column is a
    mistake, not two different answers."""
    _, faults = group(
        _rows(
            _bytes(
                "setA,セットA,B66goldS,1,setA,,",
                "setA,セットA,B34silver19cm,1,setA,gift2026a,202612a-1",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert any("一致していません" in f.reason for f in faults)


def test_one_channel_address_under_two_different_sets_is_refused() -> None:
    """Whichever row landed second would be refused by uq_channel_sku_mapping
    after the rest of the file had been staged."""
    _, faults = group(
        _rows(
            _bytes(
                "setA,セットA,B66goldS,1,setA,gift2026a,202612a-1",
                "setB,セットB,B34silver19cm,1,setB,gift2026a,202612a-1",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert any("重複" in f.reason and "202612a-1" in f.reason for f in faults)


def test_two_sets_on_one_rakuten_page_under_different_skus_are_not_a_duplicate() -> None:
    """The within-file check keys on the TRIPLE, like the unique constraint.
    Keyed on the page alone it would refuse the real file outright: the
    Christmas correspondence table puts 46 Rakuten SKUs across 4 pages."""
    _, faults = group(
        _rows(
            _bytes(
                "setA,セットA,B66goldS,1,setA,gift2026a,202612a-1",
                "setB,セットB,B34silver19cm,1,setB,gift2026a,202612a-2",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert not faults, _reasons(faults)


def test_a_channel_sku_too_long_for_the_column_is_refused() -> None:
    """Checked here, where the message can name the set, rather than letting the
    database refuse the insert after the file has been staged."""
    _, faults = group(
        _rows(
            _bytes(
                f"setA,セットA,B66goldS,1,{'x' * 129},,",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert any("ShopifySKU" in f.reason and "128文字" in f.reason for f in faults)


def test_a_rakuten_sku_management_number_too_long_for_the_column_is_refused() -> None:
    """The variant key is String(128) too, and it is the half that used to be
    unchecked because it was never written."""
    _, faults = group(
        _rows(
            _bytes(
                f"setA,セットA,B66goldS,1,,gift2026a,{'x' * 129}",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert any("楽天SKU" in f.reason and "128文字" in f.reason for f in faults), _reasons(faults)


def test_a_rakuten_page_too_long_for_the_column_names_the_page_column() -> None:
    """The width fault reported COLUMN_OF_CHANNEL, which for Rakuten is the LAST
    column — so a 130-character 楽天商品管理番号 beside a 10-character 楽天SKU was
    refused with 「楽天SKUが128文字を超えています」, pointing the operator at a cell
    that is plainly 10 characters long."""
    _, faults = group(
        _rows(
            _bytes(
                f"setA,セットA,B66goldS,1,,{'x' * 130},202612a-1",
                header=HEADER_CHANNELS,
            )
        )
    )

    assert any("楽天商品管理番号" in f.reason and "128文字" in f.reason for f in faults), _reasons(
        faults
    )
    assert not any("楽天SKU" in f.reason for f in faults), _reasons(faults)


async def test_a_channel_sku_already_pointing_at_another_product_is_refused() -> None:
    result = await plan(
        _FakeSession(
            [B66S],
            mappings=[_shopify("setA", 77)],
            conflict_names=[(77, "B99silver")],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,setA,,", header=HEADER_CHANNELS),
    )

    assert any("別の商品に紐づいています" in f.reason for f in result.faults)


async def test_the_conflict_message_names_the_sku_holding_the_address() -> None:
    """`code_of` knows only the masters the CSV names, and an address held by a
    master the file never mentions is the NORMAL case — so the refusal used to
    print 「現在: master_sku_id 77」, an internal id no operator screen can look
    up."""
    result = await plan(
        _FakeSession(
            [B66S],
            mappings=[_shopify("setA", 77)],
            conflict_names=[(77, "B99silver")],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,setA,,", header=HEADER_CHANNELS),
    )

    assert any("現在: B99silver" in f.reason for f in result.faults), _reasons(result.faults)
    assert not any("master_sku_id" in f.reason for f in result.faults)


async def test_a_rakuten_sku_on_a_shared_page_held_by_another_product_is_refused() -> None:
    """Two sets may share a 商品管理番号, but not a (page, SKU) pair."""
    result = await plan(
        _FakeSession(
            [B66S],
            mappings=[_rakuten("gift2026a", "202612a-1", 77)],
            conflict_names=[(77, "B99silver")],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,,gift2026a,202612a-1", header=HEADER_CHANNELS),
    )

    assert any("gift2026a/202612a-1" in f.reason for f in result.faults), _reasons(result.faults)


async def test_a_row_on_the_same_rakuten_page_under_another_sku_is_not_a_conflict() -> None:
    """Pinning the whole triple is the point. Keyed on (channel, channel_sku)
    the page's OTHER products would each read as a claim on this set's address,
    and the real file — 46 SKUs over 4 pages — could never be applied."""
    result = await plan(
        _FakeSession(
            [B66S],
            mappings=[_rakuten("gift2026a", "202612a-9", 77)],
            conflict_names=[(77, "B99silver")],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,,gift2026a,202612a-1", header=HEADER_CHANNELS),
    )

    assert not result.faults, _reasons(result.faults)
    assert result.sets[0].pending_channels == ["rakuten"]


async def test_a_mapping_already_pointing_at_this_set_is_not_a_fault() -> None:
    """Idempotent, the same way the components check is."""
    result = await plan(
        _FakeSession(
            [("setA", 9, True, True, None), B66S],
            components=[(9, 1, 2)],
            mappings=[_shopify("setA", 9)],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,setA,,", header=HEADER_CHANNELS),
    )

    assert not result.faults, _reasons(result.faults)
    assert result.sets[0].mapped_channels == ["shopify"]
    assert result.sets[0].pending_channels == []
    assert result.ready() == []


async def test_an_inactive_mapping_on_this_set_is_work_not_a_re_run() -> None:
    """The row exists, points here, and resolves nothing: `resolve_master_sku_id`
    filters `is_active.is_(True)`. Ignoring the flag reported it 登録済み, wrote
    nothing, and left the set unresolvable with no re-run able to repair it —
    and an INSERT cannot repair it either, because the row holds the unique key.
    """
    result = await plan(
        _FakeSession(
            [("setA", 9, True, True, None), B66S],
            components=[(9, 1, 2)],
            mappings=[_shopify("setA", 9, active=False)],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,setA,,", header=HEADER_CHANNELS),
    )

    assert not result.faults, _reasons(result.faults)
    gift = result.sets[0]
    assert gift.reactivate_channels == ["shopify"]
    assert gift.pending_channels == []
    assert gift.mapped_channels == []
    assert result.ready() == [gift]
    # Named apart from 登録, because it is a different repair on a different row.
    assert state_of(gift, applied=True) == "ShopifySKU を再有効化しました"


async def test_an_inactive_mapping_on_another_product_is_still_a_fault() -> None:
    """It no longer resolves, but it still occupies `uq_channel_sku_mapping`, so
    there is no room to insert ours beside it."""
    result = await plan(
        _FakeSession(
            [B66S],
            mappings=[_shopify("setA", 77, active=False)],
            conflict_names=[(77, "B99silver")],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,setA,,", header=HEADER_CHANNELS),
    )

    assert any("別の商品に紐づいています" in f.reason for f in result.faults)
    assert any("B99silver" in f.reason for f in result.faults)


async def test_a_set_whose_components_are_there_but_whose_mapping_is_not_has_work() -> None:
    """The state a session interrupted between two files leaves behind. Dropping
    the set because its components match is what made the missing mapping
    unreachable for ever: no re-run would ever add it."""
    result = await plan(
        _FakeSession(
            [("setA", 9, True, True, None), B66S],
            components=[(9, 1, 2)],
        ),  # type: ignore[arg-type]
        _bytes("setA,B66 2個セット,B66goldS,2,setA,gift2026a,202612a-1", header=HEADER_CHANNELS),
    )

    assert not result.faults, _reasons(result.faults)
    gift = result.sets[0]
    assert gift.components_registered
    assert gift.pending_channels == ["shopify", "rakuten"]
    assert result.ready() == [gift]
    # The printed line has to say which piece is being written, or the operator
    # reads 登録しました over a set that only gained a mapping.
    assert state_of(gift, applied=True) == "ShopifySKU + 楽天SKU を登録しました"


async def test_plan_reads_exactly_what_this_fake_answers() -> None:
    """`_FakeSession` answers by POSITION, so a read added to `plan` without an
    answer added here would be served the next read's rows and every test above
    would keep passing while asserting nothing it claims."""
    session = _FakeSession([B66S])  # type: ignore[arg-type]

    await plan(
        session,
        _bytes("setA,B66 2個セット,B66goldS,2,setA,gift2026a,202612a-1", header=HEADER_CHANNELS),
    )

    assert session.reads == _FakeSession.READS


def test_a_set_gaining_one_mapping_and_reviving_another_says_both() -> None:
    """The real shape of a second pass over a half-finished session: the Shopify
    row still to write, the Rakuten row only switched off. One verb for both
    would leave the operator unable to tell which row was touched how."""
    gift = GiftSet(
        set_sku="setA",
        name="セットA",
        components_registered=False,
        pending_channels=["shopify"],
        reactivate_channels=["rakuten"],
    )

    assert (
        state_of(gift, applied=False)
        == "構成品 + ShopifySKU を登録します / 楽天SKU を再有効化します"
    )
