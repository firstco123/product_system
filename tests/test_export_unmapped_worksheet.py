"""The worksheet we hand the client to fill in.

Two things can go wrong here and neither shows up locally. The SQL is Postgres
-only and runs only against production, so a broken statement is discovered by
running it on prod. And the file is opened in Excel on a Windows desktop, where
a missing BOM silently mangles every Japanese product name — the failure that
already reached this client once.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.cli.export_unmapped_worksheet import (
    HEADER,
    WorksheetRow,
    collect_rows,
    collect_uncovered,
    write_worksheet,
)

pytestmark = pytest.mark.unit


class _CompilingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement: object) -> object:
        self.statements.append(
            str(
                statement.compile(  # type: ignore[attr-defined]
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
        )
        return type("_Empty", (), {"all": lambda self: []})()


async def _sql(*, include_mapped_keys: bool) -> str:
    session = _CompilingSession()
    await collect_rows(session, include_mapped_keys=include_mapped_keys)  # type: ignore[arg-type]
    return " ".join(session.statements)


async def test_the_query_covers_both_channels() -> None:
    """`channel` lives on orders, so the join is required either way. Scoping
    to Rakuten alone was right while Shopify had no backlog; after the 2026-09
    re-resolution the remaining 177 keys spanned both, and a Rakuten-only sheet
    would have asked about part of it and silently dropped the rest."""
    sql = await _sql(include_mapped_keys=False)
    assert "JOIN orders" in sql
    assert "'rakuten'" in sql
    assert "'shopify'" in sql
    assert "order_items.master_sku_id IS NULL" in sql


async def test_rows_are_grouped_per_channel() -> None:
    """One code can exist on both shops and mean different products. Grouping
    on the code alone would merge them into one row and one answer."""
    sql = await _sql(include_mapped_keys=False)
    assert "GROUP BY orders.channel" in sql


async def test_keys_that_already_have_a_mapping_are_excluded_by_default() -> None:
    """Those need a remap from us, not an answer from the client. Asking about
    them wastes their time and invites a confusing reply."""
    assert "NOT IN" in (await _sql(include_mapped_keys=False)).upper()


async def test_all_includes_them_for_our_own_review() -> None:
    assert "NOT IN" not in (await _sql(include_mapped_keys=True)).upper()


def _row(**overrides: object) -> WorksheetRow:
    base = {
        "channel": "rakuten",
        "manage_number": "10001",
        "product_name": "馬蹄 ネックレス",
        "lines": 3,
        "quantity": 5,
        "amount": 12000,
        "first_date": "2025-04-01",
        "last_date": "2026-08-30",
    }
    return WorksheetRow(**{**base, **overrides})  # type: ignore[arg-type]


def test_a_row_fills_every_column() -> None:
    """A short row shifts every value left of it under a wrong header."""
    assert len(_row().as_csv()) == len(HEADER)


def test_the_two_answer_columns_are_left_empty() -> None:
    row = _row().as_csv()
    assert row[-2:] == ("", ""), "the answer column and 備考 are the client's to fill"


def test_the_file_carries_a_bom_and_reads_back_intact(tmp_path) -> None:
    """Without the BOM Excel falls back to the ANSI codepage and every Japanese
    name arrives mangled — the mojibake this client already received once."""
    out = tmp_path / "worksheet.csv"
    write_worksheet([_row()], out)

    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "missing UTF-8 BOM"

    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert tuple(rows[0]) == HEADER

    # Looked up by header, not by position. Adding the チャネル column shifted
    # every index by one and broke this test rather than the export — which is
    # the wrong thing to notice, and next time might be the export.
    cell = dict(zip(rows[0], rows[1], strict=True))
    assert cell["チャネル"] == "rakuten"
    assert cell["チャネルの商品コード"] == "10001"
    assert cell["商品名"] == "馬蹄 ネックレス"
    assert cell["金額合計"] == "12000", "yen render without a decimal point"


def test_an_empty_result_still_writes_a_usable_file(tmp_path) -> None:
    """Nothing to ask about is a valid outcome; a header-only file says so,
    where a missing file looks like the export failed."""
    out = tmp_path / "empty.csv"
    write_worksheet([], out)
    rows = list(csv.reader(io.StringIO(out.read_bytes().decode("utf-8-sig"))))
    assert tuple(rows[0]) == HEADER
    assert len(rows) == 1


def test_jst_dates_are_rendered_not_timestamps() -> None:
    """A UTC timestamp at 23:00 is the NEXT day in JST; the client reads these
    as order dates."""
    from app.cli.export_unmapped_worksheet import _jst

    assert _jst(datetime(2026, 8, 30, 23, 0, tzinfo=UTC)) == "2026-08-31"
    assert _jst(None) == ""


# --- what the sheet does NOT carry ----------------------------------------


class _RowsSession:
    """Returns one canned result set, as rows with the labelled attributes the
    query selects."""

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    async def execute(self, _statement: object) -> object:
        made = [
            type("_Row", (), {"channel": r[0], "keys": r[1], "lines": r[2], "amount": r[3]})()
            for r in self._rows
        ]
        return type("_Result", (), {"all": lambda self: made})()


async def _uncovered_sql() -> str:
    session = _CompilingSession()
    await collect_uncovered(session)  # type: ignore[arg-type]
    return " ".join(session.statements)


async def test_the_uncovered_read_is_the_exact_complement_of_the_sheet() -> None:
    """Same two predicates as the sheet — a NULL master on a live line — with
    the channel test inverted. Anything else and the two numbers on screen
    cannot be compared with each other."""
    sql = await _uncovered_sql()
    assert "order_items.master_sku_id IS NULL" in sql
    assert "orders.channel NOT IN" in sql
    assert "'rakuten'" in sql
    assert "'shopify'" in sql


async def test_it_summarises_per_channel_not_per_key() -> None:
    """Nobody is filling these in, so a key-level list would just be noise next
    to the sheet that IS being filled in."""
    sql = await _uncovered_sql()
    assert "GROUP BY orders.channel" in sql
    assert "orders.channel, order_items.channel_sku" not in sql


async def test_unmapped_wholesale_revenue_is_reported() -> None:
    """The case this exists for. 卸売 writes an order line for every voucher
    line, mapped or not, so its unmapped lines reach the same rollup as the
    marketplace ones and show in the screen's 未マッピング figure — while a
    wholesale code is resolved from the carrier's files, not by the client."""
    session = _RowsSession([("wholesale", 5, 61, 184_500)])

    (row,) = await collect_uncovered(session)  # type: ignore[arg-type]

    assert row.channel == "wholesale"
    assert (row.keys, row.lines, row.amount) == (5, 61, 184_500)


async def test_nothing_uncovered_reports_nothing() -> None:
    """Before the wholesale import runs there are no such lines, and an empty
    warning block would read as a defect."""
    assert await collect_uncovered(_RowsSession([])) == []  # type: ignore[arg-type]
