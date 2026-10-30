"""Which channel push targets can be written, and which have no right answer.

Phase 2, P2-039.

`channel_targets` answers "every active mapping this master has on this
channel". That is the correct question for a fan-out — one master really is sold
from several Rakuten pages — but two of its answers must not be written, and
until this module they both were.

A PAGE-LEVEL ROW IS NOT A WRITE TARGET

    A Rakuten mapping with no `channel_variant_key` names 商品管理番号 alone. This
    shop is 100% 項目選択肢別在庫, so stock lives on SKU管理番号 and a page-level
    write has no address to land on. Those rows are not mistakes: the page row is
    what resolves live order lines, and the model keeps it beside the variant
    rows on purpose. But `channel_targets` returns it too, so every Rakuten
    fan-out carried one guaranteed failure per page — the adapter raises,
    `push_single` writes a failed sync_attempt, Slack fires. Skipping it is not
    hiding an error; the row is correct and simply is not somewhere to write.

ONE MASTER ACROSS SEVERAL SKUs OF ONE PAGE HAS NO CORRECT QUANTITY

    Measured 2026-10-30 against `rakuten_variant_map_proposal.csv`: of 141
    mapped masters, 57 map to more than one SKU管理番号 on the SAME page — 111
    such pairs, and in 93 of them Rakuten currently holds a DIFFERENT quantity
    per SKU. Page 0020 maps SKUs 1396/1397/1399 all to the single master
    `0020c`, holding 5/4/4 respectively.

    These are the coarse masters: one of ours standing in for several of
    Rakuten's. Writing the master's availability to each of them does not
    divide it, it REPEATS it. Three SKUs at 5 is 15 units of apparent stock
    against 5 we hold, and the per-colour figures the shop actually sells on are
    flattened to one number. No quantity this push could send is correct, so it
    sends none and names the master that needs splitting.

    Equal quantities today are not a reprieve: screening happens before the
    read, and two SKUs holding 4 apiece this morning still cannot both be set
    from one figure tomorrow.

Screening is a pure function over the target list so that a dry run and a real
push reach the same verdict. A gate that only engages on the real push is a gate
the operator cannot preview, which is how the thing it prevents gets discovered
in production.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.services.mapping import ChannelTarget

#: The channel whose `channel_sku` is a page rather than a sellable unit.
RAKUTEN = "rakuten"

#: Stable slugs, so logs and tests name a reason instead of matching prose.
PAGE_LEVEL_ROW = "page_level_row"
COARSE_MASTER = "coarse_master"


@dataclass(frozen=True, slots=True)
class Refusal:
    """One target that will not be written, and why, in the operator's words."""

    channel_sku: str
    variant_key: str | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Screened:
    """The verdict for one master on one channel.

    `skipped` and `blocked` are separate because conflating them costs the
    operator either way. Every Rakuten page produces a skip, every run, by
    design — surfacing those as problems trains people to ignore the list that
    also carries `blocked`, which is a real stop needing a data change.
    """

    targets: list[ChannelTarget] = field(default_factory=list)
    skipped: list[Refusal] = field(default_factory=list)
    blocked: list[Refusal] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        return bool(self.targets)

    @property
    def blocked_pages(self) -> list[Refusal]:
        """One representative refusal per blocked page.

        `blocked` carries a row per SKU管理番号 so a caller can name them all,
        but they share one message. Logging or notifying per row would repeat
        the same sentence three times for page 0020 and bury the next page.
        """
        seen: dict[str, Refusal] = {}
        for refusal in self.blocked:
            seen.setdefault(refusal.channel_sku, refusal)
        return list(seen.values())

    @property
    def wholly_blocked(self) -> bool:
        """Nothing to write, and the reason is a data problem, not an absence.

        Distinguishes "this master has no Rakuten mapping", which is ordinary,
        from "this master's mappings cannot be written", which is not.
        """
        return not self.targets and bool(self.blocked)


def _page_level_refusal(channel_sku: str) -> Refusal:
    return Refusal(
        channel_sku=channel_sku,
        variant_key=None,
        code=PAGE_LEVEL_ROW,
        message=(
            f"商品管理番号 {channel_sku} はページ単位の対応づけのため、在庫の書き込み先では"
            "ありません。この店舗は項目選択肢別在庫のみで、在庫はSKU管理番号に書き込みます。"
        ),
    )


def _coarse_master_refusals(channel_sku: str, keys: Sequence[str]) -> list[Refusal]:
    listed = "、".join(keys)
    message = (
        f"商品管理番号 {channel_sku} には、同じマスタに対応づいたSKU管理番号が "
        f"{len(keys)}件 あります: {listed}。1つの在庫数を複数のSKUに書き込むと"
        "在庫を重複して計上するため、このページへの書き込みを中止しました。"
        "マスタをSKU単位に分割してください。"
    )
    return [
        Refusal(channel_sku=channel_sku, variant_key=key, code=COARSE_MASTER, message=message)
        for key in keys
    ]


def screen_targets(targets: Sequence[ChannelTarget], *, channel: str) -> Screened:
    """Split a master's targets into what to write, what to skip, and what to stop on.

    Only Rakuten is screened. On every other channel `channel_sku` is already
    the sellable unit, so each target is its own address and there is nothing to
    disambiguate — passing those through untouched keeps this out of the Shopify
    path it has no business being in.
    """
    if channel != RAKUTEN:
        return Screened(targets=list(targets))

    by_page: dict[str, list[ChannelTarget]] = defaultdict(list)
    for target in targets:
        by_page[target.channel_sku].append(target)

    screened = Screened()
    for channel_sku, rows in by_page.items():
        variants = [r for r in rows if r.variant_key is not None]
        if len(rows) != len(variants):
            screened.skipped.append(_page_level_refusal(channel_sku))
        if len(variants) > 1:
            # `variant_key is not None` holds for every row in `variants`, but
            # mypy cannot see that through the comprehension above.
            keys = [r.variant_key for r in variants if r.variant_key is not None]
            screened.blocked.extend(_coarse_master_refusals(channel_sku, keys))
        else:
            screened.targets.extend(variants)
    return screened


__all__ = [
    "COARSE_MASTER",
    "PAGE_LEVEL_ROW",
    "RAKUTEN",
    "Refusal",
    "Screened",
    "screen_targets",
]
