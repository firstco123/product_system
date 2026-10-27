"""規格カナ parsing and master matching (docs/29 §5).

Every assertion here is about a table a person will read and approve, so the
tests care as much about WHY a row came out the way it did — exact, by a client
rule, or by dropping a colour — as about which master it names.
"""

from __future__ import annotations

import csv
import glob
from pathlib import Path

import pytest

from app.wholesale_catalogue import (
    GIFT_BAG_CODE,
    GIFT_BAG_KANA,
    MatchKind,
    build_table,
    match_code,
    parse_spec_kana,
    summarise,
    token_candidates,
)

# --------------------------------------------------------------------- parsing


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kana", "token", "colour", "size"),
    [
        ("IT-B40-GD", "B40", "gold", None),
        ("IT-P07-SV", "P07", "silver", None),
        # Size BEFORE colour.
        ("IT-B34-20CM-GD", "B34", "gold", "20cm"),
        # Colour BEFORE size — the same product family, the other order.
        ("IT-R34-SV-US5", "R34", "silver", "us5"),
        # Token alone: docs/29 §5-2 does not list this shape, but IT-N79 is it.
        ("IT-N79", "N79", None, None),
    ],
)
def test_the_five_real_shapes_parse(
    kana: str, token: str, colour: str | None, size: str | None
) -> None:
    spec = parse_spec_kana(kana)

    assert spec is not None
    assert (spec.token, spec.colour, spec.size) == (token, colour, size)


@pytest.mark.unit
def test_segment_order_does_not_change_the_result() -> None:
    """docs/29 §5-2: the order is not fixed, so classification is by pattern."""
    size_first = parse_spec_kana("IT-X1-20CM-GD")
    colour_first = parse_spec_kana("IT-X1-GD-20CM")

    assert size_first is not None and colour_first is not None
    assert (size_first.colour, size_first.size) == ("gold", "20cm")
    assert (colour_first.colour, colour_first.size) == ("gold", "20cm")


@pytest.mark.unit
@pytest.mark.parametrize(
    "kana",
    [
        "",
        "B40-GD",  # no IT prefix
        "IT",  # prefix alone
        GIFT_BAG_KANA,  # a space and mixed case: not a product
        "IT-B34-XX-GD",  # an unreadable segment must not be skipped over
        "IT-B34-GD-SV",  # two colours
        "IT-B34-20CM-22CM",  # two sizes
    ],
)
def test_anything_unreadable_returns_none_rather_than_a_guess(kana: str) -> None:
    """Ignoring a segment would turn IT-B34-XX-GD into IT-B34-GD, which
    resolves — to the wrong master. None sends it to a person instead."""
    assert parse_spec_kana(kana) is None


# ------------------------------------------------------------- the client rules


@pytest.mark.unit
def test_a_b34_with_no_size_is_22cm() -> None:
    """2026-09-17. Our masters carry 19/20/22cm, so without the rule the
    commonest bracelet code matches three ways."""
    spec = parse_spec_kana("IT-B34-GD")

    assert spec is not None
    assert (spec.size, spec.ruled) == ("22cm", True)

    match = match_code("x", "IT-B34-GD", ["B34gold19cm", "B34gold20cm", "B34gold22cm"])
    assert match.kind is MatchKind.RULE
    assert match.sku_code == "B34gold22cm"
    assert "22cm" in match.note
    # The reviewer sees what it was chosen over.
    assert match.candidates == ("B34gold19cm", "B34gold20cm", "B34gold22cm")


@pytest.mark.unit
def test_a_b66_at_19cm_is_size_m() -> None:
    """The size rule yields `M` while master codes are compared lowercased —
    the case gap that left both B66 codes unmatched until it was normalised at
    one place."""
    match = match_code("x", "IT-B66-19CM-GD", ["B66goldM", "B66goldS"])

    assert match.kind is MatchKind.RULE
    assert match.sku_code == "B66goldM"


