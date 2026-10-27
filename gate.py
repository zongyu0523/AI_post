"""The publish gate - the one invariant this project exists to keep.

Publishing is a side effect you cannot take back. The expensive failure is not "it didn't post";
it is "we told you it posted when it didn't", or "we retried something that had already fired, and
now the same thing is on your page twice". Two rules, enforced in `settle()` and nowhere else:

  1. **No evidence, no success.** A channel may only reach `success` by reading the published post
     back off the screen. "The click didn't raise an exception" is not evidence.

  2. **Only a provable non-firing may be retried.** A channel calls `Attempt.firing()` on the line
     *immediately before* the action that publishes. Anything that goes wrong while the stage is
     still NOT_FIRED is `failed` - safe to re-run. Anything after is `uncertain` - a human looks,
     and nothing re-runs by itself.

Both rules were paid for elsewhere:

  * `harness_brain/tests/test_browser_false_success_gate.py` - a workflow browser node landed GREEN
    with `{"post_url": null}` while the runner was offline: every browser call errored and success
    was fabricated. Rule 1 is the completion-time evidence gate from that fix.

  * a mail lane that can prove nothing
    was sent because each error names the stage it died at, and everything before `MAIL FROM` means
    the envelope never opened. `FireStage` is that idea with one bit instead of a signature table,
    because here there is exactly one line that fires.

`uncertain` is a first-class terminal state, not a kind of failure. It is the honest answer to
"did it post?" when we clicked publish and then lost the thread, and it is the only state that
asks the human to go and look.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

SUCCESS = "success"
UNCERTAIN = "uncertain"
FAILED = "failed"
STOPPED = "stopped"

TERMINAL = (SUCCESS, UNCERTAIN, FAILED, STOPPED)


class Stopped(Exception):
    """The user pressed stop. Raised at a step boundary, never inside an action."""


class FireStage(str, Enum):
    """Whether the irreversible action may have happened yet."""

    NOT_FIRED = "not_fired"  # provably before the publish action - re-running is safe
    UNKNOWN = "unknown"      # publish may have gone through - re-running could double-post


@dataclass(frozen=True)
class Evidence:
    """Something read back off the page/screen *after* publishing, that we did not write."""

    kind: str                 # "feed_text_match" | "post_url" | "note_text_match" | ...
    value: str                # the matched text, or the permalink
    shot: str | None = None   # screenshot filename under SHOTS_DIR

    def to_json(self) -> str:
        return json.dumps({"kind": self.kind, "value": self.value, "shot": self.shot},
                          ensure_ascii=False)


@dataclass
class Attempt:
    """Mutable running record of one publish attempt. Channels own it; `settle()` reads it."""

    channel: str
    dry_run: bool = False
    stage: FireStage = FireStage.NOT_FIRED
    evidence: Evidence | None = None
    steps: list[str] = field(default_factory=list)
    stop_requested: bool = False

    def request_stop(self) -> None:
        """Ask the run to stop. It will, at its next step boundary."""
        self.stop_requested = True

    def check_stop(self) -> None:
        """Channels call this between steps - never mid-action."""
        if self.stop_requested:
            raise Stopped("你按了終止")

    def step(self, what: str) -> None:
        """Log a step. Cheap, and it is what you read when a run goes wrong."""
        self.steps.append(what)

    def firing(self, what: str) -> None:
        """Call this on the line BEFORE the action that publishes. One-way."""
        self.steps.append(f"FIRING: {what}")
        self.stage = FireStage.UNKNOWN

    def prove(self, kind: str, value: str, shot: str | None = None) -> None:
        """Attach read-back evidence. The only way to reach `success`."""
        self.evidence = Evidence(kind=kind, value=value, shot=shot)
        self.steps.append(f"proved via {kind}: {value[:60]!r}")


@dataclass(frozen=True)
class Outcome:
    status: str
    stage: FireStage
    detail: str
    evidence: Evidence | None
    steps: tuple[str, ...]

    @property
    def retryable(self) -> bool:
        """Both of these are reachable only with stage NOT_FIRED, so re-running is safe."""
        return self.status in (FAILED, STOPPED)


def settle(attempt: Attempt, error: BaseException | None = None) -> Outcome:
    """Decide the outcome of an attempt. The whole policy is this table.

    | evidence | stage     | status    |
    |----------|-----------|-----------|
    | yes      | any       | success   |  we read the post back; it is live
    | none     | UNKNOWN   | uncertain |  publish was clicked, could not confirm -> a human looks
    | none     | NOT_FIRED | failed    |  provably never published -> safe to re-run

    `error` decides nothing, only the wording of `detail`. That is the point: an exception raised
    *after* read-back proof - the browser failing to close, a screenshot failing to save - is about
    cleanup, not about publishing, and must not turn proof that the post is live into "provably
    never published". Evidence can only be attached by `prove()`, and a channel that raised before
    reaching it has no evidence anyway, so the table stays safe in the other direction too.

    A dry run never calls `firing()`, so it stays NOT_FIRED and still needs evidence - the composed
    text, read back out of the editor - to count as a success. A dry run whose only output is "no
    exception" tells you nothing about whether the real run would work.
    """
    if isinstance(error, Stopped) and attempt.stage is FireStage.NOT_FIRED:
        # Stopped before anything could fire. Not a failure - nothing went wrong, and nothing
        # was published, so re-running it later is safe.
        return Outcome(status=STOPPED, stage=attempt.stage, detail="你按了終止，還沒發出去",
                       evidence=attempt.evidence, steps=tuple(attempt.steps))

    if attempt.evidence is not None:
        status = SUCCESS
        detail = ("預演完成：內容都備好了，停在發布前"
                  if attempt.evidence.kind == "composed_not_published"
                  else "已經在畫面上看到你的貼文了")
        if error is not None:
            detail += f"（確認之後才出的狀況：{error}）"
    elif attempt.stage is FireStage.UNKNOWN:
        status = UNCERTAIN
        detail = (f"按下發布之後出了狀況：{error}。可能已經發出去了，請自己看一眼"
                  if error else "按下發布了，但沒能在畫面上確認 —— 請自己看一眼")
    else:
        status = FAILED
        detail = (f"{error}".strip() if error else "沒有發出去，也沒有留下任何證據")

    return Outcome(
        status=status,
        stage=attempt.stage,
        detail=detail,
        evidence=attempt.evidence,
        steps=tuple(attempt.steps),
    )
