from display.outcomes import AutoCancelOutcome, LitEvent


class AutoTailCancellation:
    """Screen-side commit boundary for cancelling automatic continuation."""

    def __init__(self, state, clock_ms=None):
        self.state = state
        self.clock_ms = clock_ms

    def cancel(self, show: str, screen: str, cue: str, request: str) -> AutoCancelOutcome:
        with self.state.transaction() as connection:
            session = self.state.session(connection, show, screen)
            prior = self.state.auto_cancel_row(connection, show, screen, request)
            if prior is not None:
                return AutoCancelOutcome(show, screen, cue, request, 'cancelled')

            tail_row = self.state.segment_row(connection, show, screen, cue, 2)
            if tail_row is not None:
                tail = LitEvent.from_row(tail_row)
                return AutoCancelOutcome(show, screen, cue, request,
                                         tail.origin, tail.sequence)

            if session['closed']:
                return AutoCancelOutcome(show, screen, cue, request, 'closed')

            cancelled_ms = None if self.clock_ms is None else self.clock_ms()
            self.state.insert_auto_cancel(connection, show, screen, cue, request, cancelled_ms)
            return AutoCancelOutcome(show, screen, cue, request, 'cancelled')

    def resolve_closed(self, show: str, screen: str, cue: str,
                       request: str) -> AutoCancelOutcome:
        """Authoritative outcome after the screen session has closed.

        Unlike the link's find_* observation methods, this reads the screen
        commit itself and is used by the close boundary to prevent an unknown
        lost reply from being misclassified as closed.
        """
        with self.state.transaction(write=False) as connection:
            self.state.session(connection, show, screen)
            prior = self.state.auto_cancel_row(connection, show, screen, request)
            if prior is not None:
                return AutoCancelOutcome(show, screen, cue, request, 'cancelled')
            tail_row = self.state.segment_row(connection, show, screen, cue, 2)
            if tail_row is not None:
                tail = LitEvent.from_row(tail_row)
                return AutoCancelOutcome(show, screen, cue, request,
                                         tail.origin, tail.sequence)
            return AutoCancelOutcome(show, screen, cue, request, 'closed')