@pytest.mark.unit
def test_both_b33_sizes_land_on_the_one_master() -> None:
    """2026-09-17: 20cm and 22cm share one master for now. Two wholesale codes
    to one master is correct today and wrong the day they split (docs/29 §9)."""
    sized = match_code("a", "IT-B33-20CM-GD", ["B33gold", "B33silver"])
    unsized = match_code("b", "IT-B33-GD", ["B33gold", "B33silver"])

    assert sized.sku_code == unsized.sku_code == "B33gold"
    assert sized.kind is MatchKind.RULE
    assert unsized.kind is MatchKind.EXACT
    assert "分離した時点" in sized.note


# ------------------------------------------------------------------- the tokens


@pytest.mark.unit
def test_a_token_does_not_claim_a_longer_numbered_one() -> None:
    """`N10` must not take `N103`, nor `B3` take `B34`. Our codes put the token
    first with no separator, so a plain startswith is wrong on exactly the
    products with the most variants."""
    codes = ["N10gold", "N103gold", "N108gold", "B3gold", "B34gold22cm"]

    assert token_candidates("N10", codes) == ["N10gold"]
    assert token_candidates("N103", codes) == ["N103gold"]
    assert token_candidates("B3", codes) == ["B3gold"]


@pytest.mark.unit
def test_the_tail_must_match_exactly_so_a_two_tone_ring_is_not_caught() -> None:
    """`R34goldsilverus5` contains "gold". Only an exact tail keeps the two-tone
    ring, which is not sold wholesale, out of a wholesale mapping."""
    match = match_code("x", "IT-R34-GD-US5", ["R34goldus5", "R34goldsilverus5"])

    assert match.kind is MatchKind.EXACT
    assert match.sku_code == "R34goldus5"


@pytest.mark.unit
def test_a_length_variant_does_not_catch_the_plain_master() -> None:
    """`N108gold42` and `N108gold45` exist alongside `N108gold`. docs/29 §9 warns
    this becomes ambiguous once N108 is split by length; today it is not."""
    match = match_code("x", "IT-N108-GD", ["N108gold", "N108gold42", "N108gold45"])

    assert match.kind is MatchKind.EXACT
    assert match.sku_code == "N108gold"


@pytest.mark.unit
def test_hyphenated_and_bare_master_codes_match_alike() -> None:
    """Our codes are not uniform: N62-gold, N79-, B51silver all occur."""
    assert match_code("x", "IT-N62-GD", ["N62-gold", "N62-silver"]).sku_code == "N62-gold"
    assert match_code("x", "IT-N79", ["N79-"]).sku_code == "N79-"


# ------------------------------------------------------- the colourless products


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kana", "codes", "expected"),
    [
        ("IT-R63-SV-US5", ["R63us5", "R63us7", "R63us9"], "R63us5"),
        ("IT-K01-SV", ["K01"], "K01"),
    ],
)
def test_a_colour_is_dropped_when_no_master_for_the_token_has_one(
    kana: str, codes: list[str], expected: str
) -> None:
    """docs/29 §5-2 names R63: the size is in 属性１名 and there is no colour
    axis. Reported as its own kind so these get read before they are applied."""
    match = match_code("x", kana, codes)

    assert match.kind is MatchKind.COLOURLESS
    assert match.sku_code == expected
    assert "確認してください" in match.note


@pytest.mark.unit
def test_a_colour_is_never_dropped_when_some_master_for_the_token_has_one() -> None:
    """The safety property. If a colour axis exists and the code's colour is not
    in it, dropping the colour would point a silver code at a gold master."""
    match = match_code("x", "IT-X1-SV", ["X1gold"])

    assert match.kind is MatchKind.UNMATCHED
    assert match.sku_code is None
    assert match.candidates == ("X1gold",)


# -------------------------------------------------------------- what cannot pass


@pytest.mark.unit
def test_two_masters_sharing_a_tail_are_reported_not_picked() -> None:
    match = match_code("x", "IT-X1-GD", ["X1gold", "X1-gold"])

    assert match.kind is MatchKind.AMBIGUOUS
    assert match.sku_code is None
    assert set(match.candidates) == {"X1gold", "X1-gold"}
    assert not match.applicable


