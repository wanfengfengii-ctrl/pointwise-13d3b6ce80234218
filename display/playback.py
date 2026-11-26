from dataclasses import dataclass
from time import time_ns

from display.local_state import ScreenStateStore
from display.outcomes import LitEvent
from display.preload import PreloadBuffer
from editions.revisions import FrozenEdition
from errors import BusinessError, conflict


@dataclass(frozen=True)
class DisplayCommand:
    show: str
    screen: str
    cue: str
    segment: int
    revision: str | None = None
    epoch: int | None = None
    origin: str = 'manual'
    auto_delay_ms: int | None = None


class AutoTailNotDue(BusinessError):
    """An automatic tail poll reached the screen before its deadline."""

    def __init__(self) -> None:
        super().__init__(409, 'auto_tail_not_due', 'automatic tail continuation is not due yet')


def wall_clock_ms() -> int:
    return time_ns() // 1_000_000


def _next_segment(edition: FrozenEdition, last) -> tuple[str, int] | None:
    if last is None:
        return edition.cues[0].cue, 1
    for index, cue in enumerate(edition.cues):
        if cue.cue != last['cue']:
            continue
        if last['segment'] < len(cue.segments):
            return cue.cue, last['segment'] + 1
        if index + 1 < len(edition.cues):
            return edition.cues[index + 1].cue, 1
        return None
    raise RuntimeError('screen event references a cue outside the bound edition')


class Playback:
    def __init__(self, state: ScreenStateStore, preloads: PreloadBuffer,
                 clock_ms=None):
        self.state = state
        self.preloads = preloads
        self.clock_ms = clock_ms or wall_clock_ms

    def light(self, command: DisplayCommand) -> LitEvent:
        show, screen = command.show, command.screen
        cue, segment = command.cue, command.segment
        with self.state.transaction() as connection:
            session = self.state.session(connection, show, screen)
            if session['closed']:
                raise conflict('the screen session is closed')
            previous = self.state.segment_row(connection, show, screen, cue, segment)
            if previous is not None:
                return LitEvent.from_row(previous)
            last = self.state.last_event(connection, show, screen)
            initial = self.state.edition(connection, session['initial_revision'])
            if _next_segment(initial, last) != (cue, segment):
                raise conflict('requested segment is out of playback order')
            if command.origin == 'auto' and self.state.auto_cancel_exists(
                    connection, show, screen, cue):
                raise conflict('automatic tail continuation has been cancelled')
            now_ms = int(self.clock_ms())
            if (command.origin == 'auto' and command.auto_delay_ms is not None
                    and last is not None and last['lit_ms'] is not None
                    and now_ms < last['lit_ms'] + command.auto_delay_ms):
                raise AutoTailNotDue()
            if segment == 1:
                revision = session['active_revision']
                if (command.revision, command.epoch) != (revision, session['epoch']):
                    raise conflict('opening command has expired')
            else:
                revision = last['revision']
            edition = self.state.edition(connection, revision)
            candidate = self.preloads.get(show, screen, cue, segment, revision)
            segment_data = candidate if candidate is not None else edition.get_segment(cue, segment)
            sequence = 1 if last is None else last['sequence'] + 1
            lit_ms = now_ms
            origin = 'auto' if command.origin == 'auto' else 'manual'
            self.state.insert_event(connection, show, screen, sequence, cue, segment,
                                    revision, segment_data.lines, lit_ms, origin)
            event = LitEvent(show, screen, sequence, cue, segment, revision,
                             segment_data.lines, lit_ms, origin)
        self.preloads.discard(show, screen, cue, segment)
        return event
