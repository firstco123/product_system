"""流通BMS wholesale EDI file reader (P2-051) — bytes in, vouchers out.

This is the read half of the wholesale import. It does no database work and no
inventory arithmetic: `read_bms` turns one uploaded file into vouchers, or into
the reasons it cannot be trusted. That split is what lets the upload screen show
every problem at once, before anything is written (docs/29 §8).

Two files arrive from 株式会社ヌーヴ・エイ, and **which one it is decides the
SIGN of every quantity in it** — 受注 removes stock, 返品 adds it. Nothing in
the upload form says which is which; the operator picks a file and sends it. So
the kind is read out of the data (`メッセージ種`, docs/29 §3), cross-checked
against two independent signals, and any disagreement stops the whole file.
Guessing here does not lose a row, it moves stock the wrong way.

What the format actually is, measured against the 2026-09-16 files (受注 167
columns / 335 rows, 返品 170 columns / 1,887 rows):

* CP932, header on line 1, every cell double-quoted.
* Denormalized: a voucher's own fields (発注者コード, the date, the destination)
  repeat on each of its detail lines. One file holds many vouchers — 21 and 79
  respectively.
* **Columns are matched by name, never by position.** The two files share most
  of their columns but not their offsets: `処理種別` is column 83 in 受注 and 85
  in 返品, and the quantity columns differ outright.
* The date lives in a different column per kind. 受注 fills `発注日` and leaves
  `計上日` empty on all 335 rows; 返品 does the exact opposite. There is no one
  column to read.
* `商品名カナ` is not a name. It holds the 規格カナ (`IT-N63-GD`) on some rows
  and an abbreviated product name (`316L anchor br`) on others, so nothing here
  reads it — docs/29 §5-1 bans it as a key and this is why.

Keys are zero-padded text: 取引番号 `000109900`, 取引明細番号 `01`, 発注者コード
`0393`. Excel strips those zeros on save, and the stripped values still parse
perfectly — so the damage is invisible unless something looks for it.
`_padding_fault` is that something. A broken 取引番号 breaks the idempotency key,
which is the one thing standing between a re-upload and double-counted stock
(docs/29 §7-3), so suspected Excel damage is fatal for the whole file rather
than per row: no partial import can be made safe afterwards.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.csv_intake import (
    EMPTY_FILE_MESSAGE,
    MISSING_COLUMNS_PREFIX,
    NO_HEADER_MESSAGE,
    ColumnSpec,
    CsvDecodeError,
    CsvSpec,
    decode_csv,
    resolve_header,
)

# ---------------------------------------------------------------- column names

COL_MESSAGE_TYPE = "メッセージ種"
COL_PROCESS_TYPE = "処理種別"
COL_BUYER = "発注者コード"
COL_VOUCHER = "取引番号（発注・返品）"
COL_VOUCHER_LINE = "取引明細番号（発注・返品）"
COL_ORIGINAL_VOUCHER = "元取引番号"
COL_PRODUCT_CODE = "商品コード（発注用）"
COL_SPEC_KANA = "規格カナ"
COL_PRODUCT_NAME = "商品名"
COL_UNIT_COST = "原単価"
COL_DESTINATION = "最終納品先コード"
COL_DESTINATION_NAME = "最終納品先名称"
COL_CREATED_AT = "作成日時"

COL_ORDER_DATE = "発注日"
COL_ACCOUNTING_DATE = "計上日"
COL_SHIPPED_QTY = "出荷数量（バラ）"
COL_RECEIVED_QTY = "受領数量（バラ）"
COL_RETURNED_QTY = "返品数量（バラ）"


class BmsKind(StrEnum):
    """Which of the two files this is, and therefore which way stock moves."""

    #: 受注伝票. `メッセージ種` 01 — stock leaves us.
    ORDER = "order"
    #: 受領・返品. `メッセージ種` 08 — stock comes back.
    RETURN = "return"


@dataclass(frozen=True, slots=True)
class KindRules:
    """Everything that differs between the two files, in one place.

    Written as data rather than as branches through the parser because the whole
    risk of this module is the two kinds being handled almost-but-not-quite
    symmetrically. Here the asymmetries sit side by side where they can be read.
    """

    message_type: str
    #: `処理種別`, docs/29 §3's second signal. Checked, never trusted alone.
    process_type: str
    #: The column this kind's quantity comes from. Its ABSENCE from the header is
    #: fatal: a file calling itself 受注 that carries no 出荷数量 column is not a
    #: 受注 file, whatever its メッセージ種 says.
    quantity_column: str
    #: The other kind's quantity columns — docs/29 §3's third signal. Each must
    #: be blank or zero on every row. The spec says those columns hold "0"; the
    #: real 返品 file leaves them EMPTY, so blank has to count as zero here or
    #: every genuine file is rejected.
    counter_quantity_columns: tuple[str, ...]
    #: Where this kind keeps its date. The other kind's date column is present in
    #: the header and empty on every row, so reading the wrong one yields 1,887
    #: "the date is blank" issues rather than anything diagnostic.
    date_column: str
    label: str


RULES: dict[BmsKind, KindRules] = {
    BmsKind.ORDER: KindRules(
        message_type="01",
        process_type="01",
        quantity_column=COL_SHIPPED_QTY,
        counter_quantity_columns=(COL_RETURNED_QTY,),
        date_column=COL_ORDER_DATE,
        label="受注",
    ),
    BmsKind.RETURN: KindRules(
        message_type="08",
        process_type="02",
        quantity_column=COL_RETURNED_QTY,
        counter_quantity_columns=(COL_SHIPPED_QTY, COL_RECEIVED_QTY),
        date_column=COL_ACCOUNTING_DATE,
        label="返品",
    ),
}

_KIND_BY_MESSAGE_TYPE = {rules.message_type: kind for kind, rules in RULES.items()}

#: Zero-padded identifiers and the width they are padded to. A value that is all
#: digits but SHORTER than this is Excel damage, not a new numbering scheme:
#: every one of the 2,222 rows measured 2026-09-16 was padded to exactly these
#: widths. Longer is accepted — the carrier may widen a field one day, and
#: rejecting a longer number would turn a good file away for no gain.
_PADDED_WIDTHS = {
    COL_VOUCHER: 9,
    COL_VOUCHER_LINE: 2,
    COL_BUYER: 4,
    COL_DESTINATION: 4,
}

#: 商品コード（発注用）is a GTIN-13 and is the mapping key (docs/29 §5-1). Excel
#: renders it as `2.70000243044E+12`, which is neither 13 long nor all digits.
_PRODUCT_CODE_WIDTH = 13

_EXCEL_HINT = (
    "Excelで開いて保存すると先頭の0が失われます。"
    "出力されたファイルをそのままアップロードしてください。"
)


def _shared_columns() -> tuple[ColumnSpec, ...]:
    """Columns both files carry.

    `required` here is about the HEADER, not about any row's value — a missing
    column rejects the upload, a blank cell is judged per row further down.
    Identifiers and the price are required because nothing downstream works
    without them; the descriptive columns are not, so a carrier that drops
    `最終納品先名称` costs us a label rather than the whole import.
    """
    required = (
        COL_MESSAGE_TYPE,
        COL_PROCESS_TYPE,
        COL_BUYER,
        COL_VOUCHER,
        COL_VOUCHER_LINE,
        COL_PRODUCT_CODE,
        COL_UNIT_COST,
    )
    optional = (
        COL_SPEC_KANA,
        COL_PRODUCT_NAME,
        COL_ORIGINAL_VOUCHER,
        COL_DESTINATION,
        COL_DESTINATION_NAME,
        COL_CREATED_AT,
    )
    return tuple(ColumnSpec(canonical=name) for name in required) + tuple(
        ColumnSpec(canonical=name, required=False) for name in optional
    )


#: Used for exactly two things: choosing the encoding, and finding `メッセージ種`
#: so the kind can be decided. Rows are read with the per-kind spec below.
#: Column validators are deliberately absent — csv_intake's validators drop a
#: row, and every rejection here has to name its line and its reason.
BMS_BASE_SPEC = CsvSpec(
    columns=_shared_columns(),
    encoding_message="文字コードを判別できませんでした。流通BMSのファイルはCP932です。",
    comment_prefix="",
)


def spec_for(kind: BmsKind) -> CsvSpec:
    """The columns a file of this kind must carry."""
    rules = RULES[kind]
    return CsvSpec(
        columns=(
            *_shared_columns(),
            ColumnSpec(canonical=rules.quantity_column),
            ColumnSpec(canonical=rules.date_column),
            *(ColumnSpec(canonical=c, required=False) for c in rules.counter_quantity_columns),
        ),
        encoding_message=BMS_BASE_SPEC.encoding_message,
        comment_prefix="",
    )


# ---------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class BmsLine:
    """One detail line, with the voucher's repeated fields stripped off."""

    line_no: int  # 1-based file line — the number the operator sees in Excel
    voucher: str
    voucher_line: str
    product_code: str
    spec_kana: str
    product_name: str
    #: Always > 0. A zero-quantity line never becomes a BmsLine; see
    #: `BmsReading.zero_quantity_rows`.
    quantity: int
    unit_cost: Decimal
    #: `元取引番号`. Empty on all 1,887 return rows measured, which is why
    #: docs/29 §4 treats a return as an independent addition rather than the
    #: reversal of a shipment. Carried anyway, so the day it stops being empty is
    #: something we can see instead of something we assumed.
    original_voucher: str = ""


