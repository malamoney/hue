"""When the Gateway asks the Bridge again, and when it must not.

Retrying is a decision about the request, not about the failure. A `GET` that
never reached the Bridge and a `GET` that reached it and was lost on the way
back are the same `GET`, so asking again costs nothing but time. A `PUT` is
not: a mutation that failed after the Bridge acted on it cannot be told apart
from one that failed before, and repeating it is the Gateway deciding to
change the lights twice on the strength of a guess.

So the policy is keyed on the HTTP method rather than on the exception, and
an unsafe method yields no schedule at all — there is no failure that unlocks
one. Which failures are worth retrying a safe read for is
`hue_grpc.hue.transport`'s decision; how long to wait between them is here.

The wait is jittered because it is not the only one: several clients that
lost the same Bridge at the same moment retry on the same schedule, and an
unjittered one has them all come back together, which is the load that kept
the Bridge from answering in the first place. Full jitter — a uniform draw
from zero to the ceiling — spreads them out.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass

__all__ = ["SAFE_METHODS", "Retry", "full_jitter"]

#: The HTTP methods that change nothing, and so can be sent twice. Every other
#: method — `PUT` and `POST` are the ones this Gateway sends — is sent once.
SAFE_METHODS = frozenset({"GET", "HEAD"})


def full_jitter(ceiling: float) -> float:
    """A uniform draw from zero to `ceiling`.

    Public because the event stream's reconnect schedule needs the same draw
    for the same reason, and two spellings of it would be two things to keep
    honest. What differs between the two policies is how long they go on for,
    which is the dataclass around this rather than this.
    """
    return random.uniform(0.0, ceiling)


@dataclass(frozen=True)
class Retry:
    """How many times a safe read is tried, and how long it waits between.

    The defaults are small on purpose. Every pause is spent inside the
    client's deadline, and a Bridge on the same LAN either answers quickly or
    is not there.
    """

    #: Including the first one, so `attempts=1` never retries anything.
    attempts: int = 3
    #: The ceiling on the first pause. It doubles for each one after.
    base_delay: float = 0.05
    #: Where the doubling stops.
    max_delay: float = 1.0
    #: How a pause is drawn from its ceiling. Injectable so that a test can
    #: see the schedule the jitter is drawn from.
    jitter: Callable[[float], float] = full_jitter

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(f"a request needs at least one attempt: {self.attempts}")

    def pauses(self, method: str) -> Iterator[float]:
        """How long to wait before each retry of `method`, in order.

        Empty for anything that is not a safe read, and one shorter than
        `attempts` for one that is: the first attempt does not wait.
        """
        if method.upper() not in SAFE_METHODS:
            return
        for retry in range(self.attempts - 1):
            yield self.jitter(min(self.max_delay, self.base_delay * 2**retry))
