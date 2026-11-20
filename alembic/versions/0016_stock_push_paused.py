"""stock_push_paused_at — hold a master's channel stock writes without disabling it

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-19

Phase 2 Stage 1. Client decision of 2026-11-19: during the Christmas selling
period they will set the 48 gift sets' Shopify quantities by hand, so the hourly
derived-availability push must not overwrite them.

WHY A NEW COLUMN AND NOT `is_stock_managed = false`

That flag is the obvious lever and it is a trap. `InventoryService`
`resolve_consumption` tests it on the PARENT first and returns an empty list, so
a set flagged that way deducts NOTHING from its components on any sale — it
disables the feature this pause exists to protect. The client asked to stop the
WRITING, not the deducting, and nothing in the schema could express that
difference until now.

Nor can the hold be done by deactivating the sets' `channel_sku_mappings` rows:
those rows are what `ingest._lookup_mapping` resolves an incoming Shopify line
on, so deactivating them turns every set order into a MappingAlert and, again,
deducts nothing.

WHY A TIMESTAMP AND A REASON, NOT A BOOLEAN

The same shape as `archived_at` / `archived_reason` in this table, for the same
purpose: a hold that outlives the season it was taken for is a silent fault —
sets that stop tracking their components in January and nobody notices until the
shop oversells. The timestamp is what makes "paused since 11-19, still paused in
March" answerable, and `app.cli.pause_stock_push --list` reads both.

WHY THIS MIGRATION TOUCHES ZERO ROWS

NULL means 「自動書き込みの対象」 and is the default for every existing master,
so the scheduled push behaves exactly as it does today until a pause is set
deliberately by CLI. There is nothing to backfill: which masters the client
wants held by hand is a decision, not something derivable in SQL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_TABLE = "master_skus"
_CHECK = "ck_master_sku_stock_push_paused_reason"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("stock_push_paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("stock_push_paused_reason", sa.String(length=255), nullable=True),
    )
    # A reason with no pause is a row that reads as held and is not, which is
    # the one combination an operator could act on wrongly. The reverse is
    # allowed: a pause taken in a hurry may carry no reason.
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        "stock_push_paused_reason IS NULL OR stock_push_paused_at IS NOT NULL",
    )


def downgrade() -> None:
    # Safe to drop unconditionally: losing the column restores the previous
    # behaviour (every master is pushed), which is the state before any pause
    # was taken. It does mean a live hold is silently lifted, so a downgrade
    # while the shop is selling by hand would start overwriting their numbers
    # within the hour — check `pause_stock_push --list` first.
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, "stock_push_paused_reason")
    op.drop_column(_TABLE, "stock_push_paused_at")
