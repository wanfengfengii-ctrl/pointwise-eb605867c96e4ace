import json
from dataclasses import asdict, dataclass

from daily.contribution import Contribution, ViewRow
from daily.snapshot import PublishedSnapshot
from storage import canonical, read_scope


@dataclass(frozen=True)
class StatementRecord:
    number: str
    snapshot: PublishedSnapshot


def ensure_statement_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS statements (
        number TEXT PRIMARY KEY, day TEXT NOT NULL, rule_id TEXT NOT NULL,
        sequence INTEGER NOT NULL, totals TEXT NOT NULL
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS statement_provenance (
        number TEXT NOT NULL REFERENCES statements(number), region TEXT NOT NULL,
        ordinal INTEGER NOT NULL, station TEXT NOT NULL, reading_id TEXT NOT NULL,
        view_row TEXT NOT NULL, PRIMARY KEY(number, region, ordinal, station, reading_id)
    )''')


def store_statement(db, record):
    snapshot = record.snapshot
    db.execute('INSERT INTO statements VALUES (?, ?, ?, ?, ?)',
               (record.number, snapshot.day, snapshot.rule_id, snapshot.sequence,
                canonical([asdict(total) for total in snapshot.totals])))
    db.executemany('INSERT INTO statement_provenance VALUES (?, ?, ?, ?, ?, ?)', (
        (record.number, row.region, row.image.ordinal, row.image.key.station,
         row.image.key.reading_id, canonical(row.to_data())) for row in snapshot.rows
    ))


def load_provenance_rows(db, number, *, region=None, station=None):
    conditions = ['number = ?']
    params = [number]
    if region is not None:
        conditions.append('region = ?')
        params.append(region)
    if station is not None:
        conditions.append('station = ?')
        params.append(station)
    where = ' AND '.join(conditions)
    rows = db.execute(f'''SELECT view_row FROM statement_provenance WHERE {where}
        ORDER BY region, ordinal, station, reading_id''', params)
    return tuple(ViewRow.from_data(json.loads(payload)) for payload, in rows)


def load_statement(db, number):
    with read_scope(db):
        row = db.execute('SELECT day, rule_id, sequence, totals FROM statements WHERE number = ?',
                         (number,)).fetchone()
        if row is None:
            return None
        day, rule_id, sequence, totals = row
        areas = tuple(Contribution(**item) for item in json.loads(totals))
        return StatementRecord(number, PublishedSnapshot(day, rule_id, sequence, areas,
                                                         load_provenance_rows(db, number)))
