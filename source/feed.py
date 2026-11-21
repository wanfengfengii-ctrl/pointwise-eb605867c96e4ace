import json
from dataclasses import dataclass

from source.rows import ObservationImage
from storage import canonical, day_id, digest, index, transaction


@dataclass(frozen=True)
class ChangeBatch:
    day: str
    sequence: int
    batch_id: str
    before: ObservationImage | None
    after: ObservationImage | None

    def to_data(self):
        return {
            'day': self.day, 'sequence': self.sequence, 'batch_id': self.batch_id,
            'before': None if self.before is None else self.before.to_data(),
            'after': None if self.after is None else self.after.to_data(),
        }

    @classmethod
    def from_payload(cls, payload):
        data = json.loads(payload)
        for field in ('before', 'after'):
            if data[field] is not None:
                data[field] = ObservationImage.from_data(data[field])
        return cls(**data)

    @property
    def fingerprint(self):
        return digest(self.to_data())


def ensure_feed_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS source_days (
        day TEXT PRIMARY KEY, latest INTEGER NOT NULL, confirmed INTEGER NOT NULL,
        CHECK(0 <= confirmed AND confirmed <= latest)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS source_changes (
        day TEXT NOT NULL REFERENCES source_days(day), sequence INTEGER NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(day, sequence)
    )''')


def source_frontiers(db, day):
    day_id(day)
    frontier = db.execute('SELECT latest, confirmed FROM source_days WHERE day = ?', (day,)).fetchone()
    return (0, 0) if frontier is None else frontier


def append_change(db, batch):
    latest, _ = source_frontiers(db, batch.day)
    if batch.sequence != latest + 1:
        raise ValueError('source append must be contiguous')
    db.execute('''INSERT INTO source_days VALUES (?, ?, 0)
        ON CONFLICT(day) DO UPDATE SET latest = excluded.latest''', (batch.day, batch.sequence))
    db.execute('INSERT INTO source_changes VALUES (?, ?, ?)',
               (batch.day, batch.sequence, canonical(batch.to_data())))


def read_changes(db, day, *, after=0, through=None):
    day_id(day)
    index(after, 'after sequence')
    if through is None:
        through = 2**63 - 1
    index(through, 'through sequence')
    rows = db.execute('''SELECT payload FROM source_changes
        WHERE day = ? AND sequence > ? AND sequence <= ? ORDER BY sequence''', (day, after, through))
    return tuple(ChangeBatch.from_payload(payload) for payload, in rows)


def confirm_through(db, day, through):
    day_id(day)
    index(through, 'confirmation sequence')
    with transaction(db, write=True):
        latest, confirmed = source_frontiers(db, day)
        if through > latest:
            raise ValueError('confirmation exceeds the source sequence')
        if through > confirmed:
            db.execute('UPDATE source_days SET confirmed = ? WHERE day = ?', (through, day))
        return max(confirmed, through)


def reclaim_confirmed(db, day):
    day_id(day)
    with transaction(db, write=True):
        _, confirmed = source_frontiers(db, day)
        removed = db.execute('DELETE FROM source_changes WHERE day = ? AND sequence <= ?', (day, confirmed))
        return removed.rowcount
