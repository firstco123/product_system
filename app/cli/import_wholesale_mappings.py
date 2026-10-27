"""Apply the reviewed wholesale correspondence table (docs/29 §5).

Input is the CSV `build_wholesale_mapping` proposes, after a person has read it.
Only rows whose `apply` column says yes are written, so the reviewer's decision
is recorded in the file rather than in whoever ran the command.

`import_mappings` and `import_variant_mappings` cannot do this: the first is the
Phase 1-A CROSS MALL one-off and the second consumes the Phase 1-B artifacts,
and both build masters from channel exports. This writes mappings onto masters
that already exist, and deliberately has no power to invent one — with a single
named exception below.

WHAT IT WRITES

One `channel_sku_mappings` row per (商品コード, 発注者コード), with
`channel='wholesale'` and `channel_variant_key` NULL. The carrier's 13-digit code
IS the sellable unit, so there is no second grain to model here — unlike Rakuten,
where the page carries several SKUs. Writes are upserts on
`uq_channel_sku_mapping`, so re-running after extending the table touches only
the new rows.

THE GIFT BAG

`IT-Gift bag` needs a master that does not exist, not a mapping onto one
(docs/29 §5-4). `--gift-bag-sku CODE` creates exactly that one master with
`is_stock_managed=false` / `non_inventory_kind=packaging` and maps the code to
it. One narrow flag for one known product, rather than a general ability to
create masters from a spreadsheet.

It must NOT be merged with the existing H1 / 10005 gift boxes — different item,
different supplier. And it must be 在庫管理対象外 or it climbs forever: 20
shipped against 154 returned in the measured period, the same shape that took
Phase 1-B's gift boxes to -12,509.

    powershell -File scripts/run_cli.ps1 -Cli import_wholesale_mappings ^
        -Args "--file csv_file/phase2/mappings/wholesale_mapping_reviewed.csv --dry-run"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.csv_intake import ColumnSpec, CsvSpec, OnEmpty, inspect, iter_rows
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MasterSku
from app.models.enums import NonInventoryKindEnum
from app.services.wholesale_import import WHOLESALE_CHANNEL
from app.wholesale_catalogue import GIFT_BAG_CODE

log = get_logger(__name__)

#: Injectable so the integration tests can bind this to the test engine, the
#: same way mark_non_inventory does. Without it a CLI test opens its own
#: connection from DATABASE_URL and writes somewhere the test cannot see.
SessionFactory = async_sessionmaker[AsyncSession]

COL_CODE = "商品コード"
COL_BUYER = "発注者コード"
COL_SKU = "sku_code"
COL_APPLY = "apply"

#: Read through the shared intake helper so this file gets the same encoding
#: detection, header aliases and line-numbered row reporting as every upload
#: screen. The reviewer edits this CSV in Excel, so it can come back UTF-8 with
#: a BOM, CP932, or with the columns reordered.
REVIEWED_SPEC = CsvSpec(
    columns=(
        ColumnSpec(canonical=COL_CODE, aliases=("product_code", "商品コード（発注用）")),
        ColumnSpec(canonical=COL_BUYER, aliases=("buyer_code", "marketplace_id")),
        ColumnSpec(canonical=COL_SKU, aliases=("master_sku", "SKUコード")),
        # A blank `apply` is a row the reviewer has not decided on. It must not
        # read as yes, and it is not an error worth stopping the file for.
        ColumnSpec(canonical=COL_APPLY, on_empty=OnEmpty.KEEP),
    ),
)

_YES = {"yes", "y", "true", "1", "はい", "○", "o"}


def wants_apply(value: str) -> bool:
    return value.strip().lower() in _YES


async def run(
    *,
    data: bytes,
    dry_run: bool,
    gift_bag_sku: str | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """Takes the file's BYTES, not its path.

    File IO stays out of the async run (ASYNC240), the same way
    `mark_non_inventory.read_csv` keeps it in a sync helper — and it makes this
    testable without a tmp file.
    """
    inspection = inspect(data, REVIEWED_SPEC)
    if inspection.fatal:
        for message in inspection.fatal:
            log.error("wholesale_mappings.unreadable", reason=message)
        return 1
    for issue in inspection.row_issues:
        log.warning("wholesale_mappings.row_issue", **issue)

    wanted: list[tuple[str, str, str]] = []
    skipped = 0
    for row in iter_rows(data, REVIEWED_SPEC):
        if not wants_apply(row[COL_APPLY]):
            skipped += 1
            continue
        code = row[COL_CODE].strip()
        sku = row[COL_SKU].strip()
        buyer = row[COL_BUYER].strip()
        if not sku:
            # `apply=yes` with no master is a reviewer slip, and guessing one is
            # exactly what this whole two-step exists to avoid.
            log.error("wholesale_mappings.no_master", product_code=code)
            return 1
        wanted.append((code, buyer, sku))

    if not wanted and gift_bag_sku is None:
        log.error("wholesale_mappings.nothing_to_apply", skipped=skipped)
        return 1

    factory = session_factory or async_session_factory
    async with factory() as session:
        masters = await _master_ids(session, {sku for _, _, sku in wanted})
        missing = sorted({sku for _, _, sku in wanted if sku not in masters})
        if missing:
            # Stop on the whole file: a table referring to masters that are gone
            # was built against a different catalogue, so the rows that DO
            # resolve were matched under assumptions that no longer hold.
            log.error("wholesale_mappings.unknown_masters", sku_codes=missing)
            return 1

        rows: list[dict[str, object]] = [
            {
                "master_sku_id": masters[sku],
                "channel": WHOLESALE_CHANNEL,
                "channel_sku": code,
                "channel_variant_key": None,
                "marketplace_id": buyer or None,
                "is_active": True,
            }
            for code, buyer, sku in wanted
        ]

        if gift_bag_sku is not None:
            gift_row = await _gift_bag_row(
                session,
                sku_code=gift_bag_sku,
                buyer=next((buyer for _, buyer, _ in wanted), ""),
                dry_run=dry_run,
            )
            if gift_row is not None:
                rows.append(gift_row)

        if dry_run:
            # Not `row`: that name is already bound to an `iter_rows` dict above,
            # and reusing it makes this list look like the CSV's own rows.
            for mapping_row in rows:
                log.info("wholesale_mappings.would_write", **_loggable(mapping_row))
            log.info(
                "wholesale_mappings.dry_run",
                mappings=len(rows),
                skipped=skipped,
                gift_bag=gift_bag_sku,
            )
            print(f"dry-run: mappings={len(rows)} skipped={skipped}")
            return 0

        statement = pg_insert(ChannelSkuMapping).values(rows)
        await session.execute(
            statement.on_conflict_do_update(
                constraint="uq_channel_sku_mapping",
                set_={
                    "master_sku_id": statement.excluded.master_sku_id,
                    "is_active": statement.excluded.is_active,
                },
            )
        )
        await session.commit()

    log.info(
        "wholesale_mappings.applied",
        mappings=len(rows),
        skipped=skipped,
        gift_bag=gift_bag_sku,
    )
    print(f"applied: mappings={len(rows)} skipped={skipped}")
    return 0


async def _master_ids(session: AsyncSession, sku_codes: set[str]) -> dict[str, int]:
    if not sku_codes:
        return {}
    found = await session.execute(
        select(MasterSku.sku_code, MasterSku.id).where(MasterSku.sku_code.in_(sku_codes))
    )
    return dict(found.tuples().all())


async def _gift_bag_row(
    session: AsyncSession,
    *,
    sku_code: str,
    buyer: str,
    dry_run: bool,
) -> dict[str, object] | None:
    """Create the 在庫管理対象外 packaging master and return its mapping row.

    None on a dry run that would have had to create the master: there is no id
    to report yet, and inventing a placeholder one would put a row in the
    preview that does not describe anything real.
    """
    existing = await session.execute(select(MasterSku).where(MasterSku.sku_code == sku_code))
    master = existing.scalar_one_or_none()
    if master is None and dry_run:
        log.info("wholesale_mappings.would_create_gift_bag", sku_code=sku_code)
        return None
    if master is None:
        master = MasterSku(
            sku_code=sku_code,
            name="Gift bag（卸売・梱包材）",
            is_stock_managed=False,
            non_inventory_kind=NonInventoryKindEnum.PACKAGING,
        )
        session.add(master)
        await session.flush()
        log.info("wholesale_mappings.gift_bag_created", sku_code=sku_code, id=master.id)
    elif master is not None and master.is_stock_managed:
        # Refusing rather than flipping the flag: a stocked master under this
        # code is something else, and turning off its stock management would
        # abandon whatever quantity it holds.
        raise ValueError(
            f"{sku_code} は在庫管理対象のマスタとして既に存在します。"
            "別のコードを指定するか、既存マスタを確認してください。"
        )
    assert master is not None  # created above, or found
    return {
        "master_sku_id": master.id,
        "channel": WHOLESALE_CHANNEL,
        "channel_sku": GIFT_BAG_CODE,
        "channel_variant_key": None,
        "marketplace_id": buyer or None,
        "is_active": True,
    }


def _loggable(row: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in row.items() if key != "channel_variant_key"}


def main() -> None:
    p = argparse.ArgumentParser(description="Apply the reviewed wholesale mapping table")
    p.add_argument("--file", required=True, help="the reviewed proposal CSV")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--gift-bag-sku",
        default=None,
        help=(
            "create this sku_code as a 在庫管理対象外 packaging master and map "
            f"{GIFT_BAG_CODE} to it (docs/29 §5-4)"
        ),
    )
    args = p.parse_args()
    configure_logging("INFO")
    path = Path(args.file)
    if not path.exists():
        log.error("wholesale_mappings.missing_file", path=str(path))
        sys.exit(1)
    sys.exit(
        asyncio.run(
            run(
                data=path.read_bytes(),
                dry_run=args.dry_run,
                gift_bag_sku=args.gift_bag_sku,
            )
        )
    )


if __name__ == "__main__":
    main()
