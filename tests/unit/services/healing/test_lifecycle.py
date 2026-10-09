"""The healing-case stage machine, checked over every (stage, event) pair."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from embry0.services.healing.lifecycle import (
    OPEN_UNACCEPTED_STAGES,
    CaseEvent,
    Effect,
    IllegalTransitionError,
    Stage,
    fix_deployed,
    transition,
)

S, E = Stage, CaseEvent

# Written out independently of the implementation: one row per event, one
# cell per stage in lifecycle order. A cell is the resulting stage plus how
# the event landed, or None where the transition is illegal. Two events have
# two rows each, because their outcome depends on a fact about the case's
# data that the machine has to be told: pr_closed on whether another PR of
# the case is still open, deployed on whether the deploy can anchor (a merge
# exists and the deploy is not older than it), merged on whether the merge
# belongs to the current fix attempt (not one a reopen already discarded).
_A, _R, _L, _N, _P = Effect.APPLIED, Effect.REPEAT, Effect.LATE, Effect.NOTED, Effect.POST_CLOSE
_STAGES = [S.DETECTED, S.TRACKED, S.ACCEPTED, S.PR_OPEN, S.MERGED, S.DEPLOYED, S.VERIFIED, S.CLOSED]
_X = None
_POST = (S.CLOSED, _P)

Row = list[tuple[Stage, Effect] | None]
# Key: (event, other_open_prs, deploy_anchors, merge_current) — defaults (False, True, True).
EXPECTED: dict[tuple[CaseEvent, bool, bool, bool], Row] = {
    #                              detected          tracked           accepted          pr_open           merged            deployed          verified          closed
    (E.TRACKER_ATTACHED, False, True, True): [(S.TRACKED, _A), (S.TRACKED, _R), (S.ACCEPTED, _L), (S.PR_OPEN, _L), (S.MERGED, _L), (S.DEPLOYED, _L), (S.VERIFIED, _L), _POST],
    (E.ACCEPTED, False, True, True): [(S.ACCEPTED, _A), (S.ACCEPTED, _A), (S.ACCEPTED, _R), (S.PR_OPEN, _L), (S.MERGED, _L), (S.DEPLOYED, _L), (S.VERIFIED, _L), _POST],
    (E.PR_OPENED, False, True, True): [(S.PR_OPEN, _A), (S.PR_OPEN, _A), (S.PR_OPEN, _A), (S.PR_OPEN, _R), (S.MERGED, _L), (S.DEPLOYED, _L), (S.VERIFIED, _L), _POST],
    (E.PR_UPDATED, False, True, True): [(S.DETECTED, _N), (S.TRACKED, _N), (S.ACCEPTED, _N), (S.PR_OPEN, _N), (S.MERGED, _N), (S.DEPLOYED, _N), (S.VERIFIED, _N), _POST],
    (E.PR_CLOSED, False, True, True): [(S.DETECTED, _N), (S.TRACKED, _N), (S.ACCEPTED, _N), (S.ACCEPTED, _A), (S.MERGED, _N), (S.DEPLOYED, _N), (S.VERIFIED, _N), _POST],
    (E.PR_CLOSED, True, True, True): [(S.DETECTED, _N), (S.TRACKED, _N), (S.ACCEPTED, _N), (S.PR_OPEN, _N), (S.MERGED, _N), (S.DEPLOYED, _N), (S.VERIFIED, _N), _POST],
    (E.MERGED, False, True, True): [(S.MERGED, _A), (S.MERGED, _A), (S.MERGED, _A), (S.MERGED, _A), (S.MERGED, _R), (S.DEPLOYED, _L), (S.VERIFIED, _L), _POST],
    (E.MERGED, False, True, False): [(S.DETECTED, _N), (S.TRACKED, _N), (S.ACCEPTED, _N), (S.PR_OPEN, _N), (S.MERGED, _N), (S.DEPLOYED, _N), (S.VERIFIED, _N), _POST],
    (E.DEPLOYED, False, True, True): [(S.DEPLOYED, _A), (S.DEPLOYED, _A), (S.DEPLOYED, _A), (S.DEPLOYED, _A), (S.DEPLOYED, _A), (S.DEPLOYED, _R), (S.VERIFIED, _L), _POST],
    (E.DEPLOYED, False, False, True): [(S.DETECTED, _N), (S.TRACKED, _N), (S.ACCEPTED, _N), (S.PR_OPEN, _N), (S.MERGED, _N), (S.DEPLOYED, _N), (S.VERIFIED, _N), _POST],
    (E.VERDICT_RECORDED, False, True, True): [(S.VERIFIED, _A), (S.VERIFIED, _A), (S.VERIFIED, _A), (S.VERIFIED, _A), (S.VERIFIED, _A), (S.VERIFIED, _A), (S.VERIFIED, _R), _POST],
    (E.RECURRENCE, False, True, True): [(S.DETECTED, _N), (S.TRACKED, _N), (S.ACCEPTED, _N), (S.PR_OPEN, _N), (S.MERGED, _N), (S.DEPLOYED, _N), (S.VERIFIED, _N), _POST],
    (E.ABANDONED, False, True, True): [(S.DETECTED, _N), (S.TRACKED, _N), (S.CLOSED, _A), (S.PR_OPEN, _N), (S.MERGED, _N), (S.DEPLOYED, _N), (S.VERIFIED, _N), _POST],
    (E.REOPENED, False, True, True): [_X, _X, (S.ACCEPTED, _R), _X, _X, _X, (S.ACCEPTED, _A), _X],
    (E.CLOSED, False, True, True): [(S.CLOSED, _A)] * 7 + [(S.CLOSED, _R)],
}  # fmt: skip

_PAIRS = [(stage, event, others, anchors, current) for (event, others, anchors, current) in EXPECTED for stage in Stage]


def _expected(
    stage: Stage, event: CaseEvent, others: bool, anchors: bool, current: bool
) -> tuple[Stage, Effect] | None:
    return EXPECTED[(event, others, anchors, current)][_STAGES.index(stage)]


def test_the_expectation_table_is_exhaustive() -> None:
    assert list(Stage) == _STAGES
    assert {event for event, *_ in EXPECTED} == set(CaseEvent)
    assert all(len(row) == len(_STAGES) for row in EXPECTED.values())
    assert len(_PAIRS) == 120


@pytest.mark.parametrize(("stage", "event", "others", "anchors", "current"), _PAIRS)
def test_every_pair(stage: Stage, event: CaseEvent, others: bool, anchors: bool, current: bool) -> None:
    expected = _expected(stage, event, others, anchors, current)
    if expected is None:
        with pytest.raises(IllegalTransitionError) as err:
            transition(stage, event, other_open_prs=others, deploy_anchors=anchors, merge_current=current)
        assert err.value.stage is stage
        assert err.value.event is event
        return
    result = transition(stage, event, other_open_prs=others, deploy_anchors=anchors, merge_current=current)
    assert (result.stage, result.effect) == expected


@pytest.mark.parametrize(("stage", "event", "others", "anchors", "current"), _PAIRS)
def test_reapplying_a_landed_event_changes_nothing(
    stage: Stage, event: CaseEvent, others: bool, anchors: bool, current: bool
) -> None:
    """Delivering the same event twice leaves the case where the first left it."""
    if _expected(stage, event, others, anchors, current) is None:
        return
    first = transition(stage, event, other_open_prs=others, deploy_anchors=anchors, merge_current=current)
    second = transition(first.stage, event, other_open_prs=others, deploy_anchors=anchors, merge_current=current)
    assert second.stage is first.stage
    assert second.effect is not Effect.APPLIED


def test_each_guard_only_matters_to_its_own_event() -> None:
    for stage, event in itertools.product(Stage, CaseEvent):
        guards = (
            ("other_open_prs", CaseEvent.PR_CLOSED),
            ("deploy_anchors", CaseEvent.DEPLOYED),
            ("merge_current", CaseEvent.MERGED),
        )
        for guard, owner in guards:
            if event is owner:
                continue
            outcomes = []
            for value in (True, False):
                try:
                    outcomes.append(transition(stage, event, **{guard: value}))
                except IllegalTransitionError:
                    outcomes.append(None)
            assert outcomes[0] == outcomes[1]


def test_a_deploy_is_never_refused() -> None:
    # A fast deploy marker can beat the merge webhook. Refusing it would roll
    # the transaction back and lose the deploy; it is noted and kept instead.
    for stage in Stage:
        assert transition(stage, E.DEPLOYED, deploy_anchors=False).stage is stage
    assert transition(Stage.MERGED, E.DEPLOYED, deploy_anchors=True).stage is Stage.DEPLOYED


def test_a_closed_case_only_refuses_a_reopen() -> None:
    # M6: what happens after close is history, not an error. A regression
    # is a new case, so reopened stays illegal.
    for event in CaseEvent:
        if event is CaseEvent.REOPENED:
            with pytest.raises(IllegalTransitionError):
                transition(Stage.CLOSED, event)
        else:
            assert transition(Stage.CLOSED, event).stage is Stage.CLOSED


def test_only_a_reopen_or_a_closed_pr_moves_a_case_backwards() -> None:
    for stage, event, others, anchors, current in _PAIRS:
        expected = _expected(stage, event, others, anchors, current)
        if expected is not None and _STAGES.index(expected[0]) < _STAGES.index(stage):
            assert event in (CaseEvent.REOPENED, CaseEvent.PR_CLOSED)


def test_a_transition_names_the_stage_its_event_stands_for() -> None:
    # M5a: a late event stamps this stage's timestamp when it is still empty.
    assert transition(Stage.MERGED, E.PR_OPENED).target is Stage.PR_OPEN
    assert transition(Stage.ACCEPTED, E.TRACKER_ATTACHED).target is Stage.TRACKED
    assert transition(Stage.DETECTED, E.MERGED).target is Stage.MERGED
    assert transition(Stage.PR_OPEN, E.RECURRENCE).target is None
    assert transition(Stage.PR_OPEN, E.PR_CLOSED).target is None
    assert transition(Stage.VERIFIED, E.REOPENED).target is None


def test_a_transition_says_when_it_moves_the_case_backwards() -> None:
    # The service keeps the first <stage>_at on a backward move (funnel timings).
    for stage, event, others, anchors, current in _PAIRS:
        expected = _expected(stage, event, others, anchors, current)
        if expected is None:
            continue
        result = transition(stage, event, other_open_prs=others, deploy_anchors=anchors, merge_current=current)
        assert result.backward == (_STAGES.index(result.stage) < _STAGES.index(stage))


def test_abandoning_closes_only_an_accepted_case() -> None:
    # #410: the honest end of a fix attempt nobody will finish. An accepted
    # case has an attempt to give up, so it closes; anywhere else this is
    # history. Past pr_open the change request outlives the job that opened
    # it, and before accepted nothing was ever dispatched — closing either
    # would throw away work or a pending decision.
    assert transition(S.ACCEPTED, E.ABANDONED).stage is S.CLOSED
    assert transition(S.ACCEPTED, E.ABANDONED).effect is Effect.APPLIED
    for stage in (S.DETECTED, S.TRACKED, S.PR_OPEN, S.MERGED, S.DEPLOYED, S.VERIFIED):
        landed = transition(stage, E.ABANDONED)
        assert (landed.stage, landed.effect) == (stage, Effect.NOTED), stage
    # It names the stage it stands for, so a post-close redelivery still dates
    # closed_at when nothing else has.
    assert transition(S.ACCEPTED, E.ABANDONED).target is S.CLOSED
    assert transition(S.CLOSED, E.ABANDONED).effect is Effect.POST_CLOSE


def test_a_verdict_needs_no_merge() -> None:
    # OQ2: vendor credits topped up, or an error that stopped by itself,
    # can be confirmed healed with no fix ever merged.
    assert transition(Stage.TRACKED, E.VERDICT_RECORDED).stage is Stage.VERIFIED


def test_the_happy_path_walks_every_stage_in_order() -> None:
    stage = Stage.DETECTED
    walked = [stage]
    for event in (
        E.TRACKER_ATTACHED,
        E.ACCEPTED,
        E.PR_OPENED,
        E.MERGED,
        E.DEPLOYED,
        E.VERDICT_RECORDED,
        E.CLOSED,
    ):
        stage = transition(stage, event).stage
        walked.append(stage)
    assert walked == _STAGES


def test_an_out_of_band_merge_skips_the_pipeline_stages() -> None:
    # D4 / P1.5: a human fixes a tracked finding; the case still gets a verdict.
    assert transition(Stage.TRACKED, E.MERGED).stage is Stage.MERGED
    assert transition(Stage.MERGED, E.VERDICT_RECORDED).stage is Stage.VERIFIED


def test_accepts_plain_strings_from_a_database_row() -> None:
    assert transition("detected", "tracker_attached").stage is Stage.TRACKED
    with pytest.raises(ValueError):
        transition("nonsense", "closed")


def test_open_unaccepted_stages_are_the_pre_acceptance_ones() -> None:
    # P1.6: the proposal cap counts exactly these.
    assert OPEN_UNACCEPTED_STAGES == (Stage.DETECTED, Stage.TRACKED)


def test_fix_deployed_needs_a_merge_and_a_deploy_no_older_than_it() -> None:
    merged = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
    assert fix_deployed(merge_sha="m", merged_at=merged, deployed_at=merged)
    assert fix_deployed(merge_sha="m", merged_at=merged, deployed_at=merged + timedelta(hours=1))
    # A deploy that predates the merge cannot contain it.
    assert not fix_deployed(merge_sha="m", merged_at=merged, deployed_at=merged - timedelta(hours=4))
    assert not fix_deployed(merge_sha="m", merged_at=merged, deployed_at=None)
    assert not fix_deployed(merge_sha=None, merged_at=merged, deployed_at=merged)
    assert not fix_deployed(merge_sha="m", merged_at=None, deployed_at=merged)
