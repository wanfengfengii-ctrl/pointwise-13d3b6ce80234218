import argparse
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock, Thread
from urllib.parse import urlencode

from audit.ingest import DisplayLedger
from control.auto_tail import AutoTailStore, poll_auto_tail
from control.http_api import make_handler, request_json, serve
from control.receipts import CorrectionReceipt, ReceiptStore
from display.auto_control import AutoTailCancellation
from display.cutover import Cutover
from display.local_state import ScreenStateStore
from display.outcomes import LitEvent, ScreenOutcomes
from display.playback import DisplayCommand, Playback, wall_clock_ms
from display.preload import PreloadBuffer
from editions.catalog import EditionCatalog
from editions.revisions import FrozenEdition
from errors import BusinessError, conflict, invalid
from shows.lifecycle import Show, finish_show, require_live_binding
from shows.local_state import ShowStore
from shows.screens import Screen, ScreenRegistry
from storage import Database


class LocalScreenLink:
    def __init__(self, state: ScreenStateStore, clock_ms=None):
        self.state = state
        self.clock_ms = clock_ms or wall_clock_ms
        self.preloads = PreloadBuffer()
        self.playback = Playback(state, self.preloads, self.clock_ms)
        self.cutover = Cutover(state)
        self.auto_cancellation = AutoTailCancellation(state, self.clock_ms)
        self.outcomes = ScreenOutcomes(state)

    def send_segment(self, command: DisplayCommand) -> None:
        self.playback.light(command)

    def find_segment(self, show: str, screen: str, cue: str, segment: int):
        return self.outcomes.find_segment(show, screen, cue, segment)

    def cancel_auto_tail(self, show: str, screen: str, cue: str, request: str):
        return self.auto_cancellation.cancel(show, screen, cue, request)

    def find_auto_cancel(self, show: str, screen: str, request: str):
        return self.outcomes.find_auto_cancel(show, screen, request)

    def find_effective_auto_cancel(self, show: str, screen: str, cue: str):
        return self.outcomes.find_effective_auto_cancel(show, screen, cue)

    def resolve_auto_tail_on_close(self, show: str, screen: str, cue: str, request: str):
        return self.auto_cancellation.resolve_closed(show, screen, cue, request)

    def prepare_cutover(self, receipt: CorrectionReceipt, edition: FrozenEdition) -> None:
        self.cutover.prepare(receipt.show, receipt.screen, receipt.request, edition, receipt.epoch)

    def apply_cutover(self, receipt: CorrectionReceipt) -> None:
        self.cutover.apply(receipt.show, receipt.screen, receipt.request)

    def find_cutover(self, show: str, screen: str, request: str):
        return self.outcomes.find_cutover(show, screen, request)

    def list_events(self, show: str, screen: str):
        return self.outcomes.list_events(show, screen)


class Runtime:
    def __init__(self, control_db: Database, screen: LocalScreenLink, clock_ms=None):
        self.clock_ms = clock_ms or wall_clock_ms
        self.editions = EditionCatalog(control_db)
        self.screens = ScreenRegistry(control_db)
        self.shows = ShowStore(control_db)
        self.receipts = ReceiptStore(control_db)
        self.auto_tails = AutoTailStore(control_db)
        self.ledger = DisplayLedger(control_db)
        self.screen = screen
        self.operator_lock = RLock()
        self._bind_screen_clock()

    def _bind_screen_clock(self) -> None:
        # Resolve through the runtime so tests can replace runtime.clock_ms
        # after construction as well as inject it through build_runtime().
        reader = self._read_clock_ms
        self.screen.clock_ms = reader
        playback = getattr(self.screen, 'playback', None)
        if playback is not None:
            playback.clock_ms = reader
        cancellation = getattr(self.screen, 'auto_cancellation', None)
        if cancellation is not None:
            cancellation.clock_ms = reader

    def _read_clock_ms(self) -> int:
        return self.clock_ms()

    def poll_auto_tail(self):
        with self.operator_lock:
            return poll_auto_tail(self)

    def ensure_screen(self, show: Show) -> bool:
        initial = self.editions.get(show.initial_revision)
        closed = self.screen.state.ensure_session(show.show, show.screen, initial)
        if closed:
            self.shows.save_closed(finish_show(show))
        return closed

    def live_show(self, show: str, screen: str) -> Show:
        bound = require_live_binding(self.shows.get(show), screen)
        if self.ensure_screen(bound):
            raise conflict(f'show {show} is closed')
        return bound

    def stop_screen(self, show: Show) -> None:
        self.ensure_screen(show)
        self.screen.state.close_session(show.show, show.screen)
        self.screen.preloads.discard(show.show, show.screen)

    def receive_display(self, event: LitEvent) -> None:
        self.ledger.accept(event)
        self.receipts.record_display_result(event)


