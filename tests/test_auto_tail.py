from errors import BusinessError
from rehearsal import ManualClock, poll_at
from tests.support import ControlledScreen, HTTPCase, event, example


class CancelControlledScreen(ControlledScreen):
    def __init__(self, state):
        super().__init__(state)
        self.paused_auto_cancels = set()
        self.hidden_auto_cancels = set()

    def cancel_auto_tail(self, show, screen, cue, request):
        identity = (show, screen, cue, request)
        if identity in self.paused_auto_cancels:
            return None
        return super().cancel_auto_tail(show, screen, cue, request)

    def find_auto_cancel(self, show, screen, request):
        if (show, screen, request) in self.hidden_auto_cancels:
            return None
        return super().find_auto_cancel(show, screen, request)


class AutoTailHTTPTest(HTTPCase):
    def setUp(self):
        super().setUp()
        self.clock = ManualClock(10_000)
        self.app.clock_ms = self.clock

    def restart(self, link_factory=ControlledScreen):
        self.stop_server()
        self.start_server(link_factory)
        self.app.clock_ms = self.clock

    def start_server(self, link_factory=CancelControlledScreen, clock_ms=None):
        super().start_server(link_factory, clock_ms=clock_ms)

    def open_auto(self, delay_ms=5_000, cue='C17', expected=200, **extra):
        revision = self.publish()['revision']
        body = self.post('/open', expected, **self.binding, revision=revision,
                         auto_tail={'cue': cue, 'delay_ms': delay_ms}, **extra)
        return revision, body

    def auto_status(self, expected=200, cue='C17'):
        return self.get('/auto_tail', expected, **self.binding, cue=cue)

    def cancel(self, request='C1', expected=200, cue='C17'):
        return self.post('/auto_tail/cancel', expected, **self.binding,
                         cue=cue, request=request)

    def test_open_validation_and_reopen_rules(self):
        script = example('original')
        script['cues'].append({'cue': 'C19', 'order': 19, 'segments': ['甲', '乙']})
        revision = self.publish(script)['revision']
        bad_delay = self.post('/open', 400, **self.binding, revision=revision,
                              auto_tail={'cue': 'C17', 'delay_ms': True})
        self.assertIn('error', bad_delay)
        self.post('/open', 400, **self.binding, revision=revision,
                  auto_tail={'cue': 'C18', 'delay_ms': 100})
        self.post('/open', 400, **self.binding, revision=revision,
                  auto_tail={'cue': 'MISSING', 'delay_ms': 100})
        # The failed openings did not create the show.
        self.assertEqual(self.app.shows.get('S7').phase, 'unstarted')

        self.post('/open', **self.binding, revision=revision,
                  auto_tail={'cue': 'C17', 'delay_ms': 5_000})
        self.post('/open', **self.binding, revision=revision)
        self.post('/open', **self.binding, revision=revision,
                  auto_tail={'cue': 'C17', 'delay_ms': 5_000})
        self.post('/open', 409, **self.binding, revision=revision,
                  auto_tail={'cue': 'C17', 'delay_ms': 4_000})
        self.post('/open', 409, **self.binding, revision=revision,
                  auto_tail={'cue': 'C19', 'delay_ms': 5_000})

    def test_due_poll_lights_exactly_once_with_first_revision(self):
        old = self.publish()['revision']
        new = self.publish(example('amendment'))['revision']
        self.post('/open', **self.binding, revision=old,
                  auto_tail={'cue': 'C17', 'delay_ms': 5_000})
        self.assertEqual(self.auto_status()['status'], 'waiting')
        self.advance('C17', 1)
        armed = self.auto_status()
        self.assertEqual((armed['status'], armed['due_ms']), ('armed', 15_000))

        # A correction after the first light must not change the tail revision.
        self.correct(new)
        poll_at(self.clock, 14_999, self.app.poll_auto_tail)
        self.assertEqual(self.events(), [event(1, 'C17', 1, old)])

        results = poll_at(self.clock, 15_000, self.app.poll_auto_tail)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, 'lit')
        self.assertEqual(results[0].revision, old)
        self.assertEqual(self.auto_status()['status'], 'auto')
        self.assertEqual(self.auto_status()['sequence'], 2)
        poll_at(self.clock, 16_000, self.app.poll_auto_tail)
        self.advance('C18', 1)
        self.assertEqual(self.events(), [
            event(1, 'C17', 1, old),
            event(2, 'C17', 2, old),
            event(3, 'C18', 1, new),
        ])

    def test_late_first_report_still_arms_after_communication_returns(self):
        old, _ = self.open_auto()
        self.screen.hidden_segments.add(self.first_segment)
        self.advance('C17', 1, 202)
        self.assertIsNone(self.app.receipts.get_progress(*self.first_segment))

        poll_at(self.clock, 14_999, self.app.poll_auto_tail)
        self.assertIsNone(self.screen.outcomes.find_segment('S7', 'P1', 'C17', 2))

        # The first report remains hidden, but the screen can enforce the
        # deadline from its own committed first-light time and light the tail.
        results = poll_at(self.clock, 15_000, self.app.poll_auto_tail)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].sequence, 2)
        self.assertEqual(results[0].revision, old)
        self.assertIsNone(self.app.receipts.get_progress(*self.first_segment))

        self.screen.hidden_segments.remove(self.first_segment)
        first = self.advance('C17', 1)
        self.assertEqual((first['sequence'], first['revision']), (1, old))
        self.assertEqual(self.events(), [event(1, 'C17', 1, old), event(2, 'C17', 2, old)])

    def test_manual_early_tail_is_manual_and_blocks_auto(self):
        old, _ = self.open_auto()
        self.advance('C17', 1)
        manual = self.advance('C17', 2)
        self.assertEqual(manual['sequence'], 2)
        self.assertEqual(self.auto_status()['status'], 'manual')
        poll_at(self.clock, 20_000, self.app.poll_auto_tail)
        self.assertEqual(len(self.events()), 2)

        cancelled = self.cancel('CX')
        self.assertEqual(cancelled['status'], 'manual')
        self.assertEqual(cancelled['sequence'], 2)
        self.assertEqual(self.cancel('CX'), cancelled)

    def test_cancel_first_on_screen_preserves_original_outcome_after_manual_tail(self):
        old, _ = self.open_auto()
        self.advance('C17', 1)
        cancelled = self.cancel('CX')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIsNone(cancelled['sequence'])
        self.assertEqual(self.auto_status()['status'], 'cancelled')

        manual = self.advance('C17', 2)
        self.assertEqual(manual['status'], 'lit')
        self.assertEqual(manual['sequence'], 2)
        self.assertEqual(self.auto_status()['status'], 'manual')
        self.assertEqual(self.cancel('CX'), cancelled)
        self.assertEqual(self.events(), [event(1, 'C17', 1, old), event(2, 'C17', 2, old)])

    def test_in_flight_auto_command_is_invalidated_by_cancel(self):
        old, _ = self.open_auto()
        self.advance('C17', 1)
        self.screen.paused_segments.add(('S7', 'P1', 'C17', 2))
        poll_at(self.clock, 15_000, self.app.poll_auto_tail)
        self.assertIsNone(self.screen.outcomes.find_segment('S7', 'P1', 'C17', 2))

        cancelled = self.cancel('CX')
        self.assertEqual(cancelled['status'], 'cancelled')
        with self.assertRaisesRegex(BusinessError, 'cancelled'):
            self.screen.release_segment(('S7', 'P1', 'C17', 2))
        self.assertIsNone(self.screen.outcomes.find_segment('S7', 'P1', 'C17', 2))

        manual = self.advance('C17', 2)
        self.assertEqual(manual['sequence'], 2)
        self.assertEqual(self.events()[1], event(2, 'C17', 2, old))

    def test_same_request_continues_lost_screen_delivery(self):
        self.open_auto()
        self.advance('C17', 1)
        self.screen.paused_auto_cancels.add(('S7', 'P1', 'C17', 'CX'))
        self.cancel('CX', 202)
        self.assertIsNone(self.screen.outcomes.find_auto_cancel('S7', 'P1', 'CX'))

        self.screen.paused_auto_cancels.clear()
        continued = self.cancel('CX')
        self.assertEqual(continued['status'], 'cancelled')
        self.assertIsNotNone(self.screen.outcomes.find_auto_cancel('S7', 'P1', 'CX'))

    def test_tail_wins_race_cancel_returns_auto_sequence(self):
        self.open_auto()
        self.advance('C17', 1)
        self.screen.paused_auto_cancels.add(('S7', 'P1', 'C17', 'CX'))
        pending = self.cancel('CX', 202)
        self.assertEqual(pending['status'], 'pending')

        poll_at(self.clock, 15_000, self.app.poll_auto_tail)
        self.assertEqual(self.auto_status()['status'], 'auto')
        self.screen.paused_auto_cancels.clear()

        won = self.cancel('CX')
        self.assertEqual(won['status'], 'auto')
        self.assertEqual(won['sequence'], 2)
        self.assertEqual(self.cancel('CX'), won)

    def test_lost_cancel_reply_survives_restart_and_later_manual_light(self):
        old, _ = self.open_auto()
        self.advance('C17', 1)
        self.screen.hidden_auto_cancels.add(('S7', 'P1', 'CX'))
        pending = self.cancel('CX', 202)
        self.assertEqual(pending['status'], 'pending')
        # The screen call still executed even though its observation is hidden.
        self.assertIsNotNone(self.screen.outcomes.find_auto_cancel('S7', 'P1', 'CX'))

        self.restart(CancelControlledScreen)
        # Re-apply the test hide on the replacement link.
        self.screen.hidden_auto_cancels.add(('S7', 'P1', 'CX'))
        pending_again = self.cancel('CX', 202)
        self.assertEqual(pending_again, pending)

        self.screen.hidden_auto_cancels.clear()
        cancelled = self.cancel('CX')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.advance('C17', 2)
        self.assertEqual(self.cancel('CX'), cancelled)
        self.assertEqual(self.auto_status()['status'], 'manual')
        self.assertEqual(self.events(), [event(1, 'C17', 1, old), event(2, 'C17', 2, old)])

    def test_restart_keeps_original_deadline(self):
        old, _ = self.open_auto()
        self.advance('C17', 1)
        self.restart()
        self.assertEqual(self.auto_status()['due_ms'], 15_000)
        poll_at(self.clock, 14_999, self.app.poll_auto_tail)
        self.assertEqual(len(self.events()), 1)
        poll_at(self.clock, 15_000, self.app.poll_auto_tail)
        self.assertEqual(self.auto_status()['status'], 'auto')
        self.assertEqual(self.events()[1], event(2, 'C17', 2, old))

    def test_close_ends_opportunity_and_preserves_outcomes(self):
        old, _ = self.open_auto()
        self.advance('C17', 1)
        # Pause cancellation so it has no screen outcome at close time.
        self.screen.paused_auto_cancels.add(('S7', 'P1', 'C17', 'CX'))
        accepted = self.cancel('CX', 202)
        self.assertEqual(accepted['status'], 'pending')

        self.post('/close', show='S7')
        closed = self.cancel('CX')
        self.assertEqual(closed['status'], 'closed')
        self.assertEqual(self.cancel('CX'), closed)
        self.post('/auto_tail/cancel', 409, **self.binding, cue='C17', request='NEW')
        poll_at(self.clock, 99_999, self.app.poll_auto_tail)
        self.assertEqual(self.auto_status()['status'], 'closed')
        self.assertEqual(self.events(), [event(1, 'C17', 1, old)])

    def test_close_preserves_hidden_screen_cancel_outcome(self):
        self.open_auto()
        self.advance('C17', 1)
        self.screen.hidden_auto_cancels.add(('S7', 'P1', 'CX'))
        self.cancel('CX', 202)
        self.post('/close', show='S7')
        reclaimed = self.cancel('CX')
        self.assertEqual(reclaimed['status'], 'cancelled')
        self.assertEqual(self.cancel('CX'), reclaimed)

    def test_unknown_config_and_parameter_conflicts(self):
        self.open_auto()
        self.get('/auto_tail', 404, **self.binding, cue='C18')
        self.post('/auto_tail/cancel', 404, **self.binding, cue='C18', request='X')
        self.cancel('CX')
        self.post('/auto_tail/cancel', 409, show='S7', screen='P2', cue='C17', request='CX')
        self.post('/auto_tail/cancel', 409, **self.binding, cue='C18', request='CX')
