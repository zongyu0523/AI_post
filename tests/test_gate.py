"""The gate's whole truth table, exhaustively, plus the two properties that matter.

Run from AI_post:  python -m pytest tests -q
"""
import itertools

import pytest

from gate import FAILED, SUCCESS, UNCERTAIN, Attempt, FireStage, settle


def build(fired: bool, proved: bool) -> Attempt:
    a = Attempt(channel="test")
    if fired:
        a.firing("publish")
    if proved:
        a.prove("read_back", "the post text")
    return a


# (fired, proved, errored) -> status
EXPECTED = {
    (False, False, False): FAILED,     # did nothing, proved nothing
    (False, False, True): FAILED,      # broke before firing -> safe to re-run
    (False, True, False): SUCCESS,     # a dry run that read its own composed text back
    (False, True, True): SUCCESS,      # proof stands; the error is cleanup
    (True, False, False): UNCERTAIN,   # clicked publish, could not confirm
    (True, False, True): UNCERTAIN,    # clicked publish, then broke
    (True, True, False): SUCCESS,      # the normal good path
    (True, True, True): SUCCESS,       # proved live, then the browser failed to close
}


@pytest.mark.parametrize("fired,proved,errored", list(itertools.product([False, True], repeat=3)))
def test_truth_table(fired, proved, errored):
    outcome = settle(build(fired, proved), RuntimeError("boom") if errored else None)
    assert outcome.status == EXPECTED[(fired, proved, errored)]


def test_no_evidence_is_never_success():
    """Rule 1. The failure this project exists to prevent is a green run with nothing behind it."""
    for fired, errored in itertools.product([False, True], repeat=2):
        outcome = settle(build(fired, False), RuntimeError("x") if errored else None)
        assert outcome.status != SUCCESS
        assert outcome.evidence is None


def test_only_a_provable_non_firing_is_retryable():
    """Rule 2. `retryable` must never be true once the publish action may have happened."""
    for proved, errored in itertools.product([False, True], repeat=2):
        after_firing = settle(build(True, proved), RuntimeError("x") if errored else None)
        assert not after_firing.retryable
    assert settle(build(False, False)).retryable


def test_error_after_proof_does_not_erase_the_proof():
    """The bug this file caught: cleanup failing after read-back once read as 'never published'."""
    outcome = settle(build(True, True), RuntimeError("context close failed"))
    assert outcome.status == SUCCESS
    assert "context close failed" in outcome.detail
    assert outcome.evidence is not None


def test_firing_is_recorded_in_the_steps():
    a = build(True, True)
    assert any(s.startswith("FIRING:") for s in a.steps), a.steps
    assert settle(a).stage is FireStage.UNKNOWN
