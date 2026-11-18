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

The CSV, one row per component (a set spans as many rows as it has parts):

    セットSKU,セット商品名,構成品SKU,数量
    B66setGoldS,B66 ゴールド S 2個セット,B66goldS,2
    B34B66setA,B34シルバー19 と B66ゴールドS のセット,B34silver19cm,1
    B34B66setA,B34シルバー19 と B66ゴールドS のセット,B66goldS,1

    powershell -File scripts/run_cli.ps1 -Cli create_gift_sets ^
        -Args "--file csv_file/phase2/source/gift_sets.csv"
    ... same again with --apply to write.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.csv_intake import ColumnSpec, CsvSpec, inspect, iter_rows
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import BundleComponent, InventorySnapshot, MasterSku

log = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

COL_SET = "セットSKU"
COL_NAME = "セット商品名"
COL_COMPONENT = "構成品SKU"
COL_QTY = "数量"

#: `master_skus.sku_code` is String(64) and `name` is String(255). Checked here,
#: where the message can name the row, rather than letting the database refuse
#: the insert after the rest of the file has been staged.
SKU_CODE_WIDTH = 64
NAME_WIDTH = 255


def quantity_fault(value: str) -> str | None:
    """Why this 数量 is not a count of items, or None when it is.

    A fullwidth digit is accepted: `float()` reads it as the number it is,
    which is what the operator who typed it meant.
    """
    try:
        number = float(value)
    except ValueError:
        return f"{COL_QTY}が数字ではありません: 「{value}」"
    if number != int(number):
        return f"{COL_QTY}が整数ではありません: 「{value}」"
    if int(number) < 1:
        return f"{COL_QTY}が1以上ではありません: 「{value}」"
    return None


SPEC = CsvSpec(
    columns=(
        ColumnSpec(canonical=COL_SET, aliases=("set_sku",)),
        ColumnSpec(canonical=COL_NAME, aliases=("set_name",)),
        ColumnSpec(canonical=COL_COMPONENT, aliases=("component_sku",)),
        ColumnSpec(canonical=COL_QTY, aliases=("quantity",), validator=quantity_fault),
    )
)


@dataclass(frozen=True, slots=True)
class Part:
    """One component of one set, as the file asks for it."""

    component_sku: str
    quantity: int


@dataclass(slots=True)
class GiftSet:
    set_sku: str
    name: str
    parts: list[Part] = field(default_factory=list)
    already_registered: bool = False


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
        return [s for s in self.sets if not s.already_registered]


def read_faults(data: bytes) -> list[Fault]:
    """Everything `csv_intake` would report, as whole-file refusals.

    A row it would merely skip is promoted here: see the module docstring on
    why a partial apply is the outcome worth refusing.
    """
    found = inspect(data, SPEC)
    faults = [Fault("ファイル", reason) for reason in found.fatal]
    faults += [Fault(f"{issue['line']}行目", str(issue["reason"])) for issue in found.row_issues]
    hidden = (found.total_rows - found.valid_rows) - len(found.row_issues)
    if hidden > 0:
        # `max_row_issues` caps the list. Saying so beats a report that reads
        # as if the problems stopped at fifty.
        faults.append(Fault("ファイル", f"ほかにも {hidden} 行に問題があります"))
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

        if set_sku not in names:
            order.append(set_sku)
            names[set_sku] = name
        elif names[set_sku] != name:
            # One set, two names: we would have to pick one, and the operator
            # would never learn which. Say so instead.
            faults.append(Fault(where, f"同じ{COL_SET}に別の{COL_NAME}が書かれています"))
            continue

        if any(p.component_sku == component for p in parts[set_sku]):
            # Two rows would become two bundle_components for one pair, and the
            # UNIQUE constraint would refuse the second after staging the rest.
            faults.append(Fault(where, f"同じ構成品が重複しています: 「{component}」"))
            continue
        parts[set_sku].append(Part(component_sku=component, quantity=int(float(row[COL_QTY]))))

    return [GiftSet(set_sku=s, name=names[s], parts=parts[s]) for s in order], faults


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
        select(MasterSku.sku_code, MasterSku.id, MasterSku.is_bundle, MasterSku.archived_at).where(
            MasterSku.sku_code.in_(wanted)
        )
    )
    masters = {code: (mid, bundle, archived) for code, mid, bundle, archived in found.all()}
    code_of = {mid: code for code, (mid, _, _) in masters.items()}

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

    for gift in sets:
        where = f"「{gift.set_sku}」"
        for part in gift.parts:
            if part.component_sku not in masters:
                faults.append(Fault(where, f"構成品のマスタがありません: 「{part.component_sku}」"))
                continue
            _, is_bundle, archived = masters[part.component_sku]
            if is_bundle:
                # resolve_consumption expands one level only; see the module
                # docstring. The goods on the shelf would never move.
                faults.append(Fault(where, f"構成品が既にセット商品です: 「{part.component_sku}」"))
            if archived is not None:
                faults.append(Fault(where, f"構成品が廃止されています: 「{part.component_sku}」"))

        if gift.set_sku not in masters:
            continue  # a new master: nothing more to check

        set_id, is_bundle, archived = masters[gift.set_sku]
        if archived is not None:
            faults.append(Fault(where, "このSKUは廃止されています"))
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
            gift.already_registered = True
        elif have:
            current = ", ".join(sorted(code_of[mid] for mid in have if mid in code_of))
            faults.append(Fault(where, f"既に別の構成で登録されています。現在: {current}"))
        elif not is_bundle:
            faults.append(Fault(where, "既に通常の商品として登録されています"))

    return Plan(sets, faults)


async def apply(session: AsyncSession, result: Plan) -> None:
    """Create the set masters and their component links. Adds only."""
    codes = {s.set_sku for s in result.ready()} | {
        p.component_sku for s in result.ready() for p in s.parts
    }
    rows = await session.execute(
        select(MasterSku.sku_code, MasterSku.id).where(MasterSku.sku_code.in_(codes))
    )
    ids = {code: mid for code, mid in rows.all()}  # noqa: C416

    for gift in result.ready():
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
        for part in gift.parts:
            session.add(
                BundleComponent(
                    bundle_master_sku_id=set_id,
                    component_master_sku_id=ids[part.component_sku],
                    quantity_per=part.quantity,
                )
            )


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
            if gift.already_registered:
                state = "登録済み"
            else:
                state = "登録しました" if apply_changes else "登録します"
            parts = " + ".join(f"{p.component_sku} x{p.quantity}" for p in gift.parts)
            print(f"  {gift.set_sku:24} {state}  ({parts})")

        if not apply_changes:
            log.info("create_gift_sets.report", file=str(path), ready=len(ready))
            print(f"\n  {len(ready)}件を登録できます  ※ 登録していません (--apply で実行)")
            return 0

    log.info("create_gift_sets.applied", file=str(path), created=len(ready))
    print(f"\n  {len(ready)}件を登録しました")
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
