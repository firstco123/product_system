"""The single source of truth for "which master SKUs does this query cover?".

Every screen, job and aggregate in Phase 2 narrows the SKU population, and each
one narrows it *differently*. Writing those predicates inline is how ~32 bundle
parents and ~400 archived masters end up included in one screen and excluded from
the next — a discrepancy nobody notices until the numbers are compared.

Three flags participate, and they mean genuinely different things:

`is_bundle`
    Set/shared-stock parents. They hold NO stock of their own; availability is
    derived from `bundle_components`. Counting them in a stock or demand
    aggregate double-counts their components.

`is_stock_managed`
    False for gift boxes, coupons, made-to-order items. These "sell" with every
    order but have no physical stock, so they march negative forever (one hit
    -12509 in Phase 1-B) and they polluted the best-seller ranking. They produce
    no inventory events at all once flagged.

`archived_at`
    Retired masters — mostly the legacy product-level rows the variant cutover
    replaced. This is a VISIBILITY concept, not an inventory one, which is why it
    is handled asymmetrically:

        current-state screens/jobs  ->  exclude archived
        historical period aggregates ->  INCLUDE archived

    Filtering archived out of a period report would erase those masters' past
    sales and silently break every comparison spanning the 2026-07-20 cutover.

Because that asymmetry is the easiest thing to get wrong, `include_archived` is a
REQUIRED keyword on `analysable_conditions()`: callers must state whether they are
reporting on history or on the present.

Usage:

    stmt = select(MasterSku).where(*analysable_conditions(include_archived=True))
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import BundleComponent, ChannelSkuMapping, InventorySnapshot, MasterSku

__all__ = [
    "ArchiveBlockers",
    "analysable_conditions",
    "archive_blockers",
    "auto_push_conditions",
    "bundle_parent_conditions",
    "operational_conditions",
]


def analysable_conditions(*, include_archived: bool) -> list[ColumnElement[bool]]:
    """SKU population for ANALYTICS: dashboards, velocity, forecasting, reorder.

    Always excludes bundle parents (their stock is derived from components) and
    non-stock-managed items (packaging/coupons are not merchandise).

    `include_archived` is required and has no default:

    * ``True``  — historical period aggregates (sales trends, period comparison).
      Retired masters really did sell in the past; dropping them rewrites history.
    * ``False`` — present-tense analytics (stock rollup, velocity, forecast,
      reorder). A retired SKU must not appear in "what should I order today".
    """
    conditions: list[ColumnElement[bool]] = [
        MasterSku.is_stock_managed.is_(True),
        MasterSku.is_bundle.is_(False),
    ]
    if not include_archived:
        conditions.append(MasterSku.archived_at.is_(None))
    return conditions


def operational_conditions(
    *,
    include_archived: bool = False,
    include_unmanaged: bool = False,
    include_bundles: bool = True,
) -> list[ColumnElement[bool]]:
    """SKU population for CURRENT-STATE operational screens (inventory list,
    manual adjust, stocktake, alerts).

    Unlike `analysable_conditions`, bundle parents are NOT excluded: an operator
    legitimately looks up a set and sees its derived availability.

    Defaults hide retired and non-stock-managed SKUs, which is what an operator
    wants; pass the flags to widen the view (e.g. an "アーカイブ済も表示"
    toggle, or the maintenance screen that assigns `non_inventory_kind`).

    `include_bundles=False` is for screens that list STOCK. A bundle parent has
    no snapshot of its own, so it read as 「0・ゼロ」 in the inventory list and
    inflated the zero badge (107 against the dashboard's 71, 2026-10-03) while
    duplicating its components' state.
    """
    conditions: list[ColumnElement[bool]] = []
    if not include_bundles:
        conditions.append(MasterSku.is_bundle.is_(False))
    if not include_archived:
        conditions.append(MasterSku.archived_at.is_(None))
    if not include_unmanaged:
        conditions.append(MasterSku.is_stock_managed.is_(True))
    return conditions


def auto_push_conditions() -> list[ColumnElement[bool]]:
    """Masters whose stock a SCHEDULED job may write to a channel.

    `stock_push_paused_at` is set per master by `app.cli.pause_stock_push` and
    means 「このSKUの在庫数はチャネルへ自動で書き込まない」. The client asked to
    hold the 48 gift sets' Shopify quantities by hand for the Christmas period
    (2026-11-19), and the obvious lever — `is_stock_managed = false` — would have
    broken the very thing they wanted kept: `resolve_consumption` tests that flag
    on the parent and returns [], so the set would stop deducting its components
    on every sale.

    This is a separate function rather than a clause inside `operational_conditions`
    because a paused master is still fully operational: it sells, it deducts, it
    shows its derived figure on every screen, and the reconcile and stock screens
    must keep listing it. The ONLY thing it is excluded from is an unattended
    write to a channel.

    Operator-driven pushes are deliberately not filtered by this. The 同期エラー
    screen's 再実行 button and an approved reconcile adjustment are a person
    asking for one write with the number in front of them; a pause is protection
    from the unattended hourly job, not a lock against the operator.
    """
    return [MasterSku.stock_push_paused_at.is_(None)]


def bundle_parent_conditions(*, include_archived: bool = False) -> list[ColumnElement[bool]]:
    """The set / shared-stock PARENTS themselves.

    For screens that have to find a parent after the stock screens stopped
    listing them (2026-10-03): searching 「N108」 in 在庫一覧 showed only the
    shared component, and the client asked how to reach the 45cm and 42cm pages
    (acceptance Q&A, 2026-10-12).
    """
    conditions: list[ColumnElement[bool]] = [MasterSku.is_bundle.is_(True)]
    if not include_archived:
        conditions.append(MasterSku.archived_at.is_(None))
    return conditions


@dataclass(frozen=True, slots=True)
class ArchiveBlockers:
    """Which of the given masters must NOT be archived, and why.

    Archiving is a VISIBILITY change: an archived master disappears from every
    current-state screen while orders keep consuming it. That is exactly right
    for a retired master and exactly wrong for a live one, so the same three
    questions are asked by the bulk CLI (`app.cli.archive_legacy_skus`) and by
    the per-row toggle in the admin UI. They live here, once, because two
    implementations of "is this SKU still in use?" would eventually disagree —
    and the one that says "no" wins by hiding the SKU.
    """

    with_stock: set[int]
    with_active_mapping: set[int]
    component_of_live_bundle: set[int]
    #: on-hand quantity for each master in `with_stock`. Carried because "4 SKUs
    #: were skipped" is not a reviewable number — whether the blocker is a
    #: leftover -3 or a real +120 on the shelf decides whether the fix is a
    #: stocktake or a conversation with the client.
    stock_qty: dict[int, int] = field(default_factory=dict)

    def blocked(self) -> set[int]:
        return self.with_stock | self.with_active_mapping | self.component_of_live_bundle

    def reasons_for(self, master_sku_id: int) -> list[str]:
        """Operator-facing Japanese explanations, all of them — not just the
        first. Reporting one at a time makes a repeat attempt look like a new
        problem each time."""
        reasons: list[str] = []
        if master_sku_id in self.with_stock:
            qty = self.stock_qty.get(master_sku_id)
            reasons.append(
                f"在庫が {qty} 残っています" if qty is not None else "在庫が残っています"
            )
        if master_sku_id in self.with_active_mapping:
            reasons.append("有効なチャネルマッピングがあります")
        if master_sku_id in self.component_of_live_bundle:
            reasons.append("有効なセット商品の構成品です")
        return reasons


async def archive_blockers(session: AsyncSession, master_sku_ids: Sequence[int]) -> ArchiveBlockers:
    """Three set-based queries, so one master and four hundred cost the same."""
    ids = list(master_sku_ids)
    if not ids:
        return ArchiveBlockers(set(), set(), set())

    stock = await session.execute(
        select(InventorySnapshot.master_sku_id, InventorySnapshot.on_hand_qty).where(
            InventorySnapshot.master_sku_id.in_(ids),
            InventorySnapshot.on_hand_qty != 0,
        )
    )
    mapping = await session.execute(
        select(ChannelSkuMapping.master_sku_id).where(
            ChannelSkuMapping.master_sku_id.in_(ids),
            ChannelSkuMapping.is_active.is_(True),
        )
    )
    # "Live" parent = not itself archived. Archiving a set first therefore
    # releases its components on the next pass, so a cleanup can converge
    # instead of deadlocking on its own guard.
    parent = aliased(MasterSku)
    component = await session.execute(
        select(BundleComponent.component_master_sku_id)
        .join(parent, parent.id == BundleComponent.bundle_master_sku_id)
        .where(
            BundleComponent.component_master_sku_id.in_(ids),
            parent.archived_at.is_(None),
        )
    )
    stock_qty: dict[int, int] = {mid: qty for mid, qty in stock.all()}  # noqa: C416
    return ArchiveBlockers(
        with_stock=set(stock_qty),
        with_active_mapping={mid for (mid,) in mapping.all()},
        component_of_live_bundle={mid for (mid,) in component.all()},
        stock_qty=stock_qty,
    )
