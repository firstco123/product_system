"""流通BMS reader unit tests (P2-051).

Fixtures are built here rather than read from disk: the real files under
`csv_file/` are gitignored, so CI never sees them. One test at the bottom does
read them and skips when they are absent — it is the only thing that can catch
the carrier changing the format, and it must not be the only thing that runs.

The fixtures deliberately put the shared columns at DIFFERENT positions in the
two kinds, because that is true of the real files (`処理種別` is column 83 in
受注 and 85 in 返品) and matching by position would pass every other test here.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.wholesale_bms import (
    BmsKind,
    read_bms,
)

# --------------------------------------------------------------------- fixtures

_ORDER_COLUMNS = (
    "メッセージ種",
    "発注者コード",
    "取引番号（発注・返品）",
    "最終納品先コード",
    "最終納品先名称",
    "発注日",
    "計上日",
    "処理種別",
    "作成日時",
    "取引明細番号（発注・返品）",
    "元取引番号",
    "商品コード（発注用）",
    "商品名",
    "商品名カナ",
    "規格カナ",
    "原単価",
    "売単価",
    "発注数量（バラ）",
    "出荷数量（バラ）",
    "欠品数量(バラ数)",
)

#: Same names, shuffled, plus the return-only quantity columns — the real 返品
#: file has three more columns than the 受注 one and shares no offsets with it.
_RETURN_COLUMNS = (
    "メッセージ種",
    "発注者コード",
    "取引番号（発注・返品）",
    "商品移動区分",
    "最終納品先コード",
    "最終納品先名称",
    "発注日",
    "計上日",
    "返品・値引理由コード",
    "処理種別",
    "作成日時",
    "取引明細番号（発注・返品）",
    "元取引番号",
    "商品コード（発注用）",
    "商品名",
    "商品名カナ",
    "規格カナ",
    "原単価",
    "売単価",
    "出荷数量（バラ）",
    "受領数量（バラ）",
    "返品数量（バラ）",
)

_ORDER_DEFAULTS = {
    "メッセージ種": "01",
    "処理種別": "01",
    "発注者コード": "0393",
    "取引番号（発注・返品）": "000109900",
    "取引明細番号（発注・返品）": "01",
    "最終納品先コード": "0653",
    "最終納品先名称": "町田コレクターズ",
    "発注日": "20260914",
    "作成日時": "2026-09-14T12:05:59",
    "商品コード（発注用）": "2700002430598",
    "商品名": "３１６Ｌ　ａｎｃｈｏｒ　ｎｅｃｋｌａｃｅ",
    "商品名カナ": "IT-N63-GD",
    "規格カナ": "IT-N63-GD",
    "原単価": "7898",
    "売単価": "14364",
    "発注数量（バラ）": "1",
    "出荷数量（バラ）": "1",
    "欠品数量(バラ数)": "0",
}

_RETURN_DEFAULTS = {
    "メッセージ種": "08",
    "処理種別": "02",
    "商品移動区分": "01",
    "返品・値引理由コード": "001",
    "発注者コード": "0393",
    "取引番号（発注・返品）": "000171243",
    "取引明細番号（発注・返品）": "01",
    "最終納品先コード": "0653",
    "最終納品先名称": "町田コレクターズ",
    "計上日": "20260909",
    "作成日時": "2026-09-10T02:02:32",
    "商品コード（発注用）": "2700002488353",
    "商品名": "３１６Ｌ　ｃｕｔ　ｃｈａｉｎ　ｂｒａｃｅ",
    "商品名カナ": "IT-B51-SV",
    "規格カナ": "IT-B51-SV",
    "原単価": "4719",
    "売単価": "8580",
    # The real file leaves these EMPTY, although docs/29 §3 describes them as 0.
    "出荷数量（バラ）": "",
    "受領数量（バラ）": "",
    "返品数量（バラ）": "3",
}


def _build(columns: tuple[str, ...], defaults: dict[str, str], *rows: dict[str, str]) -> bytes:
    """One BMS file: CP932, header on line 1, every cell quoted."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(columns)
    for overrides in rows or ({},):
        merged = {**defaults, **overrides}
        writer.writerow([merged.get(name, "") for name in columns])
    return buffer.getvalue().encode("cp932")