@dataclass(frozen=True, slots=True)
class BmsVoucher:
    """One 取引番号 and its lines — what becomes one `orders` row."""

    kind: BmsKind
    voucher: str
    buyer_code: str
    occurred_on: date
    destination_code: str
    destination_name: str
    created_at_raw: str
    lines: tuple[BmsLine, ...]

    @property
    def total_quantity(self) -> int:
        return sum(line.quantity for line in self.lines)


@dataclass(slots=True)
class BmsReading:
    """What one uploaded file turned out to be.

    A `fatal` entry means nothing may be imported from this file. `row_issues`
    are lines an operator can fix: they are skipped and the rest still imports,
    following docs/29 §8-1 — stopping a 48-line voucher over one bad line is how
    a day of wholesale movement goes unrecorded.
    """

    kind: BmsKind | None = None
    vouchers: tuple[BmsVoucher, ...] = ()
    fatal: list[str] = field(default_factory=list)
    row_issues: list[dict[str, Any]] = field(default_factory=list)
    total_rows: int = 0
    valid_rows: int = 0
    #: Lines whose quantity was 0 or blank. Counted, not reported as an issue:
    #: one such row exists in the 2026-09-16 返品 file (取引番号 000171243,
    #: 明細 02) and it means "nothing came back on this line", not an operator
    #: mistake. Same reasoning as `IngestResult.unmanaged_skipped` — "nothing,
    #: deliberately" and "we do not know what this is" must not share a counter,
    #: or a healthy file reads as a backlog.
    zero_quantity_rows: int = 0
    #: Return lines that DID name an original voucher. Still zero as of
    #: 2026-09-16; if that changes, returns can be tied to their shipment and
    #: docs/29 §4 needs revisiting.
    linked_original_rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.fatal

    @property
    def total_quantity(self) -> int:
        return sum(v.total_quantity for v in self.vouchers)

    def as_dict(self) -> dict[str, Any]:
        """Template-friendly shape, matching `csv_intake.Inspection.as_dict`."""
        return {
            "kind": str(self.kind) if self.kind is not None else None,
            "kind_label": RULES[self.kind].label if self.kind is not None else None,
            "fatal": self.fatal,
            "row_issues": self.row_issues,
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "voucher_count": len(self.vouchers),
            "total_quantity": self.total_quantity,
            "zero_quantity_rows": self.zero_quantity_rows,
            "linked_original_rows": self.linked_original_rows,
        }


