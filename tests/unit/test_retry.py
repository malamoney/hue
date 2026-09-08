"""When the Gateway asks the Bridge again, and when it must not.

The rule these tests exist for is the one that cannot be recovered from once
it is broken: a mutation the Bridge may already have applied is never sent
twice.
"""

from __future__ import annotations

import pytest

from hue_grpc.hue.retry import SAFE_METHODS, Retry

#: The schedule with the jitter taken out, so a test can see the ceiling it
#: was drawn from.
UNJITTERED = Retry(attempts=4, base_delay=0.05, max_delay=0.4, jitter=lambda c: c)


def test_a_mutation_is_never_asked_again() -> None:
    """A `PUT` that failed after the Bridge acted cannot be told from one
    that failed before, so the Gateway does not get to guess."""
    for method in ("PUT", "POST", "PATCH", "DELETE", "put"):
        assert list(Retry().pauses(method)) == []


def test_a_safe_read_is_asked_again_up_to_its_attempts() -> None:
    """Three attempts is two pauses: the first try does not wait."""
    assert len(list(Retry(attempts=3).pauses("GET"))) == 2
    assert list(Retry(attempts=1).pauses("GET")) == []


def test_the_wait_grows_and_then_stops_growing() -> None:
    assert list(UNJITTERED.pauses("GET")) == [0.05, 0.1, 0.2]
    assert list(UNJITTERED.pauses("get")) == [0.05, 0.1, 0.2]

    capped = Retry(attempts=6, base_delay=0.1, max_delay=0.2, jitter=lambda c: c)

    assert list(capped.pauses("GET")) == [0.1, 0.2, 0.2, 0.2, 0.2]


def test_the_wait_is_jittered_within_its_ceiling() -> None:
    """Unjittered backoff synchronises every client that failed together."""
    ceilings = list(UNJITTERED.pauses("GET"))
    jittered = Retry(attempts=4, base_delay=0.05, max_delay=0.4)
    drawn = [tuple(jittered.pauses("GET")) for _ in range(50)]

    for pauses in drawn:
        assert all(
            0.0 <= pause <= ceiling
            for pause, ceiling in zip(pauses, ceilings, strict=True)
        )
    # Fifty identical schedules would mean no jitter at all.
    assert len(set(drawn)) > 1


def test_a_retry_policy_that_would_never_try_anything_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one attempt"):
        Retry(attempts=0)


def test_safe_means_reads() -> None:
    assert frozenset({"GET", "HEAD"}) == SAFE_METHODS