def order_file(*rows: dict[str, str]) -> bytes:
    return _build(_ORDER_COLUMNS, _ORDER_DEFAULTS, *rows)


def return_file(*rows: dict[str, str]) -> bytes:
    return _build(_RETURN_COLUMNS, _RETURN_DEFAULTS, *rows)


# ------------------------------------------------------------------ the two kinds


@pytest.mark.unit
def test_an_order_file_reads_the_shipped_quantity_and_the_order_date() -> None:
    reading = read_bms(order_file({"出荷数量（バラ）": "2", "発注数量（バラ）": "2"}))

    assert reading.ok, reading.fatal
    assert reading.kind is BmsKind.ORDER
    assert reading.row_issues == []
    (voucher,) = reading.vouchers
    assert voucher.voucher == "000109900"
    assert voucher.buyer_code == "0393"
    # 発注日, not 計上日 — the order file leaves 計上日 empty on every row.
    assert voucher.occurred_on == date(2026, 9, 14)
    assert voucher.destination_code == "0653"
    assert voucher.destination_name == "町田コレクターズ"
    (line,) = voucher.lines
    assert line.quantity == 2
    assert line.unit_cost == Decimal("7898")
    assert line.product_code == "2700002430598"
    assert line.spec_kana == "IT-N63-GD"
    assert line.line_no == 2


@pytest.mark.unit
def test_a_return_file_reads_the_returned_quantity_and_the_accounting_date() -> None:
    reading = read_bms(return_file({"返品数量（バラ）": "5"}))

    assert reading.ok, reading.fatal
    assert reading.kind is BmsKind.RETURN
    (voucher,) = reading.vouchers
    # 計上日, not 発注日 — which is empty on every row of the real return file.
    assert voucher.occurred_on == date(2026, 9, 9)
    (line,) = voucher.lines
    assert line.quantity == 5


@pytest.mark.unit
def test_the_order_quantity_comes_from_shipped_not_ordered() -> None:
    """docs/29 §4: 発注数量 is what they asked for, 出荷数量 is what left.

    They agreed on all 335 rows measured, so reading the wrong one would look
    correct until the first short shipment.
    """
    reading = read_bms(order_file({"発注数量（バラ）": "9", "出荷数量（バラ）": "4"}))

    assert reading.ok, reading.fatal
    assert reading.vouchers[0].lines[0].quantity == 4


@pytest.mark.unit
def test_blank_counter_quantities_are_not_a_disagreement() -> None:
    """The guard that would reject every genuine return file if written
    literally from docs/29 §3, which says 出荷数量・受領数量 hold "0"."""
    reading = read_bms(return_file({"出荷数量（バラ）": "", "受領数量（バラ）": ""}))

    assert reading.ok, reading.fatal
    assert reading.valid_rows == 1


@pytest.mark.unit
def test_zero_counter_quantities_are_not_a_disagreement_either() -> None:
    reading = read_bms(return_file({"出荷数量（バラ）": "0", "受領数量（バラ）": "0"}))

    assert reading.ok, reading.fatal


# --------------------------------------------------------- refusing to guess the kind


@pytest.mark.unit
def test_a_populated_counter_quantity_stops_the_file() -> None:
    reading = read_bms(return_file({"出荷数量（バラ）": "3", "返品数量（バラ）": "3"}))

    assert not reading.ok
    assert "出荷数量（バラ）" in reading.fatal[0]
    assert reading.vouchers == ()


