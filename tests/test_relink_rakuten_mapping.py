"""Repointing a Rakuten mapping at a different master.

The B46 case: page 10099 sells one length, but both its SKU管理番号 were mapped to
the BRACELET masters rather than the ANKLET ones. The dangerous direction is the
one that created the defect — pointing a Rakuten SKU at a master that derives its
stock (`is_bundle`) instead of holding it. Such a master pushes
`compute_bundle_available()`, which is 0 when it has no components, so the live
page goes 在庫切れ while the real stock sits on the pool master. Most of this file
is about refusing that, and refusing it loudly enough that the run is known to
have done nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.cli.relink_rakuten_mapping import BUNDLE_TARGET, Move, parse_move, plan, run

pytestmark = pytest.mark.unit


class _Mapping:
    def __init__(self, mapping_id: int, master_sku_id: int, *, is_active: bool = True) -> None:
        self.id = mapping_id
        self.master_sku_id = master_sku_id
        self.is_active = is_active


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Answers `plan`'s reads in order: the target masters, then ONE mapping read
    per move that gets as far as looking its mapping up, then — only when at
    least one move was plannable — the targets' existing rows for the
    coarse-target check.

    A move refused on its target (missing / archived / is_bundle) never issues a
    mapping read, so the answers after the first belong only to the moves that
    survive that check. `held` defaults to no existing rows, which is the shape
    of the real B46 job: both anklet masters carry no Rakuten mapping yet.
    """

    def __init__(
        self,
        masters: list[tuple[int, str, bool, datetime | None]],
        mappings: list[list[Any]] | None = None,
        held: list[tuple[int, int, str, str | None]] | None = None,
    ) -> None:
        self._answers: list[list[Any]] = [masters, *(mappings or []), held or []]
        self.committed = False
        self.by_id: dict[int, _Mapping] = {}

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._answers.pop(0) if self._answers else [])

    async def get(self, _model: Any, pk: int) -> Any:
        return self.by_id.get(pk)

    async def commit(self) -> None:
        self.committed = True


def _factory(session: _FakeSession) -> Any:
    class _Factory:
        def __call__(self) -> Any:
            return self

        async def __aenter__(self) -> _FakeSession:
            return session

        async def __aexit__(self, *_: Any) -> None:
            return None

    return _Factory()


ANKLET = (250, "B46goldanklet", False, None)
SILVER_ANKLET = (251, "B46silveranklet", False, None)
BRACELET = (77, "B46goldbraceret", True, None)


# ---------------------------------------------------------------- parse_move


def test_a_move_without_a_variant_key_names_the_page_level_row() -> None:
    assert parse_move("10099=B46goldanklet") == Move("10099", None, "B46goldanklet")


def test_a_move_with_a_variant_key_names_one_sku_on_that_page() -> None:
    assert parse_move("10099/10099-gold=B46goldanklet") == Move(
        "10099", "10099-gold", "B46goldanklet"
    )


@pytest.mark.parametrize(
    "spec",
    [
        "10099/10099-gold",  # no target
        "=B46goldanklet",  # no sku
        "10099/=B46goldanklet",  # slash but no variant key
        "",
    ],
)
def test_a_malformed_move_is_rejected_before_any_read(spec: str) -> None:
    with pytest.raises(ValueError, match="書式"):
        parse_move(spec)


# ---------------------------------------------------------------- plan


@pytest.mark.asyncio
async def test_a_mapping_is_repointed_at_the_named_master() -> None:
    session = _FakeSession([ANKLET], [[(_Mapping(9197, 77), "B46goldbraceret")]])

    result = await plan(session, [Move("10099", "10099-gold", "B46goldanklet")])  # type: ignore[arg-type]

    assert not result.blockers
    assert len(result.ready()) == 1
    repoint = result.ready()[0]
    assert repoint.mapping_id == 9197
    assert repoint.target_id == 250
    assert repoint.current_sku == "B46goldbraceret"


