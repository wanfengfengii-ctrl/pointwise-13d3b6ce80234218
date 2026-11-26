import json
from dataclasses import asdict

from editions.revisions import FrozenEdition, restore_edition
from errors import conflict, missing
from storage import Database


class ScreenStateStore:
    def __init__(self, database: Database):
        self.database = database
        database.initialize('''
            CREATE TABLE IF NOT EXISTS screen_editions (
                revision TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                show TEXT NOT NULL,
                screen TEXT NOT NULL,
                initial_revision TEXT NOT NULL REFERENCES screen_editions(revision),
                active_revision TEXT NOT NULL REFERENCES screen_editions(revision),
                epoch INTEGER NOT NULL DEFAULT 0,
                closed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (show, screen)
            );
            CREATE TABLE IF NOT EXISTS lit_events (
                show TEXT NOT NULL,
                screen TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                cue TEXT NOT NULL,
                segment INTEGER NOT NULL,
                revision TEXT NOT NULL REFERENCES screen_editions(revision),
                lines TEXT NOT NULL,
                lit_ms INTEGER,
                origin TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'auto')),
                PRIMARY KEY (show, screen, sequence),
                UNIQUE (show, screen, cue, segment),
                FOREIGN KEY (show, screen) REFERENCES sessions(show, screen)
            );
            CREATE TABLE IF NOT EXISTS cutovers (
                show TEXT NOT NULL,
                screen TEXT NOT NULL,
                request TEXT NOT NULL,
                revision TEXT NOT NULL REFERENCES screen_editions(revision),
                epoch INTEGER NOT NULL,
                applied INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (show, screen, request),
                UNIQUE (show, screen, epoch),
                FOREIGN KEY (show, screen) REFERENCES sessions(show, screen)
            );
            CREATE TABLE IF NOT EXISTS auto_cancellations (
                show TEXT NOT NULL,
                screen TEXT NOT NULL,
                cue TEXT NOT NULL,
                request TEXT NOT NULL,
                cancelled_ms INTEGER,
                PRIMARY KEY (show, screen, request),
                FOREIGN KEY (show, screen) REFERENCES sessions(show, screen)
            );
        ''')
        self._migrate()

    def _migrate(self) -> None:
        with self.database.transaction(write=True) as connection:
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(lit_events)')}
            if 'lit_ms' not in columns:
                connection.execute('ALTER TABLE lit_events ADD COLUMN lit_ms INTEGER')
            if 'origin' not in columns:
                connection.execute(
                    "ALTER TABLE lit_events ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'"
                )

    def transaction(self, *, write: bool = True):
        return self.database.transaction(write=write)

    @staticmethod
    def _session_row(connection, show: str, screen: str):
        return connection.execute(
            'SELECT * FROM sessions WHERE show = ? AND screen = ?', (show, screen)
        ).fetchone()

    def session(self, connection, show: str, screen: str):
        row = self._session_row(connection, show, screen)
        if row is None:
            raise missing(f'no playback session for {show} on {screen}')
        return row

    def ensure_session(self, show: str, screen: str, initial: FrozenEdition) -> bool:
        with self.transaction() as connection:
            row = self._session_row(connection, show, screen)
            if row is not None:
                if row['initial_revision'] != initial.revision:
                    raise conflict('screen session already has another initial revision')
                return bool(row['closed'])
            self.remember_edition(connection, initial)
            connection.execute('''
                INSERT INTO sessions (show, screen, initial_revision, active_revision)
                VALUES (?, ?, ?, ?)
            ''', (show, screen, initial.revision, initial.revision))
            return False

    @staticmethod
    def remember_edition(connection, edition: FrozenEdition) -> None:
        payload = json.dumps(asdict(edition), ensure_ascii=False)
        existing = connection.execute(
            'SELECT payload FROM screen_editions WHERE revision = ?', (edition.revision,)
        ).fetchone()
        if existing is None:
            connection.execute('INSERT INTO screen_editions VALUES (?, ?)', (edition.revision, payload))
        elif restore_edition(json.loads(existing['payload'])) != edition:
            raise conflict('screen revision identifier already has different content')

    @staticmethod
    def edition(connection, revision: str) -> FrozenEdition:
        row = connection.execute('SELECT payload FROM screen_editions WHERE revision = ?', (revision,)).fetchone()
        if row is None:
            raise missing(f'revision {revision} has not been loaded on the screen')
        return restore_edition(json.loads(row['payload']))

    def close_session(self, show: str, screen: str) -> None:
        with self.transaction() as connection:
            self.session(connection, show, screen)
            connection.execute('UPDATE sessions SET closed = 1 WHERE show = ? AND screen = ?', (show, screen))

    @staticmethod
    def segment_row(connection, show: str, screen: str, cue: str, segment: int):
        return connection.execute('''
            SELECT * FROM lit_events WHERE show = ? AND screen = ? AND cue = ? AND segment = ?
        ''', (show, screen, cue, segment)).fetchone()

    @staticmethod
    def last_event(connection, show: str, screen: str):
        return connection.execute('''
            SELECT * FROM lit_events WHERE show = ? AND screen = ? ORDER BY sequence DESC LIMIT 1
        ''', (show, screen)).fetchone()

    @staticmethod
    def insert_event(connection, show: str, screen: str, sequence: int, cue: str,
                     segment: int, revision: str, lines: tuple[str, ...],
                     lit_ms: int | None = None, origin: str = 'manual') -> None:
        connection.execute('''
            INSERT INTO lit_events
                (show, screen, sequence, cue, segment, revision, lines, lit_ms, origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            show, screen, sequence, cue, segment, revision,
            json.dumps(lines, ensure_ascii=False), lit_ms, origin,
        ))

    def read_segment(self, show: str, screen: str, cue: str, segment: int):
        with self.transaction(write=False) as connection:
            return self.segment_row(connection, show, screen, cue, segment)

    def read_events(self, show: str, screen: str):
        with self.transaction(write=False) as connection:
            return connection.execute('''
                SELECT * FROM lit_events WHERE show = ? AND screen = ? ORDER BY sequence
            ''', (show, screen)).fetchall()

    @staticmethod
    def cutover_row(connection, show: str, screen: str, request: str):
        return connection.execute('''
            SELECT * FROM cutovers WHERE show = ? AND screen = ? AND request = ?
        ''', (show, screen, request)).fetchone()

    @staticmethod
    def insert_cutover(connection, show: str, screen: str, request: str,
                       revision: str, epoch: int) -> None:
        connection.execute('''
            INSERT INTO cutovers (show, screen, request, revision, epoch) VALUES (?, ?, ?, ?, ?)
        ''', (show, screen, request, revision, epoch))

    @staticmethod
    def commit_cutover(connection, row) -> None:
        connection.execute('''
            UPDATE sessions SET active_revision = ?, epoch = ? WHERE show = ? AND screen = ?
        ''', (row['revision'], row['epoch'], row['show'], row['screen']))
        connection.execute('''
            UPDATE cutovers SET applied = 1 WHERE show = ? AND screen = ? AND request = ?
        ''', (row['show'], row['screen'], row['request']))

    def read_cutover(self, show: str, screen: str, request: str):
        with self.transaction(write=False) as connection:
            row = self.cutover_row(connection, show, screen, request)
        return row if row is not None and row['applied'] else None

    @staticmethod
    def auto_cancel_row(connection, show: str, screen: str, request: str):
        return connection.execute('''
            SELECT * FROM auto_cancellations
            WHERE show = ? AND screen = ? AND request = ?
        ''', (show, screen, request)).fetchone()

    @staticmethod
    def insert_auto_cancel(connection, show: str, screen: str, cue: str,
                           request: str, cancelled_ms: int | None = None) -> None:
        connection.execute('''
            INSERT INTO auto_cancellations (show, screen, cue, request, cancelled_ms)
            VALUES (?, ?, ?, ?, ?)
        ''', (show, screen, cue, request, cancelled_ms))

    @staticmethod
    def auto_cancel_exists(connection, show: str, screen: str, cue: str) -> bool:
        row = connection.execute('''
            SELECT 1 FROM auto_cancellations
            WHERE show = ? AND screen = ? AND cue = ? LIMIT 1
        ''', (show, screen, cue)).fetchone()
        return row is not None

    def read_auto_cancel(self, show: str, screen: str, request: str):
        with self.transaction(write=False) as connection:
            return self.auto_cancel_row(connection, show, screen, request)

    def read_effective_auto_cancel(self, show: str, screen: str, cue: str):
        with self.transaction(write=False) as connection:
            return connection.execute('''
                SELECT * FROM auto_cancellations
                WHERE show = ? AND screen = ? AND cue = ?
                LIMIT 1
            ''', (show, screen, cue)).fetchone()