@pytest.mark.unit
def test_a_process_type_that_contradicts_the_message_type_stops_the_file() -> None:
    reading = read_bms(order_file({"処理種別": "02"}))

    assert not reading.ok
    assert "処理種別" in reading.fatal[0]
    assert "2行目" in reading.fatal[0]
    assert reading.vouchers == ()


@pytest.mark.unit
def test_a_file_holding_both_kinds_is_refused_rather_than_split() -> None:
    reading = read_bms(
        order_file(
            {"取引明細番号（発注・返品）": "01"},
            {"取引明細番号（発注・返品）": "02", "メッセージ種": "08"},
        )
    )

    assert not reading.ok
    assert "混在" in reading.fatal[0]
    assert "受注=01" in reading.fatal[0]
    assert "返品=08" in reading.fatal[0]
    assert reading.vouchers == ()


@pytest.mark.unit
def test_an_unrecognised_message_type_stops_the_file() -> None:
    reading = read_bms(order_file({"メッセージ種": "11"}))

    assert not reading.ok
    assert "メッセージ種を判別できません" in reading.fatal[0]
    assert "11 (2行目)" in reading.fatal[0]


@pytest.mark.unit
def test_a_blank_message_type_stops_the_file() -> None:
    reading = read_bms(order_file({"メッセージ種": ""}))

    assert not reading.ok
    assert "(空欄)" in reading.fatal[0]


@pytest.mark.unit
def test_a_return_message_type_without_the_return_columns_names_the_kind() -> None:
    """A 返品 メッセージ種 on a 受注 layout. Neither signal can be trusted, so
    the error says which kind it tried to read rather than only which column it
    could not find."""
    reading = read_bms(order_file({"メッセージ種": "08", "処理種別": "02"}))

    assert not reading.ok
    assert "返品として読みましたが" in reading.fatal[0]
    assert "返品数量（バラ）" in reading.fatal[0]


# ----------------------------------------------------------------- Excel damage


@pytest.mark.unit
def test_a_voucher_number_stripped_of_its_leading_zeros_stops_the_file() -> None:
    """docs/29 §7-3. `000109900` saved by Excel becomes `109900`, which parses
    perfectly and silently changes the idempotency key."""
    reading = read_bms(order_file({"取引番号（発注・返品）": "109900"}))

    assert not reading.ok
    assert "取引番号（発注・返品）" in reading.fatal[0]
    assert "9桁未満" in reading.fatal[0]
    assert any("Excel" in message for message in reading.fatal)
    assert reading.vouchers == ()


@pytest.mark.unit
def test_a_line_number_stripped_of_its_leading_zero_stops_the_file() -> None:
    reading = read_bms(order_file({"取引明細番号（発注・返品）": "1"}))

    assert not reading.ok
    assert "取引明細番号（発注・返品）" in reading.fatal[0]


@pytest.mark.unit
def test_a_buyer_code_stripped_of_its_leading_zero_stops_the_file() -> None:
    """`0393` is the marketplace_id every wholesale mapping is keyed on
    (docs/29 §5-1). As `393` nothing would resolve, and every line would look
    like an unmapped product rather than a damaged file."""
    reading = read_bms(order_file({"発注者コード": "393"}))

    assert not reading.ok
    assert "発注者コード" in reading.fatal[0]


@pytest.mark.unit
def test_padding_damage_is_reported_once_per_column_not_once_per_row() -> None:
    reading = read_bms(
        order_file(
            {"取引明細番号（発注・返品）": "1", "取引番号（発注・返品）": "109900"},
            {"取引明細番号（発注・返品）": "2", "取引番号（発注・返品）": "109900"},
            {"取引明細番号（発注・返品）": "3", "取引番号（発注・返品）": "109900"},
        )
    )

    assert not reading.ok
    # Two damaged columns + the one shared remedy, however many rows carried it.
    assert len(reading.fatal) == 3
    assert reading.fatal[0].startswith("2行目")