#: Cap on reported row issues, so a wholly malformed file cannot flood the page.
#: Same intent as `CsvSpec.max_row_issues`.
MAX_ROW_ISSUES = 50

NO_DATA_MESSAGE = "データ行がありません。"
UNKNOWN_KIND_PREFIX = "メッセージ種を判別できません: "
MIXED_KIND_PREFIX = "1つのファイルに受注と返品が混在しています: "


# ----------------------------------------------------------------------- reader


def read_bms(data: bytes) -> BmsReading:
    """Read one uploaded 流通BMS file.

    Never raises on bad input: everything the operator needs to know comes back
    on the `BmsReading`, because this is the 検証 step of
    upload → 検証 → 確認 → 実行 and a traceback is not a screen.
    """
    reading = BmsReading()
    if not data:
        reading.fatal.append(EMPTY_FILE_MESSAGE)
        return reading
    try:
        text = decode_csv(data, BMS_BASE_SPEC)
    except CsvDecodeError as exc:
        reading.fatal.append(str(exc))
        return reading

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        reading.fatal.append(EMPTY_FILE_MESSAGE)
        return reading

    # The header is line 1, full stop. csv_intake searches the first ten lines
    # for it because the sheets we hand staff come back with a title row or a
    # spacer on top. This file is machine output that nobody is meant to open
    # (docs/29 §7-3), so a header anywhere but line 1 means it HAS been opened
    # and edited — the one condition this reader exists to refuse.
    header = rows[0]
    if not any(cell.strip() for cell in header):
        reading.fatal.append(NO_HEADER_MESSAGE)
        return reading
    # Only `メッセージ種` is needed yet: until the kind is known there is no
    # saying which quantity or date column the file owes us, so the full
    # missing-column report waits for the per-kind spec below.
    base_index, _ = resolve_header(header, BMS_BASE_SPEC)
    if COL_MESSAGE_TYPE not in base_index:
        reading.fatal.append(MISSING_COLUMNS_PREFIX + COL_MESSAGE_TYPE)
        return reading

    body = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    if not body:
        reading.fatal.append(NO_DATA_MESSAGE)
        return reading

    kind = _decide_kind(body, base_index[COL_MESSAGE_TYPE], reading)
    if kind is None:
        return reading
    reading.kind = kind

    index, missing = resolve_header(header, spec_for(kind))
    if missing:
        # Reported against the kind, because "出荷数量（バラ）がありません" alone
        # reads as a corrupt file when the real story is usually a 返品 file
        # whose メッセージ種 was somehow 01.
        reading.fatal.append(
            f"{RULES[kind].label}として読みましたが" + MISSING_COLUMNS_PREFIX + " / ".join(missing)
        )
        return reading

    _read_lines(body, index, kind, reading)
    return reading