def load_screen_manifest(path: Path | None = None) -> tuple[Screen, ...]:
    if path is None:
        return (Screen('P1', 6),)
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise invalid(f'cannot read screen manifest: {error}') from error
    rows = data.get('screens') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise invalid('screen manifest must contain a screens list of objects')
    screens = tuple(Screen(row.get('screen'), row.get('width')) for row in rows)
    if len({screen.screen for screen in screens}) != len(screens):
        raise invalid('screen manifest contains duplicate screen identifiers')
    return screens


def build_runtime(data_dir: Path, screens=None,
                  link_factory=LocalScreenLink, clock_ms=None) -> Runtime:
    # Tolerate the convenient build_runtime(path, clock_callable) form while
    # preserving the historical screens/link_factory positional parameters.
    if callable(screens) and clock_ms is None:
        clock_ms = screens
        screens = None
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    control_db = Database(directory / 'control.sqlite3')
    screen_db = Database(directory / 'display.sqlite3')
    screen = link_factory(ScreenStateStore(screen_db))
    runtime = Runtime(control_db, screen, clock_ms=clock_ms)
    runtime.screens.add_missing(load_screen_manifest() if screens is None else screens)
    for show in runtime.shows.list_live():
        runtime.ensure_screen(show)
    return runtime


def _demo() -> None:
    examples = Path(__file__).resolve().parent.parent / 'examples'
    with TemporaryDirectory(prefix='qingyu-demo-') as directory:
        now_ms = [10_000]
        runtime = build_runtime(Path(directory), clock_ms=lambda: now_ms[0])
        with ThreadingHTTPServer(('127.0.0.1', 0), make_handler(runtime)) as server:
            port = server.server_port
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
            thread.start()
            try:
                _run_example(examples, port)
                _run_auto_tail_example(examples, port, runtime, now_ms)
            finally:
                server.shutdown()
                thread.join()


def _run_example(examples: Path, port: int) -> None:
    def call(method: str, path: str, expected_status: int = 200, **fields) -> dict:
        if method == 'GET':
            path += '?' + urlencode(fields)
        status, body = request_json('127.0.0.1', port, method, path, fields if method == 'POST' else None)
        if status != expected_status:
            raise RuntimeError(f'{method} {path}: {status} {body}')
        return body

    original = json.loads((examples / 'original.json').read_text(encoding='utf-8'))
    amendment = json.loads((examples / 'amendment.json').read_text(encoding='utf-8'))
    old = call('POST', '/publish', 201, **original)
    new = call('POST', '/publish', 201, **amendment)
    binding = {'show': 'S7', 'screen': 'P1'}
    call('POST', '/open', **binding, revision=old['revision'])
    candidate = call('GET', '/preview', **binding, cue='C17', segment=1)
    display_lines = old['cues'][0]['segments'][0]['lines']
    first = call('POST', '/advance', **binding, cue='C17', segment=1)
    if display_lines != ['稍候', '开灯'] or candidate['segments'][0]['lines'] != display_lines:
        raise RuntimeError('six-column layout differs from the printed script')
    if first['lines'] != ['稍候', '开灯']:
        raise RuntimeError('first display did not consume the finalized layout')
    call('POST', '/correct', **binding, revision=new['revision'], request='Q1')
    call('POST', '/advance', **binding, cue='C17', segment=2)
    call('POST', '/advance', **binding, cue='C18', segment=1)
    call('POST', '/close', show='S7')
    events = call('GET', '/history', **binding)['events']
    expected = [
        {'sequence': 1, 'cue': 'C17', 'segment': 1, 'revision': 'R1'},
        {'sequence': 2, 'cue': 'C17', 'segment': 2, 'revision': 'R1'},
        {'sequence': 3, 'cue': 'C18', 'segment': 1, 'revision': 'R2'},
    ]
    if events != expected:
        raise RuntimeError(f'unexpected displayed history: {events!r}')
    evidence = {
        'case_id': 't1.width', 'input': original, 'expected': ['稍候', '开灯'], 'actual': display_lines,
        'downstream_expected': {'lines': ['稍候', '开灯'], 'history': expected},
        'downstream_actual': {'lines': first['lines'], 'history': events},
        'code_paths': ['editions/layout.py', 'display/playback.py', 'audit/history.py'],
        'oracle': '每个汉字两列，两词各四列，加一列词间空格后超过六列。首段和尾段同稿，下一条才使用勘误稿。',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(evidence, ensure_ascii=False))


def _demo_call(port: int, method: str, path: str, expected_status: int = 200, **fields) -> dict:
    if method == 'GET':
        path += '?' + urlencode(fields)
    status, body = request_json('127.0.0.1', port, method, path,
                                fields if method == 'POST' else None)
    if status != expected_status:
        raise RuntimeError(f'{method} {path}: expected {expected_status}, got {status} {body}')
    return body


