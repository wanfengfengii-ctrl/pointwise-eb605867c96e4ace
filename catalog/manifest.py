import json
from dataclasses import dataclass

from source.feed import ChangeBatch
from source.rows import ObservationImage
from storage import canonical, index


@dataclass(frozen=True)
class BaselineManifest:
    baseline_id: str
    day: str
    cut: int
    row_count: int
    digest: str


def ensure_manifest_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS baselines (
        baseline_id TEXT PRIMARY KEY, day TEXT NOT NULL, cut INTEGER NOT NULL,
        row_count INTEGER NOT NULL, digest TEXT NOT NULL, UNIQUE(day, cut)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS baseline_rows (
        baseline_id TEXT NOT NULL REFERENCES baselines(baseline_id), ordinal INTEGER NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(baseline_id, ordinal)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS prepared_rules (
        baseline_id TEXT NOT NULL REFERENCES baselines(baseline_id),
        rule_id TEXT NOT NULL REFERENCES rule_versions(rule_id), PRIMARY KEY(baseline_id, rule_id)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS retained_changes (
        day TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(day, sequence)
    )''')


def store_baseline(db, manifest, rows):
    db.execute('INSERT INTO baselines VALUES (?, ?, ?, ?, ?)',
               (manifest.baseline_id, manifest.day, manifest.cut, manifest.row_count, manifest.digest))
    db.executemany('INSERT INTO baseline_rows VALUES (?, ?, ?)', (
        (manifest.baseline_id, image.ordinal, canonical(image.to_data())) for image in rows
    ))


def load_manifest(db, baseline_id):
    row = db.execute('SELECT * FROM baselines WHERE baseline_id = ?', (baseline_id,)).fetchone()
    if row is None:
        raise KeyError(f'unknown baseline {baseline_id}')
    return BaselineManifest(*row)


def find_baseline(db, day, cut):
    row = db.execute('SELECT * FROM baselines WHERE day = ? AND cut = ?', (day, cut)).fetchone()
    return None if row is None else BaselineManifest(*row)


def load_baseline_page(db, baseline_id, *, after=0, limit=100):
    index(after, 'baseline cursor')
    index(limit, 'baseline page size', minimum=1)
    rows = db.execute('''SELECT payload FROM baseline_rows WHERE baseline_id = ? AND ordinal > ?
        ORDER BY ordinal LIMIT ?''', (baseline_id, after, limit))
    return tuple(ObservationImage.from_data(json.loads(payload)) for payload, in rows)


def bind_prepared_rule(db, baseline_id, rule_id):
    db.execute('INSERT INTO prepared_rules VALUES (?, ?) ON CONFLICT DO NOTHING', (baseline_id, rule_id))


def has_prepared_rule(db, baseline_id, rule_id):
    return db.execute('SELECT 1 FROM prepared_rules WHERE baseline_id = ? AND rule_id = ?',
                      (baseline_id, rule_id)).fetchone() is not None


def has_earlier_baseline(db, day, sequence):
    return db.execute('SELECT 1 FROM baselines WHERE day = ? AND cut < ? LIMIT 1',
                      (day, sequence)).fetchone() is not None


def retain_change(db, batch):
    payload = canonical(batch.to_data())
    db.execute('INSERT INTO retained_changes VALUES (?, ?, ?) ON CONFLICT DO NOTHING',
               (batch.day, batch.sequence, payload))
    row = db.execute('SELECT payload FROM retained_changes WHERE day = ? AND sequence = ?',
                     (batch.day, batch.sequence)).fetchone()
    if row[0] != payload:
        raise ValueError('conflicting retained change')


def load_retained_changes(db, day, *, after, through):
    rows = db.execute('''SELECT payload FROM retained_changes WHERE day = ? AND sequence > ? AND sequence <= ?
        ORDER BY sequence''', (day, after, through))
    return tuple(ChangeBatch.from_payload(payload) for payload, in rows)
