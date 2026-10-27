"""チャネルは、読み手が読む名前で表示する.

The screens printed the stored key. So the channel composition panel — one of
the screens the client is walked through at 検収 — listed `rakuten`, `shopify`
and `未マッピング` in one column, the last of which is a label, not a key,
because unmapped revenue has no channel to store.

Harmless while every channel was Latin-scripted. Not harmless once 卸売 arrives:
P2-011 asks for a channel composition that includes 卸, and a row reading
`wholesale` is the one row a Japanese-speaking reader cannot place.

The guards here are deliberately source-reading as well as behavioural, so a
screen added later is covered the day it lands.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.models.enums import ChannelEnum
from app.services.analytics_query import UNMAPPED_LABEL
from app.services.timeframe import Period
from app.ui.deps import CHANNEL_LABELS, channel_label, templates
from app.ui.routes.analytics import _labelled_shares

pytestmark = pytest.mark.unit

TEMPLATES = Path("app/ui/templates")
PERIOD = Period(date(2026, 10, 1), date(2026, 10, 28), "28d")


# --- the label itself -----------------------------------------------------


def test_every_channel_in_the_enum_has_a_label() -> None:
    """A channel added to the enum but not to the map renders as its key.

    That is how `wholesale` would have reached the 検収 screen: the data model
    gained the channel months before anything displayed it.
    """
    missing = [c.value for c in ChannelEnum if c.value not in CHANNEL_LABELS]
    assert not missing, f"these would render as bare keys: {missing}"


def test_the_wholesale_channel_reads_as_a_japanese_word() -> None:
    """The P2-011 row. Named 卸売 rather than 卸 to match 卸売EDI取込."""
    assert channel_label("wholesale") == "卸売"


def test_rakuten_is_japanese_and_the_latin_brands_are_not() -> None:
    """docs/35 §1-4 names them 楽天 and Shopify, and the client's own wording is
    what a label is for."""
    assert channel_label("rakuten") == "楽天"
    assert channel_label("shopify") == "Shopify"
    assert channel_label("amazon") == "Amazon"


def test_an_unknown_channel_passes_through_rather_than_vanishing() -> None:
    """Legible beats blank: a channel that reaches the data before it reaches
    the map still names itself."""
    assert channel_label("mercari") == "mercari"


def test_the_unmapped_row_is_left_alone() -> None:
    """It arrives already Japanese, and it is not a channel — it is the revenue
    whose channel attribution we do not have."""
    assert channel_label(UNMAPPED_LABEL) == UNMAPPED_LABEL


def test_a_missing_channel_stays_missing() -> None:
    """`None` reaches the filter from every nullable channel column. Turning it
    into a string here would defeat the templates' own `or '—'`."""
    assert channel_label(None) is None


# --- wiring ---------------------------------------------------------------


def test_the_filter_is_registered_under_the_name_the_templates_use() -> None:
    assert templates.env.filters["channel_label"] is channel_label


def test_only_the_shared_macro_defines_the_channel_badge() -> None:
    """Two screens each carried their own copy with the same four colour arms.

    A badge that is defined twice drifts: one screen gains a colour or a label
    and the same channel then reads as two different things.
    """
    offenders = [
        p.name
        for p in sorted(TEMPLATES.glob("*.html"))
        if p.name != "_detail.html" and "{% macro channel_badge" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"these define their own channel badge: {offenders}"


def test_no_channel_filter_offers_a_bare_key_as_its_option_text() -> None:
    """Every channel `<select>` is populated from the data, so its option text
    is a stored key until a filter is applied to it."""
    offenders = [
        p.name
        for p in sorted(TEMPLATES.glob("*.html"))
        if "{{ c }}</option>" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"these print an unlabelled option: {offenders}"


# --- the composition panel and its CSV ------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Enough of a session for `channel_share`: one `execute`, one `scalar`."""

    def __init__(self, rows: list[Any], unmapped: Decimal) -> None:
        self._rows = rows
        self._unmapped = unmapped

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._rows)

    async def scalar(self, _stmt: Any) -> Any:
        return self._unmapped


async def test_the_composition_panel_names_every_channel() -> None:
    """The screen and its CSV both read this list, so labelling it once covers
    both — and both are what the client sees at 検収."""
    session = _FakeSession(
        [("rakuten", Decimal(600)), ("wholesale", Decimal(300)), ("shopify", Decimal(50))],
        Decimal(100),
    )

    rows = await _labelled_shares(session, PERIOD)  # type: ignore[arg-type]

    assert rows == [
        ("楽天", Decimal(600)),
        ("卸売", Decimal(300)),
        ("未マッピング", Decimal(100)),
        ("Shopify", Decimal(50)),
    ]


async def test_labelling_does_not_disturb_the_amounts_or_their_order() -> None:
    """`channel_share` sorts by revenue and the parts have to keep summing to
    the headline total; a label map must not touch either."""
    session = _FakeSession([("wholesale", Decimal(10)), ("rakuten", Decimal(90))], Decimal(0))

    rows = await _labelled_shares(session, PERIOD)  # type: ignore[arg-type]

    assert [name for name, _ in rows] == ["楽天", "卸売"]
    assert sum(value for _, value in rows) == Decimal(100)