def _decide_kind(body: list[list[str]], position: int, reading: BmsReading) -> BmsKind | None:
    """Decide the kind from `メッセージ種`, requiring every row to agree.

    docs/29 §3 makes this column the authority. A file carrying both values is
    rejected rather than split: the two kinds move stock in opposite directions,
    so a mixed file is one we do not understand, and importing the half we think
    we do would be a guess with a sign on it.
    """
    seen: dict[str, int] = {}
    for offset, row in enumerate(body):
        value = row[position].strip() if position < len(row) else ""
        seen.setdefault(value, offset + 2)
    unknown = sorted(v for v in seen if v not in _KIND_BY_MESSAGE_TYPE)
    if unknown:
        reading.fatal.append(
            UNKNOWN_KIND_PREFIX
            + " / ".join(f"{v or '(空欄)'} ({seen[v]}行目)" for v in unknown)
            + f"。受注は{RULES[BmsKind.ORDER].message_type}、"
            + f"返品は{RULES[BmsKind.RETURN].message_type}です。"
        )
        return None
    if len(seen) > 1:
        reading.fatal.append(
            MIXED_KIND_PREFIX
            + " / ".join(
                f"{RULES[_KIND_BY_MESSAGE_TYPE[v]].label}={v} ({seen[v]}行目)" for v in sorted(seen)
            )
        )
        return None
    return _KIND_BY_MESSAGE_TYPE[next(iter(seen))]


