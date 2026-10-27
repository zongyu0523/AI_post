"""What a channel is: something that can put one Post somewhere, and prove it.

A channel's whole contract with the gate is:

  * raise, as long as nothing could have fired  -> `failed`, safe to re-run
  * call `attempt.firing(...)` on the line before the irreversible action
  * call `attempt.prove(...)` only with something read back off the screen -> `success`
  * return without proving -> `uncertain`

`readiness()` is advisory - it feeds the UI, it is not the gate. An earlier project's lesson
is about exactly this confusion:
"can I run it right now" is a different question from "does it exist", and answering the second with
the first makes a platform deny its own capabilities. The run itself is the oracle, so a channel
re-checks for real at publish time even when `readiness()` said yes.
"""
from __future__ import annotations

from store import Post

from gate import Attempt


class ChannelNotReady(RuntimeError):
    """Something needed is missing, and we checked before anything could fire.

    Reaching the gate with this means `failed`: provably nothing was published, so re-running after
    you fix the cause is safe.
    """


class Channel:
    key: str = ""
    label: str = ""
    runtime: str = "browser"   # "browser" | "desktop" - the UI warns differently for each
    hint: str = ""             # one line shown under the channel in the UI

    def readiness(self) -> tuple[bool, str]:
        """(ready, reason). Advisory only - see the module docstring."""
        return True, ""

    def check_post(self, post: Post) -> None:
        """Reject a post this channel could never verify. Called at the write boundary."""
        return None

    async def publish(self, post: Post, attempt: Attempt, *, dry_run: bool, prefix: str) -> None:
        raise NotImplementedError
