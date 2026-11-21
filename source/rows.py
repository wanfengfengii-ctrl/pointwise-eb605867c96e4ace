import json
from dataclasses import asdict, dataclass

from source.normalize import NormalizedReading
from storage import canonical, day_id, identifier, read_scope


@dataclass(frozen=True)
class ObservationKey:
    day: str
    station: str
    reading_id: str

    def __post_init__(self):
        day_id(self.day)
        identifier(self.station, 'station')
        identifier(self.reading_id, 'reading id')

    def columns(self):
        return (self.day, self.station, self.reading_id)

    def to_data(self):
        return asdict(self)


@dataclass(frozen=True)
class ObservationImage:
    key: ObservationKey
    ordinal: int
    revision: str
    sequence: int
    reading: NormalizedReading

    def to_data(self):
        return asdict(self)

    @classmethod
    def from_data(cls, data):
        return cls(
            ObservationKey(**data['key']), data['ordinal'], data['revision'],
            data['sequence'], NormalizedReading(**data['reading']),
        )


@dataclass(frozen=True)
class RowHead:
    ordinal: int
    revision: str
    image: ObservationImage | None


def ensure_row_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS observation_heads (
        day TEXT NOT NULL, station TEXT NOT NULL, reading_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL, revision TEXT NOT NULL, image TEXT,
        PRIMARY KEY(day, station, reading_id), UNIQUE(day, ordinal)
    )''')


def read_row_head(db, key):
    row = db.execute('''SELECT ordinal, revision, image FROM observation_heads
        WHERE day = ? AND station = ? AND reading_id = ?''', key.columns()).fetchone()
    if row is None:
        return None
    ordinal, revision, image = row
    return RowHead(ordinal, revision, None if image is None else ObservationImage.from_data(json.loads(image)))


def save_row_head(db, key, head):
    image = None if head.image is None else canonical(head.image.to_data())
    db.execute('''INSERT INTO observation_heads VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(day, station, reading_id) DO UPDATE SET
        revision = excluded.revision, image = excluded.image''',
        (*key.columns(), head.ordinal, head.revision, image))


def read_source_slice(db, day):
    day_id(day)
    with read_scope(db):
        frontier = db.execute('SELECT latest FROM source_days WHERE day = ?', (day,)).fetchone()
        images = db.execute('''SELECT image FROM observation_heads
            WHERE day = ? AND image IS NOT NULL ORDER BY ordinal, station, reading_id''', (day,))
        return (0 if frontier is None else frontier[0],
                tuple(ObservationImage.from_data(json.loads(image)) for image, in images))
