"""Unit tests for the single SKU-population predicate, plus a guard that forbids
re-deriving it anywhere else.

The guard matters more than it looks: four independently-written Phase 2 designs
each named this predicate differently, and one said "if it does not exist when
this area starts, this area creates it". Two copies with slightly different
exclusion semantics is precisely how a bundle parent shows up in one screen and
not the next.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.services.sku_scope import (
    analysable_conditions,
    auto_push_conditions,
    operational_conditions,
)

pytestmark = pytest.mark.unit

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

# Modules allowed to touch the lifecycle columns directly, with the reason.
DIRECT_USE_ALLOWED = {
    # Declares the columns.
    "app/models/master_sku.py",
    # THE definition of the predicate.
    "app/services/sku_scope.py",
    # Legitimately SELECTS bundle parents (the inverse of excluding them) — it
    # exists to push their derived availability.
    "app/services/bundle_push.py",
    # Counts the COMPLEMENT of the operational population on purpose: "how many
    # SKUs are archived / 在庫管理対象外 / bundle parents" are its own tiles.
    # Its defect tiles do use operational_conditions(); only the three
    # informational counts name the flags, and sku_scope has no predicate for
    # "the ones we exclude" because nothing else needs one.
    "app/services/data_quality.py",
    # Import/seed CLIs set and read the flag while building masters; they are not
    # filtering a query population.
    "app/cli/import_variant_mappings.py",
    "app/cli/seed_variant_stock.py",
    # Selects the 在庫管理対象外 population itself, like bundle_push selects
    # bundle parents: the flag means "this master must have no inventory
    # events", and the whole job of that CLI is to find the ones that do.
    # Narrowing to the analysable population would hide every finding.
    "app/cli/inspect_stock_event_damage.py",
    # Selects bundle parents themselves, like bundle_push: it picks a parent
    # with recent sales for the 検収 walkthrough of 売上は親・在庫は構成品.
    "app/cli/inspect_review_materials.py",
}

# `MasterSku.is_bundle.is_(...)` style predicate use in a query.
PREDICATE_RE = re.compile(
    r"\b(?:MasterSku\.)?"
    r"(is_bundle|is_stock_managed|archived_at|stock_push_paused_at)"
    r"\s*\.\s*is_\s*\("
)


def _sql_conditions(conditions) -> set[str]:
    return {str(c.compile(compile_kwargs={"literal_binds": True})) for c in conditions}


def test_analysable_excludes_bundles_and_unmanaged() -> None:
    sql = _sql_conditions(analysable_conditions(include_archived=True))
    assert any("is_stock_managed IS true" in s for s in sql)
    assert any("is_bundle IS false" in s for s in sql)
    # Historical aggregates must keep retired SKUs, or past sales disappear.
    assert not any("archived_at" in s for s in sql)


def test_analysable_can_exclude_archived_for_current_state() -> None:
    sql = _sql_conditions(analysable_conditions(include_archived=False))
    assert any("archived_at IS NULL" in s for s in sql)


def test_analysable_requires_the_archived_decision() -> None:
    """No default: the caller must state history-vs-present explicitly."""
    with pytest.raises(TypeError):
        analysable_conditions()  # type: ignore[call-arg]


def test_operational_defaults_hide_archived_and_unmanaged() -> None:
    sql = _sql_conditions(operational_conditions())
    assert any("archived_at IS NULL" in s for s in sql)
    assert any("is_stock_managed IS true" in s for s in sql)


def test_operational_keeps_bundle_parents_visible() -> None:
    """An operator looking up a set should see it, unlike an analytics rollup."""
    sql = _sql_conditions(operational_conditions())
    assert not any("is_bundle" in s for s in sql)


def test_operational_flags_widen_the_view() -> None:
    assert operational_conditions(include_archived=True, include_unmanaged=True) == []
    sql = _sql_conditions(operational_conditions(include_unmanaged=True))
    assert not any("is_stock_managed" in s for s in sql)
    assert any("archived_at" in s for s in sql)


def test_no_module_re_derives_the_scope_predicate() -> None:
    """Guard: only sku_scope.py may build the population predicate."""
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        rel = path.relative_to(APP_DIR.parent).as_posix()
        if rel in DIRECT_USE_ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PREDICATE_RE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "These modules build the SKU population predicate themselves. Use "
        "app.services.sku_scope instead (or add a justified entry to "
        "DIRECT_USE_ALLOWED):\n  " + "\n  ".join(offenders)
    )


def test_auto_push_excludes_a_held_master() -> None:
    sql = _sql_conditions(auto_push_conditions())
    assert any("stock_push_paused_at IS NULL" in s for s in sql)


def test_auto_push_does_not_narrow_anything_else() -> None:
    """A held master is still operational: it sells, it deducts, it is listed.

    The hold means one thing only — no UNATTENDED write to a channel. If this
    predicate ever grew an `is_bundle` or `archived_at` clause it would start
    overlapping `operational_conditions`, and callers combining the two would
    narrow twice by accident.
    """
    assert len(auto_push_conditions()) == 1
