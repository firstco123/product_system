"""`same_marketplace` compiles to SQL Postgres will accept.

A unit test rather than an integration one on purpose: the bug it guards is a
SQL SYNTAX error, so it is visible in the compiled statement and needs no
database. The integration coverage in
`tests/integration/test_variant_key_resolution.py` proves the query runs; this
runs everywhere, including a checkout with no Postgres.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from app.models import ChannelSkuMapping, MappingAlert
from app.services.mapping import same_marketplace


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect()))


@pytest.mark.unit
def test_a_real_marketplace_compiles_to_an_equality() -> None:
    sql = _sql(same_marketplace(ChannelSkuMapping.marketplace_id, "0393"))

    assert "marketplace_id = " in sql
    # `IS <parameter>` is what Postgres rejects outright.
    assert " IS " not in sql


@pytest.mark.unit
def test_no_marketplace_compiles_to_is_null() -> None:
    sql = _sql(same_marketplace(ChannelSkuMapping.marketplace_id, None))

    assert "marketplace_id IS NULL" in sql


@pytest.mark.unit
def test_it_works_on_the_alert_table_too() -> None:
    """Both tables carry the column and both were written the broken way."""
    assert "marketplace_id = " in _sql(same_marketplace(MappingAlert.marketplace_id, "0393"))
    assert "marketplace_id IS NULL" in _sql(same_marketplace(MappingAlert.marketplace_id, None))


@pytest.mark.unit
def test_an_empty_string_is_a_value_not_an_absence() -> None:
    """ "" and NULL are different marketplaces. Collapsing them would let a row
    with no marketplace answer for one that has a blank-named one."""
    sql = _sql(same_marketplace(ChannelSkuMapping.marketplace_id, ""))

    assert "marketplace_id = " in sql
    assert "IS NULL" not in sql
