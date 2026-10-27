"""規格カナ -> our master SKU, for building the wholesale correspondence table.

docs/29 §5. This is a **tool for producing a table a human then reviews**, not a
runtime resolver. Once the table is applied as `channel_sku_mappings` rows, the
import resolves by 商品コード（発注用）directly and never comes back here. Keeping
that boundary matters: the rules below are inference, and inference belongs
where a person can disagree with it, not on the path that moves stock.

The shapes, from all 77 codes the carrier sent (2026-09-16):

    IT-B34-GD           token, colour
    IT-B34-20CM-GD      token, size, colour
    IT-R34-SV-US5       token, colour, size      <- the opposite order
    IT-N79              token alone, no colour
    IT-Gift bag         not a product at all (§5-4)

docs/29 §5-2 lists only the middle three, so segments are classified by PATTERN
and never by position — the last two shapes are why.

Matching works on the tail of our own `sku_code`, as §5-2 instructs, because the
size lives in a different master attribute for different products and no single
attribute column can be filtered on. Our codes are not uniform either:

    B34gold22cm    N62-gold    N79-    R63us5    K01    B66goldM

so the comparison strips the token, lowercases, and drops hyphens before
comparing. That is enough to separate the cases that genuinely collide:
`R34goldus5` and `R34goldsilverus5` both contain "gold", and only an exact tail
match keeps the two-tone ring out of a wholesale mapping. The same exactness is
what resolves `N108gold` without catching `N108gold42`.

Three product-specific rules the client settled (docs/29 §1), applied here
rather than in the matcher so each is named and dated at one place:

* `B34` with no size is 22cm. Our masters carry 19/20/22cm, so without this the
  commonest bracelet code is ambiguous three ways.
* `B66` at `19CM` is size M.
* `B33` is one master for 20cm and 22cm "for now", so BOTH `IT-B33-GD` and
  `IT-B33-20CM-GD` land on `B33gold`. Two wholesale codes to one master is
  correct today and wrong the day they split — docs/29 §9.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: Only two colours appear across all 77 codes.
_COLOURS = {"GD": "gold", "SV": "silver"}

#: `20CM`, `19CM` ... and `US5`, `US7`, `US9`. Nothing else occurs.
_SIZE_CM = re.compile(r"^(\d+)CM$", re.IGNORECASE)
_SIZE_US = re.compile(r"^US(\d+)$", re.IGNORECASE)

#: The carrier's prefix on every code, product or not.
_PREFIX = "IT"

#: 商品コード 2700002475148. Packaging, not a product: docs/29 §5-4 registers it
#: with `is_stock_managed=false` / `non_inventory_kind=packaging`, and NOT merged
#: with the existing H1 / 10005 gift boxes, which are a different thing.
GIFT_BAG_KANA = "IT-Gift bag"
GIFT_BAG_CODE = "2700002475148"


class MatchKind(StrEnum):
    """How a code was matched, which is how much review it needs."""

    #: The tail matched exactly, once. Nothing was assumed.
    EXACT = "exact"
    #: Matched after a client-agreed substitution (docs/29 §1) — B34's implied
    #: 22cm, B66's 19CM meaning M, B33's two sizes sharing one master.
    RULE = "rule"
    #: Matched only after dropping the colour, because no master for this token
    #: carries one. Correct for R63/K01/K02, whose colour axis we do not model —
    #: and the one kind of match that would silently point a silver code at a
    #: gold master if a colour axis were ever added. Read these before applying.
    COLOURLESS = "colourless"
    #: Several masters matched. Never applied; the table says which.
    AMBIGUOUS = "ambiguous"
    #: No master matched.
    UNMATCHED = "unmatched"
    #: Not a product (§5-4).
    NON_PRODUCT = "non_product"


@dataclass(frozen=True, slots=True)
class SpecKana:
    """One parsed 規格カナ."""

    raw: str
    token: str
    colour: str | None
    size: str | None
    #: True when a docs/29 §1 rule changed `colour`/`size` from what the code
    #: literally said.
    ruled: bool = False

    @property
    def expected_tail(self) -> str:
        """What our `sku_code` should carry after the token.

        Lowercased here because `_tail` lowercases the master code it is compared
        against, and the comparison has to be case-blind at ONE place. It was not:
        B66's size rule yields `M`, our master is `B66goldM`, and `goldM` against
        a lowercased `goldm` left the only two B66 codes unmatched.
        """
        return f"{self.colour or ''}{self.size or ''}".lower()

    @property
    def tail_without_colour(self) -> str:
        return (self.size or "").lower()


@dataclass(frozen=True, slots=True)
class CodeMatch:
    """One row of the correspondence table, before anyone reviews it."""

    product_code: str
    spec_kana: str
    kind: MatchKind
    sku_code: str | None
    #: Every master considered for this token. Carried even on a clean match, so
    #: a reviewer can see what it was chosen over rather than trusting the pick.
    candidates: tuple[str, ...] = ()
    note: str = ""

    @property
    def applicable(self) -> bool:
        """Safe to write as a mapping without a human saying so first?"""
        return self.kind in (MatchKind.EXACT, MatchKind.RULE) and self.sku_code is not None


def parse_spec_kana(raw: str) -> SpecKana | None:
    """Parse one 規格カナ, or None when it is not a product code.

    Returns None for `IT-Gift bag` and for anything else that does not look like
    `IT-<token>[-<colour>][-<size>]`. None is "not a product here", never "close
    enough" — a code this cannot read must reach a person, because the fallback
    would be a mapping onto the wrong master.
    """
    text = raw.strip()
    if not text:
        return None
    segments = text.split("-")
    if len(segments) < 2 or segments[0].strip().upper() != _PREFIX:
        return None
    token = segments[1].strip()
    # `Gift bag` holds a space and mixes case; a real token is letter-then-digits.
    if not re.fullmatch(r"[A-Za-z]+\d+", token):
        return None

    colour: str | None = None
    size: str | None = None
    for segment in segments[2:]:
        part = segment.strip()
        if not part:
            continue
        if part.upper() in _COLOURS:
            if colour is not None:
                return None
            colour = _COLOURS[part.upper()]
            continue
        cm = _SIZE_CM.match(part)
        us = _SIZE_US.match(part)
        if cm is not None:
            if size is not None:
                return None
            size = f"{int(cm.group(1))}cm"
            continue
        if us is not None:
            if size is not None:
                return None
            size = f"us{int(us.group(1))}"
            continue
        # An unrecognised segment is not ignorable. Ignoring it would turn
        # `IT-B34-XX-GD` into `IT-B34-GD`, which resolves — to the wrong master.
        return None

    return _apply_client_rules(SpecKana(raw=text, token=token.upper(), colour=colour, size=size))


def _apply_client_rules(spec: SpecKana) -> SpecKana:
    """The three product-specific agreements in docs/29 §1."""
    family = spec.token.upper()
    if family == "B66" and spec.size == "19cm":
        # 2026-09-17: B66's 19CM is the M size. Our masters are B66goldM/S.
        return SpecKana(spec.raw, spec.token, spec.colour, "M", ruled=True)
    if family == "B34" and spec.size is None:
        # 2026-09-17: a B34 code with no size is the 22cm. Our masters carry
        # 19/20/22cm, so leaving it unset matches three ways.
        return SpecKana(spec.raw, spec.token, spec.colour, "22cm", ruled=True)
    if family == "B33" and spec.size is not None:
        # 2026-09-17: 20cm and 22cm share one master for now, so the size is
        # deliberately discarded and two wholesale codes meet on one master.
        # docs/29 §9 is the reminder to revisit when they split.
        return SpecKana(spec.raw, spec.token, spec.colour, None, ruled=True)
    return spec


def token_candidates(token: str, sku_codes: list[str]) -> list[str]:
    """Masters whose code starts with this token.

    The negative lookahead is what keeps `N10` from claiming `N103`, and `B3`
    from claiming `B34`. Our codes put the token first with no separator, so a
    plain `startswith` is wrong on exactly the products that have the most
    variants.
    """
    pattern = re.compile(rf"^{re.escape(token)}(?!\d)", re.IGNORECASE)
    return sorted(code for code in sku_codes if pattern.match(code))


def _tail(code: str, token: str) -> str:
    """The part of a master code after its token, normalised for comparison."""
    return code[len(token) :].replace("-", "").replace("_", "").lower()


def match_code(product_code: str, spec_kana: str, sku_codes: list[str]) -> CodeMatch:
    """Resolve one wholesale code to one master, or say why it could not be."""
    if spec_kana.strip() == GIFT_BAG_KANA or product_code == GIFT_BAG_CODE:
        return CodeMatch(
            product_code=product_code,
            spec_kana=spec_kana,
            kind=MatchKind.NON_PRODUCT,
            sku_code=None,
            note=(
                "梱包材。在庫管理対象外マスタとして登録する（docs/29 §5-4）。"
                "既存の H1 / 10005 ギフトボックスとは別物なので統合しない。"
            ),
        )

    spec = parse_spec_kana(spec_kana)
    if spec is None:
        return CodeMatch(
            product_code=product_code,
            spec_kana=spec_kana,
            kind=MatchKind.UNMATCHED,
            sku_code=None,
            note="規格カナを解析できません。手作業で対応先を決めてください。",
        )

    candidates = token_candidates(spec.token, sku_codes)
    if not candidates:
        return CodeMatch(
            product_code=product_code,
            spec_kana=spec_kana,
            kind=MatchKind.UNMATCHED,
            sku_code=None,
            note=f"トークン {spec.token} に該当するマスタがありません。",
        )

    wanted = spec.expected_tail
    exact = [code for code in candidates if _tail(code, spec.token) == wanted]
    if len(exact) == 1:
        return CodeMatch(
            product_code=product_code,
            spec_kana=spec_kana,
            kind=MatchKind.RULE if spec.ruled else MatchKind.EXACT,
            sku_code=exact[0],
            candidates=tuple(candidates),
            note=_rule_note(spec) if spec.ruled else "",
        )
    if len(exact) > 1:
        return CodeMatch(
            product_code=product_code,
            spec_kana=spec_kana,
            kind=MatchKind.AMBIGUOUS,
            sku_code=None,
            candidates=tuple(exact),
            note=f"末尾 {wanted!r} が複数のマスタに一致します。",
        )

    # No exact match. The colour may be an axis we do not model for this product
    # — R63 and the keyrings. Dropping it is only safe when NO master for this
    # token carries a colour at all; otherwise a silver code could land on a
    # gold master, which is the one outcome worth failing over.
    if spec.colour is not None and not any(
        colour in _tail(code, spec.token) for code in candidates for colour in _COLOURS.values()
    ):
        colourless = [
            code for code in candidates if _tail(code, spec.token) == spec.tail_without_colour
        ]
        if len(colourless) == 1:
            return CodeMatch(
                product_code=product_code,
                spec_kana=spec_kana,
                kind=MatchKind.COLOURLESS,
                sku_code=colourless[0],
                candidates=tuple(candidates),
                note=(
                    f"このトークンのマスタは色の軸を持たないため、色 "
                    f"{spec.colour} を無視して一致させました（docs/29 §5-2）。"
                    "適用前に確認してください。"
                ),
            )

    return CodeMatch(
        product_code=product_code,
        spec_kana=spec_kana,
        kind=MatchKind.UNMATCHED,
        sku_code=None,
        candidates=tuple(candidates),
        note=f"末尾 {wanted!r} に一致するマスタがありません。",
    )


def _rule_note(spec: SpecKana) -> str:
    family = spec.token.upper()
    if family == "B66":
        return "B66 の 19CM は M サイズ（2026-09-17 合意）。"
    if family == "B34":
        return "B34 のサイズ表記なしは 22cm（2026-09-17 合意）。"
    if family == "B33":
        return (
            "B33 は 20cm と 22cm を当面ひとつの在庫として管理（2026-09-17 合意）。"
            "サイズ別に分離した時点でこの対応表も更新が必要（docs/29 §9）。"
        )
    return ""


def build_table(
    pairs: list[tuple[str, str]],
    sku_codes: list[str],
) -> list[CodeMatch]:
    """Match every (商品コード, 規格カナ) pair, in code order.

    One row per wholesale code, never per line: the fix for an unmatched code is
    a single mapping row, and a table with 1,887 rows in it would hide that.
    """
    return [match_code(code, kana, sku_codes) for code, kana in sorted(set(pairs))]


def summarise(table: list[CodeMatch]) -> dict[str, int]:
    counts = {kind.value: 0 for kind in MatchKind}
    for row in table:
        counts[row.kind.value] += 1
    return counts


__all__ = [
    "GIFT_BAG_CODE",
    "GIFT_BAG_KANA",
    "CodeMatch",
    "MatchKind",
    "SpecKana",
    "build_table",
    "match_code",
    "parse_spec_kana",
    "summarise",
    "token_candidates",
]
