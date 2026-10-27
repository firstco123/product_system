"""Propose the wholesale correspondence table from the carrier's own files (§5).

Reads one or more 流通BMS files, pulls out every distinct 商品コード（発注用）and
its 規格カナ, matches each against our masters, and writes a CSV for review.

NOTHING IS WRITTEN TO THE DATABASE. This produces a proposal; `import_mappings`
applies the reviewed file. The split is the point — docs/29 §5-2 is explicit that
the 規格カナ rules are a tool for BUILDING the table, not a way to resolve a code
at runtime, and inference belongs where someone can disagree with it.

WHAT TO READ IN THE OUTPUT

`kind` says how much each row needs looking at:

    exact       the tail matched once, nothing assumed.
    rule        a client agreement decided it (docs/29 §1) — B34's implied 22cm,
                B66's 19CM meaning M, B33's two sizes sharing one master. The
                `note` names the agreement and its date.
    colourless  matched only by dropping the colour, because no master for that
                token carries one. Right for R63 and the two keyrings; the one
                kind of row that could point a silver code at a gold master if a
                colour axis were added later. READ THESE.
    ambiguous   several masters matched. Choose one by hand.
    unmatched   none did.
    non_product the gift bag (docs/29 §5-4), which needs a 在庫管理対象外 master
                rather than a mapping.

`apply` is prefilled `yes` only for exact and rule rows. Everything else comes
out `no` with the candidates listed, so applying the file untouched does the
unarguable 71 and leaves the 6 that want a decision.

The master list comes from the database, so a master created since the last run
is included automatically — which also means a run BEFORE a rename and one after
will propose different tables. Keep the reviewed file.

    powershell -File scripts/run_cli.ps1 -Cli build_wholesale_mapping ^
        -Args "--files csv_file/phase2/source/受注伝票_20260916123229.csv"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.csv_export import csv_body, write_csv
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import MasterSku
from app.services.sku_scope import operational_conditions
from app.wholesale_bms import read_bms
from app.wholesale_catalogue import CodeMatch, MatchKind, build_table, summarise

log = get_logger(__name__)

#: Injectable for tests; see import_wholesale_mappings.
SessionFactory = async_sessionmaker[AsyncSession]

DEFAULT_OUT = Path("csv_file/phase2/mappings/wholesale_mapping_proposal.csv")

#: 発注者コード is in the file because it is the `marketplace_id` every wholesale
#: mapping is keyed on. Leaving it out would mean the importer hardcoding 0393 —
#: the kind of buried constant that is correct until the day a second wholesale
#: customer appears and then wrong somewhere nobody is looking.
HEADER = (
    "商品コード",
    "発注者コード",
    "規格カナ",
    "kind",
    "sku_code",
    "apply",
    "candidates",
    "note",
)


def _row(match: CodeMatch, buyer_code: str) -> tuple[str, ...]:
    return (
        match.product_code,
        buyer_code,
        match.spec_kana,
        match.kind.value,
        match.sku_code or "",
        "yes" if match.applicable else "no",
        " / ".join(match.candidates),
        match.note,
    )


async def run(
    *,
    files: list[Path],
    out_path: Path = DEFAULT_OUT,
    session_factory: SessionFactory | None = None,
) -> int:
    pairs: set[tuple[str, str]] = set()
    # One code can in principle arrive under more than one 発注者コード, and each
    # pairing is its own mapping row. Every one of the 2,222 lines measured
    # carried 0393, so this is a set of one today.
    buyers: dict[str, set[str]] = {}
    for path in files:
        if not path.exists():
            log.error("wholesale_mapping.missing_file", path=str(path))
            return 1
        reading = read_bms(path.read_bytes())
        if reading.fatal:
            # A file this unreadable has no trustworthy code list either: the
            # same damage that breaks a 取引番号 can truncate a 商品コード.
            for message in reading.fatal:
                log.error("wholesale_mapping.unreadable", path=str(path), reason=message)
            return 1
        for voucher in reading.vouchers:
            for line in voucher.lines:
                pairs.add((line.product_code, line.spec_kana))
                buyers.setdefault(line.product_code, set()).add(voucher.buyer_code)
        log.info(
            "wholesale_mapping.read",
            path=str(path),
            kind=str(reading.kind),
            codes=len({code for code, _ in pairs}),
        )

    if not pairs:
        log.error("wholesale_mapping.no_codes")
        return 1

    factory = session_factory or async_session_factory
    async with factory() as session:
        # Archived and 在庫管理対象外 masters are excluded: a wholesale code
        # must not be mapped onto a master the rest of the system has retired.
        # `operational_conditions` is the same filter the count sheets use, so
        # the two cannot disagree about which masters exist.
        rows = await session.execute(select(MasterSku.sku_code).where(*operational_conditions()))
        sku_codes = sorted(rows.scalars().all())

    table = build_table(sorted(pairs), sku_codes)
    counts = summarise(table)
    body = csv_body(
        HEADER,
        [
            _row(match, buyer)
            for match in table
            for buyer in sorted(buyers.get(match.product_code, {""}))
        ],
    )
    written = write_csv(out_path, body)

    log.info(
        "wholesale_mapping.proposed",
        path=str(written),
        masters=len(sku_codes),
        codes=len(table),
        **counts,
    )
    needs_review = [
        match
        for match in table
        if match.kind in (MatchKind.COLOURLESS, MatchKind.AMBIGUOUS, MatchKind.UNMATCHED)
    ]
    for match in needs_review:
        log.warning(
            "wholesale_mapping.needs_review",
            product_code=match.product_code,
            spec_kana=match.spec_kana,
            kind=match.kind.value,
            proposed=match.sku_code,
            candidates=list(match.candidates),
        )
    print(f"{written}")
    tallies = " ".join(f"{name}={count}" for name, count in counts.items())
    print(f"masters={len(sku_codes)} codes={len(table)} {tallies}")
    print(f"review={len(needs_review)} apply={sum(1 for m in table if m.applicable)}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Propose the wholesale correspondence table")
    p.add_argument(
        "--files",
        required=True,
        help="comma-separated 流通BMS files (受注伝票 and/or 受領・返品)",
    )
    p.add_argument("--out", default=str(DEFAULT_OUT))
    args = p.parse_args()
    configure_logging("INFO")
    paths = [Path(part.strip()) for part in args.files.split(",") if part.strip()]
    sys.exit(asyncio.run(run(files=paths, out_path=Path(args.out))))


if __name__ == "__main__":
    main()