@pytest.mark.asyncio
async def test_a_shared_stock_target_is_refused_because_it_holds_no_stock() -> None:
    """The B46 defect itself. The repair tool must not be able to recreate it."""
    session = _FakeSession([BRACELET])

    result = await plan(session, [Move("10099", "10099-gold", "B46goldbraceret")])  # type: ignore[arg-type]

    assert not result.repoints
    assert [b.reason for b in result.blockers] == [BUNDLE_TARGET.format(sku="B46goldbraceret")]


@pytest.mark.asyncio
async def test_an_archived_target_is_refused() -> None:
    archived = (99, "10048c", False, datetime(2026, 10, 19, tzinfo=UTC))
    session = _FakeSession([archived])

    result = await plan(session, [Move("10048", "1148", "10048c")])  # type: ignore[arg-type]

    assert not result.repoints
    assert "使用停止" in result.blockers[0].reason


@pytest.mark.asyncio
async def test_a_target_that_does_not_exist_is_refused() -> None:
    session = _FakeSession([])

    result = await plan(session, [Move("10079", "1295", "N43gold")])  # type: ignore[arg-type]

    assert not result.repoints
    assert "存在しません" in result.blockers[0].reason


@pytest.mark.asyncio
async def test_a_mapping_that_does_not_exist_is_refused() -> None:
    session = _FakeSession([ANKLET], [[]])

    result = await plan(session, [Move("10099", "10099-gold", "B46goldanklet")])  # type: ignore[arg-type]

    assert not result.repoints
    assert result.blockers[0].reason == "対応づけが見つかりません"


@pytest.mark.asyncio
async def test_an_inactive_mapping_is_not_quietly_resurrected() -> None:
    session = _FakeSession([ANKLET], [[(_Mapping(1440, 77, is_active=False), "B46goldbraceret")]])

    result = await plan(session, [Move("10099", "10099-gold", "B46goldanklet")])  # type: ignore[arg-type]

    assert not result.repoints
    assert result.blockers[0].reason == "対応づけが無効化されています"


@pytest.mark.asyncio
async def test_a_mapping_already_on_the_target_reports_done_not_missing() -> None:
    """A second run has to be safe, and has to say so."""
    session = _FakeSession([ANKLET], [[(_Mapping(9197, 250), "B46goldanklet")]])

    result = await plan(session, [Move("10099", "10099-gold", "B46goldanklet")])  # type: ignore[arg-type]

    assert not result.blockers
    assert result.ready() == []
    assert result.repoints[0].already_done


@pytest.mark.asyncio
async def test_a_duplicate_mapping_is_refused_rather_than_guessed() -> None:
    rows = [(_Mapping(1, 77), "B46goldbraceret"), (_Mapping(2, 78), "B46silverbraceret")]
    session = _FakeSession([ANKLET], [rows])

    result = await plan(session, [Move("10099", "10099-gold", "B46goldanklet")])  # type: ignore[arg-type]

    assert not result.repoints
    assert "特定できません" in result.blockers[0].reason


@pytest.mark.asyncio
async def test_the_same_row_named_twice_is_refused_rather_than_applied_twice() -> None:
    """Both moves would be reported as done while only the last one landed, so the
    operator's record of a production repair would be wrong in two directions."""
    gold, silver = _Mapping(4676, 77), _Mapping(4676, 77)
    session = _FakeSession(
        [ANKLET, SILVER_ANKLET],
        [[(gold, "B46goldbraceret")], [(silver, "B46goldbraceret")]],
    )

    result = await plan(
        session,  # type: ignore[arg-type]
        [
            Move("10099-gold", None, "B46goldanklet"),
            Move("10099-gold", None, "B46silveranklet"),
        ],
    )

    assert len(result.repoints) == 1
    assert result.blockers[0].reason == "同じ対応づけを 10099-gold でも指定しています"