def _run_auto_tail_example(examples: Path, port: int, runtime: Runtime, now_ms: list[int]) -> None:
    original = json.loads((examples / 'original.json').read_text(encoding='utf-8'))
    revision = _demo_call(port, 'POST', '/publish', 201, **original)['revision']

    auto_binding = {'show': 'SA', 'screen': 'P1'}
    _demo_call(port, 'POST', '/open', **auto_binding, revision=revision,
               auto_tail={'cue': 'C17', 'delay_ms': 5_000})
    _demo_call(port, 'POST', '/advance', **auto_binding, cue='C17', segment=1)
    now_ms[0] = 14_999
    runtime.poll_auto_tail()
    before_due = _demo_call(port, 'GET', '/auto_tail', **auto_binding, cue='C17')
    if before_due != {'show': 'SA', 'screen': 'P1', 'cue': 'C17', 'status': 'armed',
                      'due_ms': 15_000, 'sequence': None}:
        raise RuntimeError(f'unexpected pre-due status: {before_due!r}')

    now_ms[0] = 15_000
    runtime.poll_auto_tail()
    auto_status = _demo_call(port, 'GET', '/auto_tail', **auto_binding, cue='C17')
    auto_history = _demo_call(port, 'GET', '/history', **auto_binding)['events']
    expected_auto_history = [
        {'sequence': 1, 'cue': 'C17', 'segment': 1, 'revision': revision},
        {'sequence': 2, 'cue': 'C17', 'segment': 2, 'revision': revision},
    ]
    if auto_status['status'] != 'auto' or auto_status['sequence'] != 2:
        raise RuntimeError(f'auto tail did not light once: {auto_status!r}')
    if auto_history != expected_auto_history:
        raise RuntimeError(f'unexpected auto history: {auto_history!r}')

    cancel_binding = {'show': 'SC', 'screen': 'P1'}
    _demo_call(port, 'POST', '/open', **cancel_binding, revision=revision,
               auto_tail={'cue': 'C17', 'delay_ms': 5_000})
    _demo_call(port, 'POST', '/advance', **cancel_binding, cue='C17', segment=1)
    cancelled = _demo_call(port, 'POST', '/auto_tail/cancel', **cancel_binding,
                           cue='C17', request='CANCEL-1')
    if cancelled['status'] != 'cancelled':
        raise RuntimeError(f'cancellation did not win: {cancelled!r}')
    manual = _demo_call(port, 'POST', '/advance', **cancel_binding, cue='C17', segment=2)
    replayed = _demo_call(port, 'POST', '/auto_tail/cancel', **cancel_binding,
                          cue='C17', request='CANCEL-1')
    final_status = _demo_call(port, 'GET', '/auto_tail', **cancel_binding, cue='C17')
    if manual['sequence'] != 2 or replayed != cancelled:
        raise RuntimeError('original cancellation outcome was not preserved')
    if final_status['status'] != 'manual' or final_status['sequence'] != 2:
        raise RuntimeError(f'manual tail status mismatch: {final_status!r}')

    evidence = {
        'case_id': 'auto_tail.joint',
        'input': {'delay_ms': 5_000, 'first_lit_ms': 10_000,
                  'polls': [14_999, 15_000], 'cancel_request': 'CANCEL-1'},
        'expected': {'before_due': 'armed@15000', 'auto': 'auto#2',
                     'cancel_original': 'cancelled', 'later_manual': 'manual#2'},
        'actual': {'before_due': before_due, 'auto': auto_status,
                   'cancel_original': cancelled, 'cancel_replay': replayed,
                   'later_manual': final_status},
        'downstream_expected': {'auto_history': expected_auto_history},
        'downstream_actual': {'auto_history': auto_history},
        'code_paths': ['control/auto_tail.py', 'display/playback.py',
                       'display/auto_control.py', 'control/runtime.py'],
        'oracle': '到期前不补段，15000 毫秒轮询用首亮稿补亮一次；取消原结局永久保留，之后人工尾段仍可实显。',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(evidence, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='本地剧场字幕领播服务')
    commands = parser.add_subparsers(dest='command', required=True)
    server = commands.add_parser('serve')
    server.add_argument('--data-dir', type=Path, default=Path('data'))
    server.add_argument('--screens', type=Path)
    server.add_argument('--host', default='127.0.0.1')
    server.add_argument('--port', type=int, default=8765)
    commands.add_parser('demo')
    args = parser.parse_args(argv)
    if args.command == 'demo':
        _demo()
        return 0
    try:
        runtime = build_runtime(args.data_dir, load_screen_manifest(args.screens))
        serve(runtime, args.host, args.port)
    except BusinessError as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
