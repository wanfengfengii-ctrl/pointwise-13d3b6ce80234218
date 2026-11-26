import json

from display.outcomes import LitEvent
from storage import Database


class DisplayLedger:
    def __init__(self, database: Database):
        self.database = database
        database.initialize('''
            CREATE TABLE IF NOT EXISTS ledger (
                show TEXT NOT NULL,
                screen TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                cue TEXT NOT NULL,
                segment INTEGER NOT NULL,
                revision TEXT NOT NULL,
                lines TEXT NOT NULL,
                lit_ms INTEGER,
                origin TEXT NOT NULL DEFAULT 'manual',
                PRIMARY KEY (show, screen, sequence)
            );
        ''')
        with database.transaction(write=True) as connection:
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(ledger)')}
            if 'lit_ms' not in columns:
                connection.execute('ALTER TABLE ledger ADD COLUMN lit_ms INTEGER')
            if 'origin' not in columns:
                connection.execute(
                    "ALTER TABLE ledger ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'"
                )

    def accept(self, event: LitEvent) -> bool:
        with self.database.transaction(write=True) as connection:
            cursor = connection.execute('''
                INSERT INTO ledger
                    (show, screen, sequence, cue, segment, revision, lines, lit_ms, origin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(show, screen, sequence) DO NOTHING
            ''', (event.show, event.screen, event.sequence, event.cue, event.segment,
                  event.revision, json.dumps(event.lines, ensure_ascii=False),
                  event.lit_ms, event.origin))
            return cursor.rowcount == 1

    def list_for_screen(self, show: str, screen: str) -> list[LitEvent]:
        with self.database.transaction() as connection:
            rows = connection.execute('''
                SELECT * FROM ledger WHERE show = ? AND screen = ? ORDER BY rowid
            ''', (show, screen)).fetchall()
        return [LitEvent.from_row(row) for row in rows]
