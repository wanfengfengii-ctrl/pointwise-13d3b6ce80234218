import json
from dataclasses import dataclass

from display.local_state import ScreenStateStore


@dataclass(frozen=True)
class LitEvent:
    show: str
    screen: str
    sequence: int
    cue: str
    segment: int
    revision: str
    lines: tuple[str, ...]
    lit_ms: int | None = None
    origin: str = 'manual'

    @classmethod
    def from_row(cls, row):
        keys = row.keys()
        return cls(row['show'], row['screen'], row['sequence'], row['cue'], row['segment'],
                   row['revision'], tuple(json.loads(row['lines'])),
                   row['lit_ms'] if 'lit_ms' in keys else None,
                   row['origin'] if 'origin' in keys else 'manual')


@dataclass(frozen=True)
class AutoCancelOutcome:
    show: str
    screen: str
    cue: str
    request: str
    status: str = 'cancelled'
    sequence: int | None = None


@dataclass(frozen=True)
class CutoverOutcome:
    show: str
    screen: str
    request: str
    revision: str
    epoch: int


class ScreenOutcomes:
    def __init__(self, state: ScreenStateStore):
        self.state = state

    def find_segment(self, show: str, screen: str, cue: str, segment: int) -> LitEvent | None:
        row = self.state.read_segment(show, screen, cue, segment)
        return None if row is None else LitEvent.from_row(row)

    def find_cutover(self, show: str, screen: str, request: str) -> CutoverOutcome | None:
        row = self.state.read_cutover(show, screen, request)
        if row is None:
            return None
        return CutoverOutcome(row['show'], row['screen'], row['request'], row['revision'], row['epoch'])

    def find_auto_cancel(self, show: str, screen: str, request: str) -> AutoCancelOutcome | None:
        row = self.state.read_auto_cancel(show, screen, request)
        if row is None:
            return None
        return AutoCancelOutcome(row['show'], row['screen'], row['cue'], row['request'], 'cancelled')

    def find_effective_auto_cancel(self, show: str, screen: str, cue: str) -> AutoCancelOutcome | None:
        row = self.state.read_effective_auto_cancel(show, screen, cue)
        if row is None:
            return None
        return AutoCancelOutcome(row['show'], row['screen'], row['cue'], row['request'], 'cancelled')

    def list_events(self, show: str, screen: str) -> list[LitEvent]:
        return [LitEvent.from_row(row) for row in self.state.read_events(show, screen)]