@pytest.mark.asyncio
async def test_two_skus_of_one_page_on_one_master_is_refused() -> None:
    """That is the 57-master shape: screen_targets then refuses the whole page, so
    it would stop receiving stock entirely."""
    session = _FakeSession(
        [ANKLET],
        [[(_Mapping(9197, 77), "B46goldbraceret")], [(_Mapping(9198, 78), "B46silverbraceret")]],
    )

    result = await plan(
        session,  # type: ignore[arg-type]
        [
            Move("10099", "10099-gold", "B46goldanklet"),
            Move("10099", "10099-silver", "B46goldanklet"),
        ],
    )

    assert len(result.blockers) == 2
    assert "10099-gold・10099-silver を同時に持つことになり" in result.blockers[0].reason


@pytest.mark.asyncio
async def test_a_sku_landing_beside_one_the_target_already_holds_is_refused() -> None:
    """The collision does not have to be inside this run to be real."""
    session = _FakeSession(
        [ANKLET],
        [[(_Mapping(9197, 77), "B46goldbraceret")]],
        held=[(5000, 250, "10099", "10099-silver")],
    )

    result = await plan(session, [Move("10099", "10099-gold", "B46goldanklet")])  # type: ignore[arg-type]

    assert len(result.blockers) == 1
    assert "在庫を書き込めなくなります" in result.blockers[0].reason


@pytest.mark.asyncio
async def test_the_real_b46_job_of_four_moves_is_not_refused() -> None:
    """Both grains of both colours at once — the command this CLI was written for.
    The page-level rows (no variant key) must not count toward the collision."""
    session = _FakeSession(
        [ANKLET, SILVER_ANKLET],
        [
            [(_Mapping(9197, 77), "B46goldbraceret")],
            [(_Mapping(9198, 78), "B46silverbraceret")],
            [(_Mapping(4676, 77), "B46goldbraceret")],
            [(_Mapping(4678, 78), "B46silverbraceret")],
        ],
    )

    result = await plan(
        session,  # type: ignore[arg-type]
        [
            Move("10099", "10099-gold", "B46goldanklet"),
            Move("10099", "10099-silver", "B46silveranklet"),
            Move("10099-gold", None, "B46goldanklet"),
            Move("10099-silver", None, "B46silveranklet"),
        ],
    )

    assert result.blockers == []
    assert len(result.ready()) == 4


# ---------------------------------------------------------------- run


@pytest.mark.asyncio
async def test_one_blocker_stops_every_move_in_the_run() -> None:
    """The good move must not be applied just because it was listed first."""
    session = _FakeSession([ANKLET, BRACELET], [[(_Mapping(9197, 77), "B46goldbraceret")]])

    code = await run(
        specs=["10099/10099-gold=B46goldanklet", "10099/10099-silver=B46goldbraceret"],
        apply_changes=True,
        session_factory=_factory(session),
    )

    assert code == 1
    assert not session.committed


@pytest.mark.asyncio
async def test_a_report_run_writes_nothing() -> None:
    mapping = _Mapping(9197, 77)
    session = _FakeSession([ANKLET], [[(mapping, "B46goldbraceret")]])
    session.by_id = {9197: mapping}

    code = await run(
        specs=["10099/10099-gold=B46goldanklet"],
        session_factory=_factory(session),
    )

    assert code == 0
    assert not session.committed
    assert mapping.master_sku_id == 77


@pytest.mark.asyncio
async def test_an_apply_run_repoints_and_commits() -> None:
    mapping = _Mapping(9197, 77)
    session = _FakeSession([ANKLET], [[(mapping, "B46goldbraceret")]])
    session.by_id = {9197: mapping}

    code = await run(
        specs=["10099/10099-gold=B46goldanklet"],
        apply_changes=True,
        session_factory=_factory(session),
    )

    assert code == 0
    assert session.committed
    assert mapping.master_sku_id == 250


@pytest.mark.asyncio
async def test_a_malformed_spec_exits_before_opening_a_session() -> None:
    code = await run(specs=["nonsense"], session_factory=None)

    assert code == 2