def _read_lines(
    body: list[list[str]],
    index: dict[str, int],
    kind: BmsKind,
    reading: BmsReading,
) -> None:
    rules = RULES[kind]
    widest = max(index.values(), default=-1)
    padding_faults: dict[str, str] = {}
    # Insertion-ordered, so vouchers come back in the order the file lists them
    # and the confirmation screen reads like the file the operator is holding.
    grouped: dict[str, list[BmsLine]] = {}
    headers: dict[str, dict[str, str]] = {}
    header_conflicts: list[str] = []

    for offset, row in enumerate(body):
        lineno = offset + 2  # line 1 is the header
        reading.total_rows += 1
        if len(row) <= widest:
            _issue(reading, lineno, f"列数が不足しています（{len(row)}列）")
            continue
        cell = {name: row[pos].strip() for name, pos in index.items()}

        if cell[COL_PROCESS_TYPE] != rules.process_type:
            # docs/29 §3: the three signals must agree, and a disagreement stops
            # the file. Fatal rather than a row issue for the same reason the
            # mixed-kind case is — getting the kind wrong inverts the sign.
            reading.fatal.append(
                f"{lineno}行目: メッセージ種は{rules.label}（{rules.message_type}）ですが"
                f"処理種別が{cell[COL_PROCESS_TYPE] or '(空欄)'}です"
                f"（{rules.label}は{rules.process_type}）。"
                "種別の取り違えは在庫を逆方向に動かすため取込を中止します。"
            )
            return
        for counter in rules.counter_quantity_columns:
            if counter in cell and _as_int(cell[counter]) not in (None, 0):
                reading.fatal.append(
                    f"{lineno}行目: {rules.label}のファイルに{counter}の値"
                    f"（{cell[counter]}）があります。"
                    "種別の取り違えは在庫を逆方向に動かすため取込を中止します。"
                )
                return

        for column, width in _PADDED_WIDTHS.items():
            if column in cell:
                fault = _padding_fault(cell[column], width)
                if fault is not None and column not in padding_faults:
                    padding_faults[column] = f"{lineno}行目: {column}「{cell[column]}」{fault}"

        # Blank means zero in a COUNTER column — that is how the real 返品 file
        # writes "nothing shipped here" — but blank in this kind's OWN quantity
        # column is a missing number, not a zero. It is the one value the row
        # exists to carry, and it was filled on all 2,222 rows measured, so a
        # blank one is reported rather than quietly treated as no movement.
        raw_quantity = cell[rules.quantity_column]
        quantity = _as_int(raw_quantity) if raw_quantity else None
        if quantity is None:
            _issue(
                reading,
                lineno,
                f"{rules.quantity_column}が数値ではありません（{raw_quantity or '(空欄)'}）",
            )
            continue
        if quantity < 0:
            # Never observed. A negative shipment would mean "add stock" on the
            # 受注 path, which is the sign error this whole module guards, so it
            # is refused rather than negated into something plausible.
            _issue(
                reading,
                lineno,
                f"{rules.quantity_column}が負の数です（{quantity}）",
            )
            continue
        if quantity == 0:
            reading.zero_quantity_rows += 1
            continue

        unit_cost = _as_decimal(cell[COL_UNIT_COST])
        if unit_cost is None:
            # 原単価 is our own revenue on this line (docs/29 §6); 売単価 is the
            # retail price and is not ours. A line imported with no 原単価 would
            # move stock correctly and understate sales silently, so the line
            # waits for a corrected file.
            _issue(reading, lineno, f"原単価が数値ではありません（{cell[COL_UNIT_COST]}）")
            continue

        occurred_on = _as_date(cell[rules.date_column])
        if occurred_on is None:
            _issue(
                reading,
                lineno,
                f"{rules.date_column}が YYYYMMDD 形式ではありません"
                f"（{cell[rules.date_column] or '(空欄)'}）",
            )
            continue

        voucher = cell[COL_VOUCHER]
        product_code = cell[COL_PRODUCT_CODE]
        if not _is_product_code(product_code):
            _issue(
                reading,
                lineno,
                f"商品コード（発注用）が{_PRODUCT_CODE_WIDTH}桁の数字ではありません"
                f"（{product_code or '(空欄)'}）。{_EXCEL_HINT}",
            )
            continue

        voucher_header = {
            COL_BUYER: cell[COL_BUYER],
            rules.date_column: cell[rules.date_column],
            COL_DESTINATION: cell.get(COL_DESTINATION, ""),
            # Labels, not identity. A carrier that renames a store mid-file
            # should not fail the import, so these stay out of the conflict
            # check below and the voucher takes whichever line it met first.
            COL_DESTINATION_NAME: cell.get(COL_DESTINATION_NAME, ""),
            COL_CREATED_AT: cell.get(COL_CREATED_AT, ""),
        }
        known = headers.setdefault(voucher, voucher_header)
        conflicting = [
            key
            for key in (COL_BUYER, rules.date_column, COL_DESTINATION)
            if known.get(key) != voucher_header[key]
        ]
        if conflicting:
            # One voucher becomes one `orders` row, which can hold only one date,
            # one buyer and one destination. Every one of the 100 vouchers
            # measured carries a single value for each, so a conflict means the
            # file is not shaped the way this import assumes — worth stopping
            # over rather than resolving by picking the first row and hoping.
            header_conflicts.append(
                f"{lineno}行目: 取引番号{voucher}の"
                + " / ".join(
                    f"{k}が{known.get(k) or '(空欄)'}と{voucher_header[k] or '(空欄)'}で"
                    "一致しません"
                    for k in conflicting
                )
            )
            continue

        original = cell.get(COL_ORIGINAL_VOUCHER, "")
        if original:
            reading.linked_original_rows += 1
        grouped.setdefault(voucher, []).append(
            BmsLine(
                line_no=lineno,
                voucher=voucher,
                voucher_line=cell[COL_VOUCHER_LINE],
                product_code=product_code,
                spec_kana=cell.get(COL_SPEC_KANA, ""),
                product_name=cell.get(COL_PRODUCT_NAME, ""),
                quantity=quantity,
                unit_cost=unit_cost,
                original_voucher=original,
            )
        )
        reading.valid_rows += 1

    if padding_faults:
        # One cause, one remedy: the file was opened in Excel and saved. Reported
        # once per column with the first line that showed it, rather than 2,222
        # times, and fatal because a damaged 取引番号 is a damaged idempotency
        # key — a later re-upload of the correct file would then count twice.
        reading.fatal.extend(padding_faults[c] for c in _PADDED_WIDTHS if c in padding_faults)
        reading.fatal.append(_EXCEL_HINT)
        return
    if header_conflicts:
        reading.fatal.extend(header_conflicts)
        return

    duplicate = _duplicate_line_keys(grouped)
    if duplicate:
        # (取引番号, 取引明細番号) is the idempotency key (docs/29 §4) and was
        # unique across all 2,222 rows. A repeat inside one file means the key
        # cannot tell a genuine second line from a duplicated one, so the file
        # stops here instead of importing whichever of the two came last.
        reading.fatal.append(
            "取引番号と取引明細番号が重複しています: "
            + " / ".join(duplicate)
            + "。冪等性キーが一意でないため取込を中止します。"
        )
        return

    reading.vouchers = tuple(
        BmsVoucher(
            kind=kind,
            voucher=voucher,
            buyer_code=headers[voucher][COL_BUYER],
            # Already validated for every line of this voucher, and every line of
            # a voucher carries the same date, so the first line speaks for it.
            occurred_on=_require_date(headers[voucher][rules.date_column]),
            destination_code=headers[voucher][COL_DESTINATION],
            destination_name=headers[voucher][COL_DESTINATION_NAME],
            created_at_raw=headers[voucher][COL_CREATED_AT],
            lines=tuple(lines),
        )
        for voucher, lines in grouped.items()
    )