@pytest.mark.unit
def test_a_product_code_in_scientific_notation_is_a_row_issue_not_a_fatal() -> None:
    """Excel renders the 13-digit GTIN as `2.70000243044E+12`. Unlike the
    voucher number this does not break the idempotency key, so the line is
    reported and the rest of the voucher still imports (docs/29 §8-1)."""
    reading = read_bms(
        order_file(
            {"取引明細番号（発注・返品）": "01", "商品コード（発注用）": "2.70000243044E+12"},
            {"取引明細番号（発注・返品）": "02"},
        )
    )

    assert reading.ok, reading.fatal
    assert reading.total_rows == 2
    assert reading.valid_rows == 1
    assert reading.row_issues[0]["line"] == 2
    assert "商品コード（発注用）" in reading.row_issues[0]["reason"]
    (voucher,) = reading.vouchers
    assert [line.voucher_line for line in voucher.lines] == ["02"]


# ------------------------------------------------------------- rows and vouchers


@pytest.mark.unit
def test_a_zero_quantity_line_is_counted_rather_than_reported() -> None:
    """One such row exists in the 2026-09-16 返品 file. It means nothing came
    back on that line, so it is not an operator error — and it must not become a
    quantity=0 inventory event either."""
    reading = read_bms(
        return_file(
            {"取引明細番号（発注・返品）": "01", "返品数量（バラ）": "0"},
            {"取引明細番号（発注・返品）": "02", "返品数量（バラ）": "2"},
        )
    )

    assert reading.ok, reading.fatal
    assert reading.row_issues == []
    assert reading.zero_quantity_rows == 1
    assert reading.total_rows == 2
    assert reading.valid_rows == 1
    assert reading.total_quantity == 2


@pytest.mark.unit
def test_a_blank_own_quantity_is_reported_even_though_blank_counters_are_not() -> None:
    reading = read_bms(return_file({"返品数量（バラ）": ""}))

    assert reading.ok, reading.fatal
    assert reading.valid_rows == 0
    assert reading.zero_quantity_rows == 0
    assert "(空欄)" in reading.row_issues[0]["reason"]


@pytest.mark.unit
def test_a_negative_quantity_is_refused_rather_than_negated() -> None:
    reading = read_bms(order_file({"出荷数量（バラ）": "-2"}))

    assert reading.ok, reading.fatal
    assert reading.valid_rows == 0
    assert "負の数" in reading.row_issues[0]["reason"]


@pytest.mark.unit
def test_a_line_with_no_unit_cost_waits_for_a_corrected_file() -> None:
    """原単価 is our revenue on the line (docs/29 §6). Importing without it
    would move stock correctly and understate sales with nothing to show why."""
    reading = read_bms(order_file({"原単価": ""}))

    assert reading.ok, reading.fatal
    assert reading.valid_rows == 0
    assert "原単価" in reading.row_issues[0]["reason"]


@pytest.mark.unit
def test_lines_group_into_vouchers_in_file_order() -> None:
    reading = read_bms(
        order_file(
            {"取引番号（発注・返品）": "000109900", "取引明細番号（発注・返品）": "01"},
            {
                "取引番号（発注・返品）": "000109900",
                "取引明細番号（発注・返品）": "02",
                "出荷数量（バラ）": "3",
            },
            {
                "取引番号（発注・返品）": "000109765",
                "取引明細番号（発注・返品）": "01",
                "発注日": "20260913",
                "最終納品先コード": "0660",
                "最終納品先名称": "川崎コレクターズ",
            },
        )
    )

    assert reading.ok, reading.fatal
    assert [v.voucher for v in reading.vouchers] == ["000109900", "000109765"]
    assert reading.vouchers[0].total_quantity == 4
    assert reading.vouchers[1].occurred_on == date(2026, 9, 13)
    assert reading.vouchers[1].destination_name == "川崎コレクターズ"


