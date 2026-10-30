"""Unit tests — screen_targets: which push targets can actually be written.

Pure function over a target list, so these need no database. The shapes used
here are the real ones, measured 2026-10-30 against
`csv_file/phase2/rakuten/rakuten_variant_map_proposal.csv`: page 0020 maps SKUs
1396/1397/1399 to the single master `0020c`, and 066's SKU管理番号 are RMS
auto-generated `r-sku...` strings that Rakuten genuinely serves.
"""

from __future__ import annotations

import pytest

from app.services.mapping import ChannelTarget
from app.services.push_safety import COARSE_MASTER, PAGE_LEVEL_ROW, screen_targets

pytestmark = pytest.mark.unit


def _t(channel_sku: str, variant_key: str | None = None) -> ChannelTarget:
    return ChannelTarget(channel_sku=channel_sku, variant_key=variant_key)


def test_a_non_rakuten_channel_is_not_screened_at_all() -> None:
    """On Shopify `channel_sku` IS the sellable unit, so every row is an address.

    Screening there would be a bug, not a safeguard: a NULL variant_key is the
    normal and permanent state for every Shopify, Amazon and wholesale row.
    """
    targets = [_t("N21gold"), _t("N55silver")]
    screened = screen_targets(targets, channel="shopify")
    assert screened.targets == targets
    assert screened.skipped == []
    assert screened.blocked == []


def test_the_rakuten_page_row_is_skipped_and_its_sku_is_written() -> None:
    """The pair that every Rakuten page produces.

    The page row resolves live order lines and is kept on purpose, but stock has
    to land on the SKU管理番号. Before screening, this page pushed twice: once
    correctly, and once with no variant key at all, which the adapter rejected.
    """
    screened = screen_targets([_t("10048"), _t("10048", "1147")], channel="rakuten")
    assert screened.targets == [_t("10048", "1147")]
    assert [r.code for r in screened.skipped] == [PAGE_LEVEL_ROW]
    assert screened.blocked == []
    assert screened.writable is True
    assert screened.wholly_blocked is False


def test_a_page_with_only_a_page_row_writes_nothing_and_is_not_blocked() -> None:
    """No variant mapped yet is an absence, not a data conflict.

    `wholly_blocked` has to stay False here: this master needs its variants
    mapping, which is ordinary unfinished work, and reporting it as a blocked
    push would put it in the same queue as the coarse masters.
    """
    screened = screen_targets([_t("10091")], channel="rakuten")
    assert screened.targets == []
    assert [r.code for r in screened.skipped] == [PAGE_LEVEL_ROW]
    assert screened.blocked == []
    assert screened.wholly_blocked is False


def test_one_master_over_several_skus_of_one_page_is_blocked() -> None:
    """The coarse masters: 57 of 141 mapped masters, measured 2026-10-30.

    Page 0020 holds 5/4/4 on SKUs 1396/1397/1399. Writing `0020c`'s availability
    to each does not divide it between them, it repeats it — 15 units of
    apparent stock against the 5 we hold.
    """
    screened = screen_targets(
        [_t("0020", "1396"), _t("0020", "1397"), _t("0020", "1399")],
        channel="rakuten",
    )
    assert screened.targets == []
    assert [r.code for r in screened.blocked] == [COARSE_MASTER] * 3
    assert [r.variant_key for r in screened.blocked] == ["1396", "1397", "1399"]
    assert screened.writable is False
    assert screened.wholly_blocked is True


def test_the_block_message_names_every_sku_and_what_to_do() -> None:
    """The message is the whole output of a refusal, so it carries the remedy.

    An operator reading `bundle_push.blocked` cannot see the mapping table; if
    the line does not say which SKUs collided and that the master needs
    splitting, the refusal is just a push that silently did not happen.
    """
    screened = screen_targets(
        [_t("0020", "1396"), _t("0020", "1397"), _t("0020", "1399")],
        channel="rakuten",
    )
    message = screened.blocked[0].message
    assert "0020" in message
    assert all(key in message for key in ("1396", "1397", "1399"))
    assert "3件" in message
    assert "分割" in message


def test_blocked_pages_reports_each_page_once() -> None:
    """Three SKUs on one page share one reason and one sentence."""
    screened = screen_targets(
        [_t("0020", "1396"), _t("0020", "1397"), _t("0010", "1465"), _t("0010", "1466")],
        channel="rakuten",
    )
    assert len(screened.blocked) == 4
    assert [r.channel_sku for r in screened.blocked_pages] == ["0020", "0010"]


def test_a_blocked_page_does_not_stop_the_master_s_other_pages() -> None:
    """A master is sold from several pages and they fail independently.

    `0020c` is coarse on page 0020 but may map cleanly on another page. Dropping
    the whole master would withhold a correct write because of an unrelated one.
    """
    screened = screen_targets(
        [_t("0020", "1396"), _t("0020", "1397"), _t("0020c", "799")],
        channel="rakuten",
    )
    assert screened.targets == [_t("0020c", "799")]
    assert [r.channel_sku for r in screened.blocked_pages] == ["0020"]
    assert screened.wholly_blocked is False


def test_placeholder_style_sku_keys_are_written_like_any_other() -> None:
    """`r-sku00000003` is a real SKU管理番号, not a gap to refuse.

    24 pages use RMS auto-generated keys exclusively, and the 2026-10-28 probe
    read stock 11 straight off `066/r-sku00000003`. Rejecting them by pattern —
    the gate this module was originally expected to be — would have blocked 24
    live pages from ever receiving stock. The same string appears on up to 24
    pages, but each page+SKU pair is its own address, so there is nothing
    ambiguous to screen.
    """
    screened = screen_targets([_t("066"), _t("066", "r-sku00000003")], channel="rakuten")
    assert screened.targets == [_t("066", "r-sku00000003")]
    assert screened.blocked == []


def test_input_order_is_preserved_so_two_runs_can_be_compared() -> None:
    """`channel_targets` orders its query for this reason; screening keeps it."""
    targets = [_t("10048", "1147"), _t("10048c", "1149"), _t("10049", "1200")]
    screened = screen_targets(targets, channel="rakuten")
    assert screened.targets == targets
