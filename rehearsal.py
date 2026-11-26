"""Deterministic rehearsal tools for the local caption service."""
import unittest
from collections import defaultdict
from functools import partial
from threading import Lock

from control.runtime import LocalScreenLink
from tests.support import HTTPCase


class ManualClock:
    def __init__(self, start_ms=0):
        self._now = start_ms
        self._lock = Lock()

    def __call__(self):
        with self._lock:
            return self._now

    def set(self, now_ms):
        with self._lock:
            if now_ms < self._now:
                raise ValueError("rehearsal time cannot go backwards")
            self._now = now_ms


def poll_at(clock, now_ms, poll):
    clock.set(now_ms)
    return poll()


class Delivery:
    """Hold calls before execution or hide the result of an observation."""

    def __init__(self):
        self._paused = set()
        self._hidden = set()
        self._queued = defaultdict(list)
        self._lock = Lock()

    def pause(self, topic):
        with self._lock:
            self._paused.add(topic)

    def hide(self, topic, hidden=True):
        with self._lock:
            if hidden:
                self._hidden.add(topic)
            else:
                self._hidden.discard(topic)

    def send(self, topic, target, *args, **kwargs):
        with self._lock:
            if topic in self._paused:
                self._queued[topic].append(partial(target, *args, **kwargs))
                return None
        return target(*args, **kwargs)

    def take(self, topic):
        with self._lock:
            self._paused.discard(topic)
            return tuple(self._queued.pop(topic, ()))

    def read(self, topic, target, *args, **kwargs):
        with self._lock:
            if topic in self._hidden:
                return None
        return target(*args, **kwargs)


class RehearsalLink(LocalScreenLink):
    def __init__(self, state):
        super().__init__(state)
        self.delivery = Delivery()

    def send_segment(self, command):
        return self.delivery.send("segment", super().send_segment, command)

    def find_segment(self, show, screen, cue, segment):
        return self.delivery.read("segment-result", super().find_segment,
                                  show, screen, cue, segment)

    def cancel_auto_tail(self, show, screen, cue, request):
        return self.delivery.send(
            "cancel", super().cancel_auto_tail, show, screen, cue, request)

    def find_auto_cancel(self, show, screen, request):
        return self.delivery.read(
            "cancel-result", super().find_auto_cancel, show, screen, request)

    def find_effective_auto_cancel(self, show, screen, cue):
        return self.delivery.read(
            "cancel-result", super().find_effective_auto_cancel, show, screen, cue)


class FixtureCase(HTTPCase):
    def start_server(self, link_factory=RehearsalLink, clock_ms=None):
        super().start_server(link_factory, clock_ms=clock_ms)

    def test_v1_delivery(self):
        revision = self.publish()["revision"]
        self.post("/open", **self.binding, revision=revision)
        clock = ManualClock()
        self.screen.delivery.hide("segment-result")
        pending = poll_at(clock, 10000, lambda: self.advance("C17", 1, expected=202))
        self.assertEqual((clock(), pending["status"]), (10000, "pending"))
        first = self.screen.outcomes.find_segment(*self.first_segment)
        self.assertEqual((first.sequence, first.revision), (1, revision))
        self.screen.delivery.hide("segment-result", False)

        self.screen.delivery.pause("segment")
        self.advance("C17", 2, expected=202)
        self.assertIsNone(self.screen.outcomes.find_segment("S7", "P1", "C17", 2))
        (deliver,) = self.screen.delivery.take("segment")
        deliver()
        deliver()
        self.assertEqual(self.events(), [
            {"cue": "C17", "segment": 1, "revision": revision, "sequence": 1},
            {"cue": "C17", "segment": 2, "revision": revision, "sequence": 2},
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
