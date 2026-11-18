"""The plan-then-apply CLIs must not open a second transaction.

`sync_shopify_masters --apply` failed on its first production run with
`InvalidRequestError: A transaction is already begun on this Session`. The
planning step reads through the session, SQLAlchemy autobegins a transaction for
those reads, and the `async with session.begin()` that followed tried to open a
second one.

The unit tests did not catch it because their fake sessions answered `execute()`
without modelling that a read starts a transaction. So the fake here does model
it, and the same shape was already present — unexercised — in
`audit_shopify_stock`, whose only tested path was `--dry-run`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import InvalidRequestError

from app.cli import link_shared_stock

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _TransactionAwareSession:
    """A session that behaves like SQLAlchemy's on the one point that matters:
    a read autobegins a transaction, and beginning a second one is an error."""

    def __init__(self, answers: list[list[Any]]) -> None:
        self._answers = answers
        self._in_transaction = False
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, _stmt: Any) -> _Result:
        self._in_transaction = True
        return _Result(self._answers.pop(0) if self._answers else [])

    def begin(self) -> Any:
        if self._in_transaction:
            raise InvalidRequestError("A transaction is already begun on this Session.")
        raise AssertionError("unreachable in these tests")

    async def get(self, _model: Any, _pk: int) -> Any:
        self._in_transaction = True

        class _Master:
            is_bundle = False

        return _Master()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1
        self._in_transaction = False

    async def __aenter__(self) -> _TransactionAwareSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _factory(session: _TransactionAwareSession) -> Any:
    def make() -> _TransactionAwareSession:
        return session

    return make


async def test_apply_commits_instead_of_opening_a_second_transaction() -> None:
    """The regression. Before the fix this raised InvalidRequestError, after the
    plan had already been printed — so the operator saw a correct-looking report
    followed by a traceback, and nothing was written."""
    session = _TransactionAwareSession(
        [
            [(1, "N108gold", False), (2, "N108gold42", False)],  # masters
            [],  # snapshots
            [],  # bundle_components
        ]
    )
    code = await link_shared_stock.run(
        pool="N108gold",
        shares=["N108gold42"],
        apply_changes=True,
        session_factory=_factory(session),
    )
    assert code == 0
    assert session.commits == 1
    assert len(session.added) == 1


async def test_a_report_only_run_writes_and_commits_nothing() -> None:
    session = _TransactionAwareSession([[(1, "N108gold", False), (2, "N108gold42", False)], [], []])
    code = await link_shared_stock.run(
        pool="N108gold",
        shares=["N108gold42"],
        apply_changes=False,
        session_factory=_factory(session),
    )
    assert code == 0
    assert session.added == []
    assert session.commits == 0


async def test_blockers_stop_the_write_even_with_apply() -> None:
    session = _TransactionAwareSession([[(1, "N108gold", False)], [], []])
    code = await link_shared_stock.run(
        pool="N108gold",
        shares=["N108goldMISSING"],
        apply_changes=True,
        session_factory=_factory(session),
    )
    assert code == 1
    assert session.added == []
    assert session.commits == 0


# --- the same mistake, guarded at the source ------------------------------
#
# These three CLIs all read to build a plan and then write it. `session.begin()`
# is correct where a session is fresh (the UI routes, the webhook handler) and
# wrong here, and the difference is invisible at the call site — which is how it
# was written three times.

PLAN_THEN_APPLY = (
    "app/cli/sync_shopify_masters.py",
    "app/cli/link_shared_stock.py",
    "app/cli/audit_shopify_stock.py",
    "app/cli/create_gift_sets.py",
)


@pytest.mark.parametrize("path", PLAN_THEN_APPLY)
def test_a_plan_then_apply_cli_does_not_call_session_begin(path: str) -> None:
    source = Path(path).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "session.begin()" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"{path} opens a transaction after reading on the same session. "
        f"Use `await session.commit()`: {offenders}"
    )
