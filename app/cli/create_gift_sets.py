"""Register ギフトセット: one sellable SKU that consumes several others.

`link_shared_stock` cannot express this. It builds the opposite topology — many
SKUs drawing on ONE pool (the N108 chain-length case) — and refuses outright
once a master already has a component: 「既に別のSKUの在庫を参照しています」.
A gift set is one master with SEVERAL components, so it needs its own entry.

The table has always supported it (`quantity_per`, UNIQUE on bundle+component)
and `resolve_consumption` already returns every component, so the sale fan-out
needs no change. Only the way in was missing.

WHY A COMPONENT MAY NOT ITSELF BE A SET

`resolve_consumption` expands ONE level and does not recurse. A set whose
component is another set would write its stock event against that inner set's
master — a bundle parent, which holds no snapshot by design — so the goods on
the shelf would never move and a parent would grow a snapshot it should not
have. Refused rather than flattened, because flattening silently would hide a
catalogue mistake behind a correct-looking result.

WHY ONE BAD ROW REFUSES THE WHOLE FILE

Same reason as `import_wholesale_mappings`: the dangerous outcome is the PARTIAL
apply. Half a gift set is worse than none — the sale still fans out to whichever
components did land, so every order silently under-deducts, and nothing on any
screen says so. `csv_intake` reports a bad row and SKIPS it, which is right for
an upload screen the operator will fix and retry; here every reported row is
promoted to fatal instead.

THE CHANNEL COLUMNS, AND WHY RAKUTEN NEEDS TWO

`ShopifySKU`, `楽天商品管理番号` and `楽天SKU` are optional columns carrying the
channel SKUs the set is sold under, and each channel becomes one
`channel_sku_mappings` row. A BLANK cell means 「このチャネルでは売らない」 and
stays blank.

The two channels are addressed at different GRAINS, and the grain is not ours to
pick: it is fixed by what the order adapters put on a line.

    shopify   channel_sku = the Shopify SKU,  channel_variant_key = NULL
    rakuten   channel_sku = 商品管理番号 (the PAGE),
              channel_variant_key = SKU管理番号 (the SKU inside that page)

`RakutenAdapter._to_normalized` reads `manageNumber` into `channel_sku` and the
SkuModelList's `variantId` into `channel_variant_key`, so a Rakuten order line
never arrives keyed on the SKU管理番号 alone. It could not be: 132 SKU管理番号
are reused across different products in this shop (`inspect_rakuten_sku_csv`),
so the SKU alone is not an address, while all 1,107 (page, SKU) pairs are
unique. The 1,104 live mappings `create_rakuten_variant_masters` wrote are that
pair, and so are these.

WHY BOTH RAKUTEN COLUMNS ARE REQUIRED TOGETHER

Half a pair is refused rather than written, because the two halves fail in
opposite directions and neither failure shows on any screen.

* A 楽天SKU with no page is a row Rakuten never asks about. The order parks in
  `pending_mapping` and deducts nothing, and `push_safety.screen_targets` drops
  every rakuten target whose variant key is NULL — so the set's availability is
  never pushed either, while this command prints 登録しました.
* A page with no 楽天SKU is worse: it is a page-level WILDCARD, and
  `resolve_master_sku_id`'s second step hands it every unmapped SKU on that
  page. One gift set would quietly start absorbing the sales of every other SKU
  sold from the same 商品管理番号.

Refusing the half pair is what makes a variant_key-NULL rakuten row impossible
to create from this CLI at all. The 商品管理番号 is not in the client's
correspondence table and has to be asked for, so a file may legitimately arrive
with both Rakuten columns blank, be registered for Shopify, and be re-run with
the pair filled in later — see RE-RUNNING A HALF-FINISHED SESSION.

WHY A BLANK CHANNEL SKU IS NEVER FILLED IN FOR THE OPERATOR

Defaulting the Shopify SKU to セットSKU would look right for the Christmas sets
and be wrong in general. The house convention (`import_variant_mappings`) is
that `sku_code` is the Shopify SKU *when there is one*, and the Rakuten
SKU管理番号 otherwise — so a future Rakuten-only set has a `sku_code` that is a
Rakuten SKU and no Shopify SKU at all, and a default would map it onto Shopify
under a code Shopify has never heard of. Worse, a mistyped header leaves the
column absent from the file entirely: with a default that reads as a file-full
of successful Shopify mappings, and with none it reads as what it is.

RE-RUNNING A HALF-FINISHED SESSION

The components and the mappings are tracked SEPARATELY on purpose. A set can be
"構成品は登録済み、チャネルSKUは未登録" — that is exactly the state a session
interrupted between two files leaves behind, and treating the set as wholly
registered because its components match would make the missing mapping
unreachable for ever: no re-run would ever add it.

The CSV, one row per component (a set spans as many rows as it has parts). The
channel columns describe the SET, not the component, so every row of one set
repeats them:

    セットSKU,セット商品名,構成品SKU,数量,ShopifySKU,楽天商品管理番号,楽天SKU
    B66setGoldS,B66 ゴールド S 2個セット,B66goldS,2,B66setGoldS,gift2026a,202612a-1
    B34B66setA,B34シルバー19 と B66ゴールドS のセット,B34silver19cm,1,B34B66setA,,
    B34B66setA,B34シルバー19 と B66ゴールドS のセット,B66goldS,1,B34B66setA,,

    powershell -File scripts/run_cli.ps1 -Cli create_gift_sets ^
        -Args "--file csv_file/phase2/source/gift_sets.csv"
    ... same again with --apply to write.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.csv_intake import (
    ColumnSpec,
    CsvSpec,
    OnEmpty,
    _read_header,
    decode_csv,
    inspect,
    iter_rows,
    resolve_header,
)
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import BundleComponent, ChannelSkuMapping, InventorySnapshot, MasterSku
from app.services.mapping import ChannelTarget

log = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

COL_SET = "セットSKU"
COL_NAME = "セット商品名"
COL_COMPONENT = "構成品SKU"
COL_QTY = "数量"
COL_SHOPIFY = "ShopifySKU"
COL_RAKUTEN_PAGE = "楽天商品管理番号"
COL_RAKUTEN = "楽天SKU"

SHOPIFY = "shopify"
RAKUTEN = "rakuten"

#: Which columns address each `channel_sku_mappings.channel`, coarsest first.
#: NOT one column per channel: a Rakuten address is the (商品管理番号, SKU管理番号)
#: PAIR, and the module docstring says why half of it is never written.
CHANNEL_COLUMNS: dict[str, tuple[str, ...]] = {
    SHOPIFY: (COL_SHOPIFY,),
    RAKUTEN: (COL_RAKUTEN_PAGE, COL_RAKUTEN),
}
#: What to call a channel in a message: the column the operator edits. The
#: finest column of the pair, because that is the one naming the set.
COLUMN_OF_CHANNEL: dict[str, str] = {ch: cols[-1] for ch, cols in CHANNEL_COLUMNS.items()}
#: Every channel column, in file order. `group` compares the RAW cells of these
#: row against row, NOT the `channels` dict built from them: `_row_channels`
#: drops a half-filled Rakuten pair and returns the reason separately, so two
#: rows disagreeing only in a dropped half yield the same dict and the
#: disagreement check compared EQUAL.
CHANNEL_CELLS: tuple[str, ...] = tuple(
    column for columns in CHANNEL_COLUMNS.values() for column in columns
)

#: `master_skus.sku_code` is String(64) and `name` is String(255). Checked here,
#: where the message can name the row, rather than letting the database refuse
#: the insert after the rest of the file has been staged.
SKU_CODE_WIDTH = 64
NAME_WIDTH = 255
#: `channel_sku_mappings.channel_sku` and `.channel_variant_key` are both
#: String(128). Same reason.
CHANNEL_SKU_WIDTH = 128

#: Fullwidth digits, by code point rather than written as themselves. RUF001 is
#: on for this whole project because a stray fullwidth character is nearly
#: always a typo; these are the rare deliberate ones, and a code point says
#: deliberate where a `noqa` only says silenced. (`ruff format` rewrites a
#: \uXXXX escape back into the character, so the escape alone will not hold.)
_FW0, _FW9 = chr(0xFF10), chr(0xFF19)

#: A 数量 that is a count of items: digits, half-width or fullwidth, optionally
#: with a trailing ".0*" because Excel writes 2 as 2.0.
_COUNT_RE = re.compile(rf"^[0-9{_FW0}-{_FW9}]+(?:\.0*)?$")
#: A number that is not a count, matched only so the refusal can say
#: 整数ではありません about the 1.5 the operator can see in the cell.
_DECIMAL_RE = re.compile(rf"^[0-9{_FW0}-{_FW9}]+\.[0-9{_FW0}-{_FW9}]+$")
#: 全角 digits are what a Japanese keyboard produces in 全角 mode.
_FULLWIDTH_DIGITS = str.maketrans({ord(_FW0) + n: str(n) for n in range(10)})

#: What a memo line starts with. `csv_intake.comment_prefix` is the ASCII one;
#: the fullwidth one is the same key on a Japanese keyboard left in 全角 mode,
#: and this project already reads fullwidth input as the thing it looks like —
#: see `_FULLWIDTH_DIGITS` above, including why it is a code point and not the
#: character itself.
_FULLWIDTH_HASH = chr(0xFF03)
_COMMENT_PREFIXES = ("#", _FULLWIDTH_HASH)

#: How many non-empty cells a commented-out line may carry and still be read as
#: prose. Excel pads every row out to the used-range width when it saves, so a
#: memo comes back as 「# 以上です」 plus six empty cells and one filled cell is
#: all a memo ever has.
_MEMO_MAX_FILLED = 1


def quantity_fault(value: str) -> str | None:
    """Why this 数量 is not a count of items, or None when it is.

    Matched explicitly instead of probed with `float()`, because `float()`
    accepts four spellings that are not counts and that the checks after it
    cannot refuse. 'nan' escapes as an uncaught ValueError and 'inf' as an
    OverflowError, turning a plan-then-apply refusal into a traceback that says
    nothing about any other row. '1e3' passes as 1000. '1_0' passes as 10, and
    that is the one that bites: the client writes underscores in the 楽天SKU
    column (202612b_9), so a file saved one column out of line puts an
    underscored token where 数量 is read and it imports as a quantity.

    A fullwidth digit is accepted, because the operator who typed it on a
    Japanese keyboard meant the number it looks like.
    """
    if _COUNT_RE.match(value) is None:
        if _DECIMAL_RE.match(value) is not None:
            return f"{COL_QTY}が整数ではありません: 「{value}」"
        return f"{COL_QTY}が数字ではありません: 「{value}」"
    if quantity_value(value) < 1:
        return f"{COL_QTY}が1以上ではありません: 「{value}」"
    return None


def quantity_value(value: str) -> int:
    """The count `quantity_fault` accepted, as an int.

    Defined next to the match rather than left as `int(float(...))` at the call
    site: that would re-admit at the second reading exactly the spellings the
    first reading refuses, so the two could disagree about the same cell.
    """
    return int(value.split(".")[0].translate(_FULLWIDTH_DIGITS))


SPEC = CsvSpec(
    columns=(
        ColumnSpec(canonical=COL_SET, aliases=("set_sku",)),
        ColumnSpec(canonical=COL_NAME, aliases=("set_name",)),
        ColumnSpec(canonical=COL_COMPONENT, aliases=("component_sku",)),
        ColumnSpec(canonical=COL_QTY, aliases=("quantity",), validator=quantity_fault),
        # OPTIONAL columns, and KEEP rather than ERROR: an empty cell is the
        # answer 「このチャネルでは売らない」, not a row the operator forgot to
        # finish. `required=False` means a file written before these columns
        # existed still imports; see the module docstring on why neither case
        # is quietly turned into a mapping.
        ColumnSpec(
            canonical=COL_SHOPIFY,
            aliases=("shopify_sku",),
            required=False,
            on_empty=OnEmpty.KEEP,
        ),
        ColumnSpec(
            canonical=COL_RAKUTEN_PAGE,
            aliases=("rakuten_manage_number", "商品管理番号"),
            required=False,
            on_empty=OnEmpty.KEEP,
        ),
        ColumnSpec(
            canonical=COL_RAKUTEN,
            aliases=("rakuten_sku", "RakutenSKU"),
            required=False,
            on_empty=OnEmpty.KEEP,
        ),
    )
)


@dataclass(frozen=True, slots=True)
class Part:
    """One component of one set, as the file asks for it."""

    component_sku: str
    quantity: int


@dataclass(slots=True)
class GiftSet:
    """One set as the file asks for it, plus what the catalogue already holds.

    `components_registered` and `pending_channels` are deliberately independent:
    a set whose 構成品 are all in place may still be missing a channel mapping,
    and that set has work to do. See the module docstring.
    """

    set_sku: str
    name: str
    parts: list[Part] = field(default_factory=list)
    #: channel -> where that channel sells the set, at the grain the channel is
    #: addressed by. `ChannelTarget` is reused from `app.services.mapping`
    #: rather than restated: it is already the (channel_sku, variant_key) pair
    #: the push path and `ChannelSkuMapping` are both built around, so a second
    #: definition here could only drift from it. Channels the file left blank
    #: are absent, which is how 「売らない」 is represented.
    channel_skus: dict[str, ChannelTarget] = field(default_factory=dict)
    components_registered: bool = False
    #: Channels whose mapping row has to be INSERTed.
    pending_channels: list[str] = field(default_factory=list)
    #: Channels whose row exists on THIS master but is inactive. Work, not a
    #: re-run: `resolve_master_sku_id` filters `is_active`, so the set stays
    #: unresolvable until the row is turned back on.
    reactivate_channels: list[str] = field(default_factory=list)
    #: Channels already active on THIS master. Printed, never written again.
    mapped_channels: list[str] = field(default_factory=list)

    def has_work(self) -> bool:
        return (
            not self.components_registered
            or bool(self.pending_channels)
            or bool(self.reactivate_channels)
        )


@dataclass(frozen=True, slots=True)
class Fault:
    """Why the file cannot be applied. One per problem, naming where it is."""

    where: str
    reason: str


@dataclass(slots=True)
class Plan:
    sets: list[GiftSet] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)

    def ready(self) -> list[GiftSet]:
        """The sets with anything left to write — components, a mapping, or both."""
        return [s for s in self.sets if s.has_work()]


def read_faults(data: bytes) -> list[Fault]:
    """Everything `csv_intake` would report, as whole-file refusals.

    A row it would merely skip is promoted here: see the module docstring on
    why a partial apply is the outcome worth refusing. A memo line is the one
    thing NOT promoted — `_memo_faults` says why.
    """
    found = inspect(data, SPEC)
    faults = [Fault("ファイル", reason) for reason in found.fatal]
    body = _read_body(data)
    memo_lines, memo_faults = _memo_faults(body)
    # A memo line is dropped from the promoted row issues, not reported.
    # `csv_intake` applies comment_prefix only ABOVE the header, so a padded
    # 「# 以上です,,,,,,」 arrives as a full-width row and `inspect` reports it as
    # 「セット商品名が空です」: the operator is told a column is empty on a line
    # the template invited them to write. Filtered here and not in `csv_intake`
    # because the CROSS MALL exports depend on that module's leniency and its
    # other callers depend on comment_prefix applying only above the header.
    faults += [
        Fault(f"{issue['line']}行目", str(issue["reason"]))
        for issue in found.row_issues
        if issue["line"] not in memo_lines
    ]
    hidden = (found.total_rows - found.valid_rows) - len(found.row_issues)
    if hidden > 0:
        # `max_row_issues` caps the list. Saying so beats a report that reads
        # as if the problems stopped at fifty.
        faults.append(Fault("ファイル", f"ほかにも {hidden} 行に問題があります"))
    return faults + memo_faults + _ragged_faults(body)


@dataclass(frozen=True, slots=True)
class _Body:
    """The lines below the resolved header, read once for both line checks.

    `needed` is how many fields a row must carry to survive `csv_intake`'s
    width test, and `lines` pairs each row with the line number the operator
    sees in Excel.
    """

    needed: int
    lines: tuple[tuple[int, list[str]], ...]


def _read_body(data: bytes) -> _Body | None:
    """The file below its header, or None when the header itself is unusable —
    `inspect` has already reported that, and a second complaint per row would
    bury it."""
    try:
        text = decode_csv(data, SPEC)
    except ValueError:
        return None
    reader = csv.reader(io.StringIO(text))
    header, header_line = _read_header(reader, SPEC)
    if header is None:
        return None
    index, missing = resolve_header(header, SPEC)
    if missing:
        return None
    return _Body(
        needed=max(index.values(), default=-1) + 1,
        lines=tuple((header_line + 1 + offset, row) for offset, row in enumerate(reader) if row),
    )


def _is_memo(row: list[str]) -> bool:
    """Whether this line below the header is commented out.

    `csv_intake` deliberately applies comment_prefix only ABOVE the header, so
    a `#` line below the data belongs to this CLI to deal with.
    """
    return bool(row) and row[0].lstrip().startswith(_COMMENT_PREFIXES)


def _memo_faults(body: _Body | None) -> tuple[set[int], list[Fault]]:
    """The memo lines to ignore, and the faults for the lines that only LOOK
    like memos.

    Every template this project hands the client opens with guidance lines and
    the client edits and returns THAT file, so a 「# 以上です」 below the data is
    an invited line and must be ignored wherever it sits — not reported as a
    short row, and not reported as an empty column either.

    WHY IGNORING EVERY COMMENTED-OUT LINE WOULD BE WRONG: "#S1,セット,B,1" is
    not prose, it is a data row someone commented out. Dropped in silence the
    set registers one component short, every sale under-deducts and no screen
    says so — the exact half-set `_ragged_faults` exists to prevent. So the
    count of non-empty cells decides: one is prose (the rest of the row is
    Excel's padding), two or more is data, and data is refused.

    That split is also why `group` never has to know about memo lines: a row
    `iter_rows` yields has all four required columns filled, so it is one of
    the rows refused here and the file never reaches the apply.
    """
    if body is None:
        return set(), []
    memo: set[int] = set()
    faults: list[Fault] = []
    for lineno, row in body.lines:
        if not _is_memo(row):
            continue
        if sum(1 for cell in row if cell.strip()) > _MEMO_MAX_FILLED:
            faults.append(
                Fault(
                    f"{lineno}行目",
                    "コメント行にデータが書かれています。行頭の「#」を外してください",
                )
            )
            continue
        memo.add(lineno)
    return memo, faults


def _ragged_faults(body: _Body | None) -> list[Fault]:
    """Data rows with fewer fields than the header resolved, which `csv_intake`
    drops in total silence.

    `inspect` and `iter_rows` both `continue` on a row no wider than the widest
    resolved column index, without recording a row issue and without counting
    the row. Adding the trailing optional channel columns made 楽天SKU the
    widest index, so a row whose empty trailing cells had been trimmed — Excel
    and several CSV writers do trim them — now disappears entirely: the set
    registers with a component missing, every sale under-deducts, and no screen
    says anything. Measured on a 6-column header, "S1,セット,A,1,S1," and
    "S1,セット,B,1,S1" yielded one part and zero faults.

    Guarded HERE and not in `csv_intake`: the CROSS MALL stock export is
    genuinely ragged and its callers depend on that leniency, so tightening the
    shared module would start rejecting files that import correctly today.
    """
    if body is None:
        return []
    faults: list[Fault] = []
    for lineno, row in body.lines:
        if _is_memo(row):
            continue  # a memo is not a short row; `_memo_faults` judges it
        if len(row) < body.needed:
            faults.append(
                Fault(
                    f"{lineno}行目",
                    f"列が足りません: {body.needed}列必要ですが {len(row)}列しかありません",
                )
            )
    return faults


def group(rows: list[dict[str, str]]) -> tuple[list[GiftSet], list[Fault]]:
    """Fold the per-component rows into one entry per set.

    Faults are named by set rather than by line: the file carries `#` guidance
    lines, so a row's position in the list is not the line number the operator
    sees, and a wrong line number is worse than none.
    """
    faults: list[Fault] = []
    order: list[str] = []
    names: dict[str, str] = {}
    parts: dict[str, list[Part]] = defaultdict(list)
    #: set -> channel -> the declared address, from the set's FIRST row. A
    #: channel that row left blank is absent, and a later row that DOES name a
    #: SKU reads as a disagreement (see `typed`), not as new information.
    declared: dict[str, dict[str, ChannelTarget]] = {}
    #: set -> column -> the cell as the operator typed it, from the set's FIRST
    #: row. What the disagreement check below compares, because a dropped half
    #: pair is invisible in `declared`.
    typed: dict[str, dict[str, str]] = {}
    #: set -> the channel-cell reasons already reported for it, half pairs and
    #: disagreements alike. Any row of a set may carry either, so every row is
    #: checked; the dedupe is what keeps a four-part set from saying the same
    #: thing four times. By reason and not by set, because two rows differing
    #: in different columns are two different mistakes.
    said: dict[str, set[str]] = defaultdict(set)
    #: (channel, channel_sku, variant_key) -> the set that claimed it, for the
    #: within-file check. The TRIPLE, matching `uq_channel_sku_mapping`: two
    #: sets legitimately share one Rakuten 商品管理番号 under different
    #: SKU管理番号, and that is exactly what the Christmas file does — 46 SKUs
    #: across 4 pages — so keying on the page alone would refuse the real file.
    owner: dict[tuple[str, str, str | None], str] = {}

    for row in rows:
        set_sku, name, component = row[COL_SET], row[COL_NAME], row[COL_COMPONENT]
        where = f"「{set_sku}」"
        if len(set_sku) > SKU_CODE_WIDTH:
            faults.append(Fault(where, f"{COL_SET}が{SKU_CODE_WIDTH}文字を超えています"))
            continue
        if len(name) > NAME_WIDTH:
            faults.append(Fault(where, f"{COL_NAME}が{NAME_WIDTH}文字を超えています"))
            continue
        if component == set_sku:
            faults.append(Fault(where, "セット自身を構成品にはできません"))
            continue

        channels, half_pairs = _row_channels(row)
        cells = {column: row.get(column, "").strip() for column in CHANNEL_CELLS}
        # Checked on EVERY row, not just the first. The first row of a set may
        # leave both Rakuten cells blank and a later row fill only one of them:
        # `_row_channels` drops the half pair from `channels` either way, so both
        # rows produced the same dict, the disagreement check compared equal, and
        # the file applied with no fault at all — the operator's value silently
        # discarded. Still reported once per set, by reason: a four-part set
        # repeating one mistake is one mistake.
        fresh = [reason for reason in half_pairs if reason not in said[set_sku]]
        said[set_sku].update(fresh)
        faults += [Fault(where, reason) for reason in fresh]
        if set_sku not in names:
            order.append(set_sku)
            names[set_sku] = name
            declared[set_sku] = channels
            typed[set_sku] = cells
            faults += _channel_faults(set_sku, channels, owner)
        elif names[set_sku] != name:
            # One set, two names: we would have to pick one, and the operator
            # would never learn which. Say so instead.
            faults.append(Fault(where, f"同じ{COL_SET}に別の{COL_NAME}が書かれています"))
            continue
        elif typed[set_sku] != cells:
            # Exactly the 別のセット商品名 case, and refused for the same
            # reason: two rows of one set disagree, so there is no way to pick
            # one without the operator ever learning which won. A blank against
            # a filled cell counts — the channel columns describe the set, so a
            # half-filled column is a mistake, not two different answers.
            #
            # Compared cell by RAW cell, and the differing cells are the ones
            # named: a disagreement inside a dropped Rakuten half is invisible
            # in `declared`, and naming only COLUMN_OF_CHANNEL would point the
            # operator at 楽天SKU when 楽天商品管理番号 is the cell that differs.
            differing = ", ".join(
                column for column in CHANNEL_CELLS if typed[set_sku][column] != cells[column]
            )
            # Reported once per set through `said`, the same way the half pair
            # above is. A four-part set whose first row leaves the channel cells
            # blank disagrees with each of the other three, and one line per row
            # made one mistake read as four; on the 48-set file an operator who
            # filled the channel columns on the component rows but not on each
            # set's first row got roughly 40 repeated lines, burying whatever
            # else was wrong.
            reason = f"同じ{COL_SET}の行で{differing}が一致していません"
            if reason not in said[set_sku]:
                said[set_sku].add(reason)
                faults.append(Fault(where, reason))
            continue

        if any(p.component_sku == component for p in parts[set_sku]):
            # Two rows would become two bundle_components for one pair, and the
            # UNIQUE constraint would refuse the second after staging the rest.
            faults.append(Fault(where, f"同じ構成品が重複しています: 「{component}」"))
            continue
        parts[set_sku].append(Part(component_sku=component, quantity=quantity_value(row[COL_QTY])))

    sets = [
        GiftSet(set_sku=s, name=names[s], parts=parts[s], channel_skus=declared[s]) for s in order
    ]
    return sets, faults


def _row_channels(row: dict[str, str]) -> tuple[dict[str, ChannelTarget], list[str]]:
    """Where this row says the set is sold, as channel -> address, plus the
    reasons any channel was left out.

    `iter_rows` yields no key at all for an optional column missing from the
    header, so every column is read with `.get`. A blank cell is left OUT rather
    than stored as "": nothing is sold on that channel, and the module docstring
    says why that is never turned into a default.

    A Rakuten address needs BOTH of its columns, and half of one is reported
    rather than written. That refusal is what makes a rakuten row with a NULL
    `channel_variant_key` impossible to create from this CLI — such a row is a
    page-level WILDCARD that `resolve_master_sku_id`'s second step would hand
    every unmapped SKU on that 商品管理番号, so one gift set would silently
    absorb the sales of everything else sold from the same page. The mirror
    case, a 楽天SKU with no page, is an address Rakuten never asks about at all:
    the order parks in `pending_mapping` and `push_safety.screen_targets` drops
    the target, while this command prints 登録しました.
    """
    found: dict[str, ChannelTarget] = {}
    problems: list[str] = []
    for channel, columns in CHANNEL_COLUMNS.items():
        values = [row.get(column, "").strip() for column in columns]
        if not any(values):
            continue
        blank = [column for column, value in zip(columns, values, strict=True) if not value]
        if blank:
            filled = ", ".join(
                column for column, value in zip(columns, values, strict=True) if value
            )
            problems.append(f"{filled}があるのに{', '.join(blank)}が空です")
            continue
        found[channel] = ChannelTarget(
            channel_sku=values[0],
            variant_key=values[1] if len(values) > 1 else None,
        )
    return found, problems


def _channel_faults(
    set_sku: str,
    channels: dict[str, ChannelTarget],
    owner: dict[tuple[str, str, str | None], str],
) -> list[Fault]:
    """Width and within-file uniqueness, checked once per set.

    `owner` is updated in place: the first set to claim a (channel, channel_sku,
    variant_key) keeps it, and the second is the one named as the problem.
    """
    faults: list[Fault] = []
    where = f"「{set_sku}」"
    for channel, address in channels.items():
        column = COLUMN_OF_CHANNEL[channel]
        # Whichever column actually overflowed, not the channel's name column:
        # a 130-character 楽天商品管理番号 beside a 10-character 楽天SKU was
        # refused with 「楽天SKUが128文字を超えています」, pointing the operator at
        # a cell that is plainly 10 characters long.
        columns = CHANNEL_COLUMNS[channel]
        values = (address.channel_sku, address.variant_key or "")[: len(columns)]
        too_long = [
            col
            for col, value in zip(columns, values, strict=True)
            if len(value) > CHANNEL_SKU_WIDTH
        ]
        if too_long:
            wide = ", ".join(too_long)
            faults.append(Fault(where, f"{wide}が{CHANNEL_SKU_WIDTH}文字を超えています"))
            continue
        key = (channel, address.channel_sku, address.variant_key)
        claimed = owner.get(key)
        if claimed is not None:
            # One channel address cannot sell two different sets: whichever row
            # landed second would be refused by `uq_channel_sku_mapping` after
            # the rest of the file had been staged. The address and not the page
            # — two sets on one Rakuten 商品管理番号 under different SKU管理番号
            # are two different rows, and the real file is full of them.
            faults.append(
                Fault(where, f"{column}「{address.label}」が「{claimed}」と重複しています")
            )
            continue
        owner[key] = set_sku
    return faults


def read_file(path: Path) -> tuple[bytes, list[Fault]]:
    """Sync on purpose: file IO does not belong inside the async run
    (ASYNC240), the same way `import_wholesale_mappings` keeps it out."""
    if not path.exists():
        return b"", [Fault(str(path), "ファイルが見つかりません")]
    return path.read_bytes(), []


async def plan(session: AsyncSession, data: bytes) -> Plan:
    faults = read_faults(data)
    if faults:
        return Plan([], faults)
    sets, faults = group(list(iter_rows(data, SPEC)))
    if faults:
        return Plan(sets, faults)

    wanted = {s.set_sku for s in sets} | {p.component_sku for s in sets for p in s.parts}
    found = await session.execute(
        select(
            MasterSku.sku_code,
            MasterSku.id,
            MasterSku.is_bundle,
            # Read for the SET and for its COMPONENTS, because the flag fails
            # in a different direction on each. `resolve_consumption` tests it
            # on the parent FIRST and returns [] — a 在庫管理対象外 set deducts
            # nothing at all — then fans out and writes one stock event per
            # component WITHOUT testing it again, so a 在庫管理対象外 component
            # (a gift bag or a box, the single most likely extra line on a
            # Christmas set) collects real stock events against a master that
            # holds no inventory and drives its snapshot negative.
            MasterSku.is_stock_managed,
            MasterSku.archived_at,
        ).where(MasterSku.sku_code.in_(wanted))
    )
    masters = {
        code: (mid, bundle, stock_managed, archived)
        for code, mid, bundle, stock_managed, archived in found.all()
    }
    code_of = {mid: code for code, (mid, _, _, _) in masters.items()}

    set_ids = [masters[s.set_sku][0] for s in sets if s.set_sku in masters]
    held = await session.execute(
        select(InventorySnapshot.master_sku_id, InventorySnapshot.on_hand_qty).where(
            InventorySnapshot.master_sku_id.in_(set_ids)
        )
    )
    on_hand = {mid: qty for mid, qty in held.all()}  # noqa: C416

    linked = await session.execute(
        select(
            BundleComponent.bundle_master_sku_id,
            BundleComponent.component_master_sku_id,
            BundleComponent.quantity_per,
        ).where(BundleComponent.bundle_master_sku_id.in_(set_ids))
    )
    existing: dict[int, dict[int, int]] = defaultdict(dict)
    for bundle_id, component_id, quantity_per in linked.all():
        existing[bundle_id][component_id] = quantity_per

    # Every master already answering to one of the file's channel addresses.
    # Not narrowed to this file's sets on purpose: the point of the read is to
    # find an address pointing somewhere ELSE.
    #
    # `is_active` is read, not filtered. An inactive row still occupies
    # `uq_channel_sku_mapping`, so it cannot be inserted around — but
    # `resolve_master_sku_id` filters `is_active.is_(True)`, so it resolves
    # nothing either. Ignoring the flag reported such a row as 登録済み, wrote
    # nothing, and left the set unresolvable with no re-run able to repair it.
    #
    # Narrowed by `channel_sku` only, and keyed on the full triple below:
    # on Rakuten `channel_sku` is the PAGE, which legitimately carries other
    # products' SKUs, so a row on the same page under a different SKU管理番号 is
    # not a claim on this set's address at all.
    mapped = await session.execute(
        select(
            ChannelSkuMapping.channel,
            ChannelSkuMapping.channel_sku,
            ChannelSkuMapping.channel_variant_key,
            ChannelSkuMapping.master_sku_id,
            ChannelSkuMapping.is_active,
        ).where(
            ChannelSkuMapping.channel.in_(sorted(CHANNEL_COLUMNS)),
            ChannelSkuMapping.channel_sku.in_(
                {address.channel_sku for s in sets for address in s.channel_skus.values()}
            ),
            # `marketplace_id` is the fourth column of the unique key and this
            # CLI always writes it NULL, so a row scoped to a marketplace does
            # not occupy the key we are about to take and is not a conflict.
            ChannelSkuMapping.marketplace_id.is_(None),
        )
    )
    holders: dict[tuple[str, str, str | None], dict[int, bool]] = defaultdict(dict)
    for channel, channel_sku, variant_key, master_id, is_active in mapped.all():
        holders[(channel, channel_sku, variant_key)][master_id] = is_active

    # `code_of` knows only the masters this file names, and an address held by a
    # master the CSV never mentions is the NORMAL conflict — so without this the
    # refusal printed 「現在: master_sku_id 77」, an internal id that no operator
    # screen can look up. Executed unconditionally, even for an empty set of
    # ids, so the number of reads this function makes does not depend on data.
    #
    # It also covers the components a set ALREADY holds, which is why one read
    # serves both: 「既に別の構成で登録されています」 names those components, and
    # filtering them through a `code_of` built from `wanted` dropped every one
    # the CSV does not mention — printing 「現在: 」 with nothing after the colon,
    # or naming only the component that is the SAME as the file's, so the
    # message asserted a difference and then hid it.
    unresolved = {mid for rows in holders.values() for mid in rows}
    unresolved |= {component_id for rows in existing.values() for component_id in rows}
    named = await session.execute(
        select(MasterSku.id, MasterSku.sku_code).where(
            MasterSku.id.in_(unresolved - set(code_of)),
        )
    )
    code_of.update(dict(named.tuples().all()))

    for gift in sets:
        where = f"「{gift.set_sku}」"
        for part in gift.parts:
            if part.component_sku not in masters:
                faults.append(Fault(where, f"構成品のマスタがありません: 「{part.component_sku}」"))
                continue
            _, is_bundle, stock_managed, archived = masters[part.component_sku]
            if is_bundle:
                # resolve_consumption expands one level only; see the module
                # docstring. The goods on the shelf would never move.
                faults.append(Fault(where, f"構成品が既にセット商品です: 「{part.component_sku}」"))
            if not stock_managed:
                # The fan-out does not re-test the flag, so this component would
                # collect real stock events against a master that holds no
                # inventory and drive its snapshot negative. None of the 14
                # components in the Christmas file is 在庫管理対象外; the next
                # file is the one that will carry a gift bag.
                faults.append(Fault(where, f"構成品が在庫管理対象外です: 「{part.component_sku}」"))
            if archived is not None:
                faults.append(Fault(where, f"構成品が廃止されています: 「{part.component_sku}」"))

        if gift.set_sku not in masters:
            continue  # a new master: nothing more to check

        set_id, is_bundle, set_stock_managed, archived = masters[gift.set_sku]
        if archived is not None:
            faults.append(Fault(where, "このSKUは廃止されています"))
            continue
        if not set_stock_managed:
            # The SET's own flag, read and until now discarded.
            # `resolve_consumption` tests is_stock_managed on the PARENT first
            # and returns [] before it ever reads bundle_components, so this set
            # would deduct NOTHING on any sale however correct its components
            # are — and `BundlePushService.all_bundle_ids` keeps only the
            # 在庫管理対象 parents, so its availability would never be pushed
            # either. Not a hypothetical state: an operator reasoning
            # 「セット自身は在庫を持たない」 sets exactly this flag, and the shop
            # already uses it for the wholesale gift bag (master 2974).
            faults.append(Fault(where, "このSKUは在庫管理対象外です"))
            continue
        quantity = on_hand.get(set_id)
        if not is_bundle and quantity:
            # Becoming a set means this master's own snapshot stops being
            # consulted — availability comes from the components. Linking it
            # would make real stock vanish from every screen. Same guard, and
            # the same reason, as link_shared_stock.
            faults.append(Fault(where, f"自身の在庫が {quantity} 残っています"))
            continue

        want = {
            masters[p.component_sku][0]: p.quantity
            for p in gift.parts
            if p.component_sku in masters
        }
        have = existing.get(set_id, {})
        if have == want:
            gift.components_registered = True
        elif have:
            # Every existing component, with its 数量: a component whose master
            # the CSV does not name is the normal case, and dropping it left the
            # message naming nothing — or naming only the part that is the same
            # as the file's, so 「別の構成」 read as an identical list. The id is
            # the fallback only when the master is truly unresolvable, exactly as
            # the channel-conflict message below does it.
            current = ", ".join(
                f"{code_of.get(mid) or f'master_sku_id {mid}'} x{quantity}"
                for mid, quantity in sorted(
                    have.items(), key=lambda item: code_of.get(item[0]) or str(item[0])
                )
            )
            faults.append(Fault(where, f"既に別の構成で登録されています。現在: {current}"))
        elif not is_bundle:
            faults.append(Fault(where, "既に通常の商品として登録されています"))

    # A second pass, so a set stopped by one of the `continue`s above is still
    # told about a channel SKU that belongs to another product: the operator
    # fixes one file, not one fault at a time.
    for gift in sets:
        where = f"「{gift.set_sku}」"
        set_id = masters[gift.set_sku][0] if gift.set_sku in masters else None
        for channel, address in gift.channel_skus.items():
            claimed = holders.get((channel, address.channel_sku, address.variant_key), {})
            # An INACTIVE row on another master is still a conflict: it holds
            # the unique key, so there is no room to insert ours beside it.
            others = sorted(set(claimed) - {set_id})
            if others:
                current = ", ".join(code_of.get(mid) or f"master_sku_id {mid}" for mid in others)
                faults.append(
                    Fault(
                        where,
                        f"{COLUMN_OF_CHANNEL[channel]}「{address.label}」は"
                        f"既に別の商品に紐づいています。現在: {current}",
                    )
                )
            elif set_id is not None and claimed.get(set_id):
                # Active and already pointing at THIS master: a re-run.
                gift.mapped_channels.append(channel)
            elif set_id is not None and set_id in claimed:
                gift.reactivate_channels.append(channel)
            else:
                gift.pending_channels.append(channel)

    return Plan(sets, faults)


async def apply(session: AsyncSession, result: Plan) -> None:
    """Create the set masters, their component links and their channel mappings.

    Adds only, and each piece independently: a set already holding its
    components still gets the mapping it is missing.
    """
    ready = result.ready()
    codes = {s.set_sku for s in ready} | {p.component_sku for s in ready for p in s.parts}
    rows = await session.execute(
        select(MasterSku.sku_code, MasterSku.id).where(MasterSku.sku_code.in_(codes))
    )
    ids = {code: mid for code, mid in rows.all()}  # noqa: C416

    for gift in ready:
        set_id = ids.get(gift.set_sku)
        if set_id is None:
            master = MasterSku(sku_code=gift.set_sku, name=gift.name, is_bundle=True)
            session.add(master)
            await session.flush()
            set_id = master.id
        else:
            existing_master = await session.get(MasterSku, set_id)
            if existing_master is not None:
                existing_master.is_bundle = True
        if not gift.components_registered:
            for part in gift.parts:
                session.add(
                    BundleComponent(
                        bundle_master_sku_id=set_id,
                        component_master_sku_id=ids[part.component_sku],
                        quantity_per=part.quantity,
                    )
                )
        for channel in gift.pending_channels:
            # The GRAIN comes from the address, not from this channel's name:
            # Shopify's `channel_sku` is the sellable unit and its variant key
            # stays NULL for ever, while Rakuten's is the (商品管理番号,
            # SKU管理番号) pair — the same shape `create_rakuten_variant_masters`
            # wrote the 1,104 live rows in. `marketplace_id` stays NULL on both,
            # as `import_variant_mappings` writes it.
            address = gift.channel_skus[channel]
            session.add(
                ChannelSkuMapping(
                    master_sku_id=set_id,
                    channel=channel,
                    channel_sku=address.channel_sku,
                    channel_variant_key=address.variant_key,
                    is_active=True,
                )
            )
        for channel in gift.reactivate_channels:
            # An UPDATE and not an insert: the row is already there, holding
            # this exact unique key, so an insert would be refused outright
            # after the rest of the file had been staged. `is_active` is forced
            # true the same way `import_variant_mappings` forces it in its
            # on_conflict_do_update — the row already points at this master, so
            # turning it back on is the whole repair.
            address = gift.channel_skus[channel]
            await session.execute(
                update(ChannelSkuMapping)
                .where(
                    ChannelSkuMapping.channel == channel,
                    ChannelSkuMapping.channel_sku == address.channel_sku,
                    ChannelSkuMapping.channel_variant_key.is_(None)
                    if address.variant_key is None
                    else ChannelSkuMapping.channel_variant_key == address.variant_key,
                    ChannelSkuMapping.marketplace_id.is_(None),
                    ChannelSkuMapping.master_sku_id == set_id,
                )
                .values(is_active=True)
            )


def state_of(gift: GiftSet, *, applied: bool) -> str:
    """What this run writes for one set, named piece by piece.

    One word cannot say it any more: a set can arrive with its 構成品 already in
    place and a mapping still missing, and collapsing that into 登録済み is
    exactly what used to hide the missing mapping.

    再有効化 is named apart from 登録 because it is a different repair on a
    different row — an existing mapping switched back on, not a new one — and an
    operator who reads 登録しました over it has no way to find out which.
    """
    pending = ["構成品"] if not gift.components_registered else []
    pending += [COLUMN_OF_CHANNEL[channel] for channel in gift.pending_channels]
    said: list[str] = []
    if pending:
        said.append(f"{' + '.join(pending)} を{'登録しました' if applied else '登録します'}")
    if gift.reactivate_channels:
        again = " + ".join(COLUMN_OF_CHANNEL[channel] for channel in gift.reactivate_channels)
        said.append(f"{again} を{'再有効化しました' if applied else '再有効化します'}")
    if not said:
        return "登録済み"
    return " / ".join(said)


async def run(
    *,
    path: Path,
    apply_changes: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    data, unreadable = read_file(path)
    factory = session_factory or async_session_factory
    async with factory() as session:
        result = Plan([], unreadable) if unreadable else await plan(session, data)

        print(f"\n--- ギフトセット登録  {path} ---")
        for fault in result.faults:
            print(f"  {fault.where:16} 実行できません: {fault.reason}")
        if result.faults:
            log.error("create_gift_sets.blocked", file=str(path), faults=len(result.faults))
            print("\n  問題があるため、1件も登録していません")
            return 1

        ready = result.ready()
        if apply_changes and ready:
            # NOT `async with session.begin()`: the planning reads above have
            # already autobegun a transaction on this session, and committing
            # that one is what makes plan-and-apply atomic.
            await apply(session, result)
            await session.commit()

        # Printed after the write, so the tense is the truth.
        for gift in result.sets:
            parts = " + ".join(f"{p.component_sku} x{p.quantity}" for p in gift.parts)
            print(f"  {gift.set_sku:24} {state_of(gift, applied=apply_changes)}  ({parts})")
            for channel, address in gift.channel_skus.items():
                mark = ""
                if channel in gift.mapped_channels:
                    mark = "登録済み"
                elif channel in gift.reactivate_channels:
                    mark = "再有効化"
                # `label` is 「page/SKU」 on Rakuten, so the printed line answers
                # 「どのSKUだったか」 from one string the same way the push logs do.
                print(f"  {'':24} {COLUMN_OF_CHANNEL[channel]}: {address.label} {mark}".rstrip())

        mappings = sum(len(gift.pending_channels) + len(gift.reactivate_channels) for gift in ready)
        if not apply_changes:
            log.info("create_gift_sets.report", file=str(path), ready=len(ready), mappings=mappings)
            print(f"\n  {len(ready)}件を登録できます  ※ 登録していません (--apply で実行)")
            return 0

    log.info("create_gift_sets.applied", file=str(path), created=len(ready), mappings=mappings)
    print(f"\n  {len(ready)}件を登録しました  (チャネルSKU {mappings}件)")
    print("  以降、これらのセットが売れると、構成品の在庫がその場で減ります")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Register gift sets (one SKU consuming several)")
    p.add_argument("--file", required=True, help="the set definition CSV")
    p.add_argument("--apply", action="store_true", help="write (default: report only)")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(path=Path(args.file), apply_changes=args.apply)))


if __name__ == "__main__":
    main()
