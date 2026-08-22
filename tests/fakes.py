"""Test doubles for unit-testing BuildOrderBot without a live SC2 game.

The executor's methods are tested UNBOUND against a lightweight fake `self` —
e.g. `BuildOrderBot.do_train(fake, step)` — so we never construct BotAI or touch
the game. A method is testable this way as long as it only reads attributes and
simple method-returns we can fake here; that covers the sequencing brain
(triggers, ordering, prewalk) and each handler's hold-vs-proceed gating.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


class FakeUnits(list):
    """Minimal stand-in for python-sc2's `Units`: a list that also answers the
    chained filters our code uses (`.ready` / `.idle` / `.amount` / `.first`) and
    is falsy when empty. `.ready`/`.idle` return self, so a test passes in exactly
    the units a real filter would have already yielded (we're testing the gating
    decision, not python-sc2's own filtering)."""

    @property
    def ready(self) -> "FakeUnits":
        return self

    @property
    def idle(self) -> "FakeUnits":
        return self

    @property
    def amount(self) -> int:
        return len(self)

    @property
    def first(self):
        return self[0]

    def filter(self, fn) -> "FakeUnits":
        return FakeUnits(u for u in self if fn(u))

    def tags_not_in(self, tags) -> "FakeUnits":
        return FakeUnits(u for u in self if getattr(u, "tag", None) not in tags)

    @property
    def gathering(self) -> "FakeUnits":
        return FakeUnits(u for u in self if u.is_gathering)

    def closest_to(self, pos):
        return min(self, key=lambda u: u.position.distance_to(pos))


def fake_unit(**attrs) -> MagicMock:
    """A stand-in game unit. It's callable (so `unit(ability)` records a call) and
    carries whatever attributes a test sets (e.g. energy=100)."""
    u = MagicMock()
    for k, v in attrs.items():
        setattr(u, k, v)
    return u


def fake_bot(**attrs) -> SimpleNamespace:
    """A fake `self` for calling BuildOrderBot methods unbound. Pass exactly the
    attributes/stubs the method under test reads; anything unset raises if touched
    (a useful signal the method needs more than the test provided)."""
    return SimpleNamespace(**attrs)