def _duplicate_line_keys(grouped: dict[str, list[BmsLine]]) -> list[str]:
    duplicate: list[str] = []
    for voucher, lines in grouped.items():
        seen: set[str] = set()
        for line in lines:
            if line.voucher_line in seen:
                duplicate.append(f"{line.line_no}行目: {voucher}-{line.voucher_line}")
            seen.add(line.voucher_line)
    return duplicate


# ------------------------------------------------------------------- primitives


def _issue(reading: BmsReading, lineno: int, reason: str) -> None:
    if len(reading.row_issues) < MAX_ROW_ISSUES:
        reading.row_issues.append({"line": lineno, "reason": reason})


def _as_int(value: str) -> int | None:
    """Blank counts as zero.

    docs/29 §3 describes the counter-kind quantity columns as holding "0"; in the
    real 返品 file they are empty on all 1,887 rows. Treating blank as a missing
    number instead would reject every genuine return file.
    """
    text = value.strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return None


def _as_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None


def _as_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def _require_date(value: str) -> date:
    parsed = _as_date(value)
    if parsed is None:  # pragma: no cover - every line was validated above
        raise ValueError(f"unparsable voucher date survived validation: {value!r}")
    return parsed


def _padding_fault(value: str, width: int) -> str | None:
    """Why this identifier looks damaged, or None when it is fine."""
    text = value.strip()
    if not text:
        return "が空欄です"
    if not text.isdecimal():
        return "が数字ではありません"
    if len(text) < width:
        return f"の桁数が{width}桁未満です"
    return None


def _is_product_code(value: str) -> bool:
    return len(value) == _PRODUCT_CODE_WIDTH and value.isdecimal()


__all__ = [
    "BMS_BASE_SPEC",
    "COL_ACCOUNTING_DATE",
    "COL_BUYER",
    "COL_MESSAGE_TYPE",
    "COL_ORDER_DATE",
    "COL_PROCESS_TYPE",
    "COL_PRODUCT_CODE",
    "COL_RETURNED_QTY",
    "COL_SHIPPED_QTY",
    "COL_VOUCHER",
    "COL_VOUCHER_LINE",
    "MAX_ROW_ISSUES",
    "RULES",
    "BmsKind",
    "BmsLine",
    "BmsReading",
    "BmsVoucher",
    "KindRules",
    "read_bms",
    "spec_for",
]