@pytest.mark.unit
def test_one_voucher_carrying_two_dates_stops_the_file() -> None:
    """A voucher becomes one `orders` row, which holds one date. All 100
    vouchers measured carry a single date, so two means the file is not shaped
    the way this import assumes — better to stop than to pick one."""
    reading = read_bms(
        order_file(
            {"取引明細番号（発注・返品）": "01", "発注日": "20260914"},
            {"取引明細番号（発注・返品）": "02", "発注日": "20260913"},
        )
    )

    assert not reading.ok
    assert "取引番号000109900" in reading.fatal[0]
    assert "発注日" in reading.fatal[0]


@pytest.mark.unit
def test_one_voucher_delivered_to_two_destinations_stops_the_file() -> None:
    reading = read_bms(
        order_file(
            {"取引明細番号（発注・返品）": "01", "最終納品先コード": "0653"},
            {"取引明細番号（発注・返品）": "02", "最終納品先コード": "0660"},
        )
    )

    assert not reading.ok
    assert "最終納品先コード" in reading.fatal[0]


@pytest.mark.unit
def test_a_renamed_destination_does_not_stop_the_file() -> None:
    """The name is a label; the code is the identity. A carrier that spells a
    store differently on two lines should cost us a label, not the import."""
    reading = read_bms(
        order_file(
            {"取引明細番号（発注・返品）": "01", "最終納品先名称": "町田コレクターズ"},
            {"取引明細番号（発注・返品）": "02", "最終納品先名称": "町田ｺﾚｸﾀｰｽﾞ"},
        )
    )

    assert reading.ok, reading.fatal
    assert reading.vouchers[0].destination_name == "町田コレクターズ"


@pytest.mark.unit
def test_a_repeated_voucher_line_number_stops_the_file() -> None:
    """(取引番号, 取引明細番号) is the idempotency key. If it repeats inside one
    file it cannot tell a real second line from a duplicated one."""
    reading = read_bms(
        order_file(
            {"取引明細番号（発注・返品）": "01"},
            {"取引明細番号（発注・返品）": "01"},
        )
    )

    assert not reading.ok
    assert "重複" in reading.fatal[0]
    assert "000109900-01" in reading.fatal[0]
    assert reading.vouchers == ()


@pytest.mark.unit
def test_an_original_voucher_number_is_counted_so_the_assumption_can_expire() -> None:
    """docs/29 §4 treats returns as independent additions because 元取引番号 was
    empty on all 1,887 rows. If it ever arrives, that is a finding, not noise."""
    reading = read_bms(return_file({"元取引番号": "000109900"}))

    assert reading.ok, reading.fatal
    assert reading.linked_original_rows == 1
    assert reading.vouchers[0].lines[0].original_voucher == "000109900"


@pytest.mark.unit
def test_a_date_that_is_not_yyyymmdd_is_a_row_issue() -> None:
    reading = read_bms(order_file({"発注日": "2026/09/14"}))

    assert reading.ok, reading.fatal
    assert reading.valid_rows == 0
    assert "発注日" in reading.row_issues[0]["reason"]


# --------------------------------------------------------------- shapeless input


@pytest.mark.unit
def test_an_empty_upload_is_reported_not_raised() -> None:
    reading = read_bms(b"")

    assert not reading.ok
    assert reading.kind is None


@pytest.mark.unit
def test_a_header_with_no_data_rows_is_reported() -> None:
    data = order_file()
    header_only = data.split(b"\r\n")[0] + b"\r\n"

    reading = read_bms(header_only)

    assert not reading.ok
    assert "データ行がありません" in reading.fatal[0]


@pytest.mark.unit
def test_bytes_that_decode_as_nothing_are_reported_as_an_encoding_problem() -> None:
    reading = read_bms(b"\x81 \x81 ")

    assert not reading.ok
    assert "文字コード" in reading.fatal[0]
    assert "CP932" in reading.fatal[0]


@pytest.mark.unit
def test_a_blank_first_line_is_reported_as_a_missing_header() -> None:
    reading = read_bms(",,,\r\n01,0393,000109900\r\n".encode("cp932"))

    assert not reading.ok
    assert "ヘッダー行" in reading.fatal[0]


