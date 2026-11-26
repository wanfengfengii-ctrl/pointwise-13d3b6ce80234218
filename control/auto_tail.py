from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from audit.cue_result import find_cue_result
from control.advance import advance
from errors import BusinessError, conflict, missing, name
from shows.lifecycle import ShowPhase, start_show
from storage import Database

if TYPE_CHECKING:
    from control.runtime import Runtime


@dataclass(frozen=True)
class AutoTailConfig:
    show: str
    screen: str
    cue: str
    delay_ms: int


@dataclass(frozen=True)
class AutoTailStatus:
    show: str
    screen: str
    cue: str
    status: str
    due_ms: int | None = None
    sequence: int | None = None


@dataclass(frozen=True)
class AutoCancelReceipt:
    show: str
    screen: str
    cue: str
    request: str
    status: str
    sequence: int | None = None

    @classmethod
    def from_row(cls, row):
        return cls(row['show'], row['screen'], row['cue'], row['request'],
                   row['status'], row['sequence'])


class AutoTailStore:
    def __init__(self, database: Database):
        self.database = database
        database.initialize('''
            CREATE TABLE IF NOT EXISTS auto_tails (
                show TEXT PRIMARY KEY,
                screen TEXT NOT NULL,
                cue TEXT NOT NULL,
                delay_ms INTEGER NOT NULL CHECK (delay_ms > 0)
            );
            CREATE TABLE IF NOT EXISTS auto_cancel_requests (
                show TEXT NOT NULL,
                request TEXT NOT NULL,
                screen TEXT NOT NULL,
                cue TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('pending', 'cancelled', 'manual', 'auto', 'closed')),
                sequence INTEGER,
                PRIMARY KEY (show, request)
            );
        ''')

    @staticmethod
    def _config_from_row(row) -> AutoTailConfig:
        return AutoTailConfig(row['show'], row['screen'], row['cue'], row['delay_ms'])

    def configure(self, show: str, screen: str, cue: str, delay_ms: int) -> AutoTailConfig:
        with self.database.transaction(write=True) as connection:
            connection.execute('INSERT INTO auto_tails VALUES (?, ?, ?, ?)',
                               (show, screen, cue, delay_ms))
        return AutoTailConfig(show, screen, cue, delay_ms)

    def get_config(self, show: str) -> AutoTailConfig | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                'SELECT * FROM auto_tails WHERE show = ?', (show,)
            ).fetchone()
        return None if row is None else self._config_from_row(row)

    def all_configs(self) -> list[AutoTailConfig]:
        with self.database.transaction() as connection:
            rows = connection.execute('SELECT * FROM auto_tails').fetchall()
        return [self._config_from_row(row) for row in rows]

    def find_cancel(self, show: str, request: str) -> AutoCancelReceipt | None:
        with self.database.transaction() as connection:
            row = connection.execute('''
                SELECT * FROM auto_cancel_requests WHERE show = ? AND request = ?
            ''', (show, request)).fetchone()
        return None if row is None else AutoCancelReceipt.from_row(row)

    def insert_cancel(self, show: str, screen: str, cue: str, request: str) -> AutoCancelReceipt:
        receipt = AutoCancelReceipt(show, screen, cue, request, 'pending')
        with self.database.transaction(write=True) as connection:
            connection.execute('''
                INSERT INTO auto_cancel_requests
                    (show, request, screen, cue, status, sequence)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (show, request, screen, cue, receipt.status, receipt.sequence))
        return receipt

    def finish_cancel(self, show: str, request: str, status: str,
                      sequence: int | None = None) -> AutoCancelReceipt:
        with self.database.transaction(write=True) as connection:
            connection.execute('''
                UPDATE auto_cancel_requests
                SET status = ?, sequence = ?
                WHERE show = ? AND request = ? AND status = 'pending'
            ''', (status, sequence, show, request))
            row = connection.execute('''
                SELECT * FROM auto_cancel_requests WHERE show = ? AND request = ?
            ''', (show, request)).fetchone()
        return AutoCancelReceipt.from_row(row)

    def pending_cancels(self, show: str) -> list[AutoCancelReceipt]:
        with self.database.transaction() as connection:
            rows = connection.execute('''
                SELECT * FROM auto_cancel_requests
                WHERE show = ? AND status = 'pending'
                ORDER BY rowid
            ''', (show,)).fetchall()
        return [AutoCancelReceipt.from_row(row) for row in rows]

    def has_cancelled_outcome(self, show: str, screen: str, cue: str) -> bool:
        with self.database.transaction() as connection:
            row = connection.execute('''
                SELECT 1 FROM auto_cancel_requests
                WHERE show = ? AND screen = ? AND cue = ? AND status = 'cancelled'
                LIMIT 1
            ''', (show, screen, cue)).fetchone()
        return row is not None


def parse_auto_tail(raw, edition) -> tuple[str, int]:
    if not isinstance(raw, dict):
        raise invalid_auto_tail()
    cue = name(raw.get('cue'), 'auto_tail.cue')
    delay_ms = raw.get('delay_ms')
    # bool is a subclass of int, but the request contract explicitly rejects it.
    if type(delay_ms) is not int or delay_ms <= 0:
        raise invalid_auto_tail('auto_tail.delay_ms must be a positive integer')
    cue_data = next((item for item in edition.cues if item.cue == cue), None)
    if cue_data is None or len(cue_data.segments) != 2:
        raise invalid_auto_tail('auto_tail.cue must identify a two-segment cue in the opening revision')
    return cue, delay_ms


def invalid_auto_tail(message: str = 'auto_tail must contain a cue and positive integer delay_ms'):
    from errors import invalid
    return invalid(message)


def open_with_auto_tail(runtime: Runtime, show_id: str, screen_id: str, revision_id: str,
                        raw_auto_tail, auto_tail_present: bool):
    screen = runtime.screens.get(screen_id)
    edition = runtime.editions.get(revision_id)
    current = runtime.shows.get(show_id)
    # Validate the stage transition before persisting the auto-tail configuration.
    opened = start_show(current, screen.screen, edition.revision)
    parsed = parse_auto_tail(raw_auto_tail, edition) if auto_tail_present else None
    existing = runtime.auto_tails.get_config(show_id)

    if current.phase == ShowPhase.UNSTARTED:
        selected = parsed
    else:
        if existing is None:
            if auto_tail_present:
                raise conflict('cannot add or change auto_tail after the show has opened')
            selected = None
        elif not auto_tail_present:
            selected = (existing.cue, existing.delay_ms)
        elif parsed != (existing.cue, existing.delay_ms):
            raise conflict('auto_tail configuration does not match the opening request')
        else:
            selected = parsed

    show = runtime.shows.save_open(opened)
    if selected is not None and existing is None:
        runtime.auto_tails.configure(show.show, show.screen, selected[0], selected[1])
    return show


def _screen_fact(runtime: Runtime, identity: tuple):
    event = runtime.screen.find_segment(*identity)
    if event is not None and (event.show, event.screen, event.cue, event.segment) == identity:
        return event
    return None


def observed_segment(runtime: Runtime, show: str, screen: str, cue: str, segment: int):
    identity = (show, screen, cue, segment)
    event = runtime.receipts.get_progress(*identity) or find_cue_result(runtime.ledger, *identity)
    screen_event = _screen_fact(runtime, identity)
    if screen_event is not None:
        runtime.receive_display(screen_event)
        return screen_event
    return event


def require_bound_config(runtime: Runtime, show_id: str, screen: str, cue: str):
    show = runtime.shows.get(show_id)
    if show.phase == ShowPhase.UNSTARTED or show.screen != screen:
        raise missing(f'no show {show_id} bound to screen {screen}')
    config = runtime.auto_tails.get_config(show_id)
    if config is None or config.screen != screen or config.cue != cue:
        raise missing(f'no auto-tail configuration for {show_id} on {screen} cue {cue}')
    return show, config


def _screen_cancel_outcome(runtime: Runtime, receipt: AutoCancelReceipt):
    finder = getattr(runtime.screen, 'find_auto_cancel', None)
    if not callable(finder):
        return None
    try:
        outcome = finder(receipt.show, receipt.screen, receipt.request)
    except BusinessError:
        outcome = None
    if outcome is not None:
        return runtime.auto_tails.finish_cancel(
            receipt.show, receipt.request, 'cancelled')
    return None


def _closed_resolution(runtime: Runtime, receipt: AutoCancelReceipt) -> AutoCancelReceipt:
    resolver = getattr(runtime.screen, 'resolve_auto_tail_on_close', None)
    if not callable(resolver):
        return runtime.auto_tails.finish_cancel(
            receipt.show, receipt.request, 'closed')
    try:
        outcome = resolver(receipt.show, receipt.screen, receipt.cue, receipt.request)
    except BusinessError:
        return receipt
    if outcome is None:
        return receipt
    if outcome.status == 'cancelled':
        return runtime.auto_tails.finish_cancel(receipt.show, receipt.request, 'cancelled')
    if outcome.status in ('manual', 'auto'):
        return runtime.auto_tails.finish_cancel(
            receipt.show, receipt.request, outcome.status, outcome.sequence)
    if outcome.status == 'closed':
        return runtime.auto_tails.finish_cancel(receipt.show, receipt.request, 'closed')
    return receipt


def observe_cancel(runtime: Runtime, receipt: AutoCancelReceipt) -> AutoCancelReceipt:
    if receipt.status != 'pending':
        return receipt

    cancelled = _screen_cancel_outcome(runtime, receipt)
    if cancelled is not None:
        return cancelled

    tail = observed_segment(runtime, receipt.show, receipt.screen, receipt.cue, 2)
    if tail is not None:
        status = tail.origin if tail.origin in ('manual', 'auto') else 'manual'
        return runtime.auto_tails.finish_cancel(
            receipt.show, receipt.request, status, tail.sequence)

    show = runtime.shows.get(receipt.show)
    if show.phase != ShowPhase.CLOSED:
        # The screen session can have been closed independently. Reconcile the
        # control-plane phase before resolving against the close boundary.
        if show.phase == ShowPhase.LIVE and runtime.ensure_screen(show):
            show = runtime.shows.get(receipt.show)

    if show.phase == ShowPhase.CLOSED:
        return _closed_resolution(runtime, receipt)
    return receipt


def _deliver_cancel(runtime: Runtime, show: str, screen: str, cue: str, request: str) -> None:
    cancel = getattr(runtime.screen, 'cancel_auto_tail', None)
    if not callable(cancel):
        return
    try:
        cancel(show, screen, cue, request)
    except BusinessError:
        # Closing and race conflicts are resolved by observation/close state.
        return


def cancel_auto_tail(runtime: Runtime, show_id: str, screen: str, cue: str,
                     request_id: str) -> AutoCancelReceipt:
    existing = runtime.auto_tails.find_cancel(show_id, request_id)
    if existing is not None:
        if (existing.screen, existing.cue) != (screen, cue):
            raise conflict('auto-tail cancellation request already has different parameters')
        if existing.status == 'pending':
            # Same request number and parameters continue the idempotent
            # screen-side operation if the first delivery was lost/held.
            _deliver_cancel(runtime, show_id, screen, cue, request_id)
        return observe_cancel(runtime, existing)

    show, _config = require_bound_config(runtime, show_id, screen, cue)
    if show.phase == ShowPhase.CLOSED:
        raise conflict(f'show {show_id} is closed')
    if runtime.ensure_screen(show):
        raise conflict(f'show {show_id} is closed')

    receipt = runtime.auto_tails.insert_cancel(show_id, screen, cue, request_id)
    _deliver_cancel(runtime, show_id, screen, cue, request_id)

    return observe_cancel(runtime, receipt)


def settle_closed(runtime: Runtime, show_id: str) -> None:
    for receipt in runtime.auto_tails.pending_cancels(show_id):
        observe_cancel(runtime, receipt)


def auto_tail_status(runtime: Runtime, show_id: str, screen: str, cue: str) -> AutoTailStatus:
    show, config = require_bound_config(runtime, show_id, screen, cue)
    if show.phase == ShowPhase.LIVE and runtime.ensure_screen(show):
        show = runtime.shows.get(show_id)

    tail = observed_segment(runtime, show_id, screen, cue, 2)
    if tail is not None:
        status = tail.origin if tail.origin in ('manual', 'auto') else 'manual'
        return AutoTailStatus(show_id, screen, cue, status, sequence=tail.sequence)

    screen_cancelled = None
    effective_finder = getattr(runtime.screen, 'find_effective_auto_cancel', None)
    if callable(effective_finder):
        try:
            screen_cancelled = effective_finder(show_id, screen, cue)
        except BusinessError:
            screen_cancelled = None
    control_cancelled = runtime.auto_tails.has_cancelled_outcome(show_id, screen, cue)
    if screen_cancelled is not None or control_cancelled:
        return AutoTailStatus(show_id, screen, cue, 'cancelled')

    if show.phase == ShowPhase.CLOSED:
        return AutoTailStatus(show_id, screen, cue, 'closed')

    first = observed_segment(runtime, show_id, screen, cue, 1)
    if first is None or first.lit_ms is None:
        return AutoTailStatus(show_id, screen, cue, 'waiting')
    return AutoTailStatus(show_id, screen, cue, 'armed',
                         due_ms=first.lit_ms + config.delay_ms)


def poll_auto_tail(runtime: Runtime):
    results = []
    for config in runtime.auto_tails.all_configs():
        show = runtime.shows.get(config.show)
        if show.phase != ShowPhase.LIVE:
            continue
        if runtime.ensure_screen(show):
            settle_closed(runtime, config.show)
            continue

        identity = (config.show, config.screen, config.cue, 2)
        tail = observed_segment(runtime, *identity)
        if tail is not None:
            for receipt in runtime.auto_tails.pending_cancels(config.show):
                observe_cancel(runtime, receipt)
            continue

        effective_cancel = None
        effective_finder = getattr(runtime.screen, 'find_effective_auto_cancel', None)
        if callable(effective_finder):
            try:
                effective_cancel = effective_finder(config.show, config.screen, config.cue)
            except BusinessError:
                effective_cancel = None
        if effective_cancel is not None:
            for receipt in runtime.auto_tails.pending_cancels(config.show):
                observe_cancel(runtime, receipt)
            continue

        first = observed_segment(runtime, config.show, config.screen, config.cue, 1)
        if first is not None and first.lit_ms is not None:
            if runtime.clock_ms() < first.lit_ms + config.delay_ms:
                continue
        # If the first report is hidden/lost, the screen command itself checks
        # the deadline against the authoritative actual first-light timestamp.

        try:
            result = advance(runtime, config.show, config.screen, config.cue, 2,
                             origin='auto', auto_delay_ms=config.delay_ms)
            results.append(result)
        except BusinessError:
            # Not-due, cancellation, closing and ordering races are reconciled
            # below; a poll round must never leave cancellation stuck.
            pass
        for receipt in runtime.auto_tails.pending_cancels(config.show):
            observe_cancel(runtime, receipt)
    return results