@pytest.mark.unit
def test_a_token_with_no_master_at_all_is_reported() -> None:
    match = match_code("x", "IT-Z99-GD", ["B34gold22cm"])

    assert match.kind is MatchKind.UNMATCHED
    assert "Z99" in match.note


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "kana"),
    [(GIFT_BAG_CODE, GIFT_BAG_KANA), ("other", GIFT_BAG_KANA), (GIFT_BAG_CODE, "whatever")],
)
def test_the_gift_bag_is_a_non_product_by_code_or_by_kana(code: str, kana: str) -> None:
    """docs/29 §5-4: packaging, registered 在庫管理対象外, and NOT merged with the
    existing H1 / 10005 gift boxes. Recognised either way round so a renamed
    規格カナ cannot turn it back into a stocked product."""
    match = match_code(code, kana, [])

    assert match.kind is MatchKind.NON_PRODUCT
    assert match.sku_code is None
    assert not match.applicable
    assert "在庫管理対象外" in match.note


@pytest.mark.unit
def test_only_exact_and_rule_matches_are_applicable_without_review() -> None:
    exact = match_code("a", "IT-B40-GD", ["B40gold"])
    ruled = match_code("b", "IT-B34-GD", ["B34gold22cm"])
    colourless = match_code("c", "IT-K01-SV", ["K01"])
    unmatched = match_code("d", "IT-Z99-GD", [])

    assert exact.applicable
    assert ruled.applicable
    # A dropped colour is an inference; it waits for a person.
    assert not colourless.applicable
    assert not unmatched.applicable


# ---------------------------------------------------------------- the whole table


@pytest.mark.unit
def test_the_table_has_one_row_per_code_not_per_line() -> None:
    """The fix for an unmatched code is one mapping row. A table with a row per
    order line would bury that."""
    pairs = [("c1", "IT-B40-GD"), ("c1", "IT-B40-GD"), ("c2", "IT-B40-SV")]

    table = build_table(pairs, ["B40gold", "B40silver"])

    assert [row.product_code for row in table] == ["c1", "c2"]


@pytest.mark.unit
def test_the_real_77_codes_resolve_as_docs_29_says() -> None:
    """docs/29 §5-3 counted 76 of 77 resolving, with the gift bag left over.

    Measured here against the real BMS files and the real stocktake sheets,
    both gitignored — so this skips in CI and is the only place the claim is
    checked at all. The counts are pinned so a master rename shows up as a
    failure rather than as a quietly emptier table.
    """
    from app.wholesale_bms import read_bms

    sources = [
        Path("csv_file/phase2/source/受注伝票_20260916123229.csv"),
        Path("csv_file/phase2/source/受領・返品_20260916123318.csv"),
    ]
    sheets = sorted(glob.glob("csv_file/phase2/stocktake/stocktake_*.csv"))
    if not all(path.exists() for path in sources) or not sheets:
        pytest.skip("the BMS files or the stocktake sheets are not in this checkout")

    pairs: set[tuple[str, str]] = set()
    for path in sources:
        reading = read_bms(path.read_bytes())
        assert reading.fatal == []
        pairs.update(
            (line.product_code, line.spec_kana)
            for voucher in reading.vouchers
            for line in voucher.lines
        )

    sku_codes: set[str] = set()
    for sheet in sheets:
        with open(sheet, encoding="utf-8-sig", newline="") as handle:
            for row in list(csv.reader(handle))[1:]:
                if row and row[0].strip() and not row[0].startswith("#"):
                    sku_codes.add(row[0].strip())

    table = build_table(sorted(pairs), sorted(sku_codes))

    assert len(table) == 77
    assert summarise(table) == {
        "exact": 65,
        "rule": 6,
        "colourless": 5,
        "ambiguous": 0,
        "unmatched": 0,
        "non_product": 1,
    }
    # 76 products resolve; the 77th is the gift bag.
    assert len([row for row in table if row.sku_code is not None]) == 76
    # No master may be claimed by two codes EXCEPT B33, where the client asked
    # for one shared stock (docs/29 §1).
    claimed: dict[str, list[str]] = {}
    for row in table:
        if row.sku_code is not None:
            claimed.setdefault(row.sku_code, []).append(row.spec_kana)
    shared = {code: kanas for code, kanas in claimed.items() if len(kanas) > 1}
    assert sorted(shared) == ["B33gold", "B33silver"], shared