@pytest.mark.unit
def test_a_row_with_too_few_columns_is_reported_with_its_line_number() -> None:
    data = order_file({"取引明細番号（発注・返品）": "01"})
    truncated = data + '"01","0393"\r\n'.encode("cp932")

    reading = read_bms(truncated)

    assert reading.ok, reading.fatal
    assert reading.total_rows == 2
    assert reading.valid_rows == 1
    assert reading.row_issues == [{"line": 3, "reason": "列数が不足しています（2列）"}]


@pytest.mark.unit
def test_the_screen_shape_carries_the_kind_and_the_totals() -> None:
    """`as_dict` is what the 検証 step renders, so the counts the operator
    decides on come from here rather than from a second walk of the file."""
    reading = read_bms(
        return_file(
            {"取引明細番号（発注・返品）": "01", "返品数量（バラ）": "2"},
            {"取引明細番号（発注・返品）": "02", "返品数量（バラ）": "0"},
        )
    )

    assert reading.as_dict() == {
        "kind": "return",
        "kind_label": "返品",
        "fatal": [],
        "row_issues": [],
        "total_rows": 2,
        "valid_rows": 1,
        "voucher_count": 1,
        "total_quantity": 2,
        "zero_quantity_rows": 1,
        "linked_original_rows": 0,
    }


@pytest.mark.unit
def test_a_file_without_the_message_type_column_is_refused() -> None:
    reading = read_bms("商品コード（発注用）\r\n2700002430598\r\n".encode("cp932"))

    assert not reading.ok
    assert "メッセージ種" in reading.fatal[0]


@pytest.mark.unit
def test_a_utf8_re_export_still_reads() -> None:
    """The carrier sends CP932, but a file that has been through another tool
    may come back UTF-8. Encoding is not what this reader is defending against —
    the padding guards are — so it should read either."""
    text = order_file().decode("cp932")

    reading = read_bms(text.encode("utf-8-sig"))

    assert reading.ok, reading.fatal
    assert reading.vouchers[0].destination_name == "町田コレクターズ"


@pytest.mark.unit
def test_row_issues_are_capped() -> None:
    rows = [{"取引明細番号（発注・返品）": f"{n:02d}", "原単価": "x"} for n in range(1, 61)]
    reading = read_bms(order_file(*rows))

    assert reading.total_rows == 60
    assert reading.valid_rows == 0
    assert len(reading.row_issues) == 50


# ------------------------------------------------------------------- real files


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filename", "kind", "rows", "vouchers", "quantity", "zero_rows"),
    [
        # Totals cross-checked against docs/29 §7-2, which counted them
        # independently: 受注 8/16-9/14 で856点, 返品 6/30-9/9 で3,901点.
        ("受注伝票_20260916123229.csv", BmsKind.ORDER, 335, 21, 856, 0),
        ("受領・返品_20260916123318.csv", BmsKind.RETURN, 1887, 79, 3901, 1),
    ],
)
def test_the_real_bms_files_read_clean(
    filename: str,
    kind: BmsKind,
    rows: int,
    vouchers: int,
    quantity: int,
    zero_rows: int,
) -> None:
    path = Path("csv_file/phase2/source") / filename
    if not path.exists():
        pytest.skip("the BMS sample files are not in this checkout")

    reading = read_bms(path.read_bytes())

    assert reading.fatal == []
    assert reading.row_issues == []
    assert reading.kind is kind
    assert reading.total_rows == rows
    assert len(reading.vouchers) == vouchers
    assert reading.total_quantity == quantity
    assert reading.zero_quantity_rows == zero_rows
    assert reading.linked_original_rows == 0
    assert {v.buyer_code for v in reading.vouchers} == {"0393"}
    # docs/29 §1: 納品先9店舗.
    assert len({v.destination_code for v in reading.vouchers}) == 9
