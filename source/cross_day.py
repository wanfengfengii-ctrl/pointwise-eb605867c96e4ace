"""Cross-day correction: move one active reading from a source day to another day.

The source database owns the authoritative pair of change batches.  Both batches
are appended in one transaction, each taking that day's actual next sequence.
The correction record keeps both coordinates, both batch identities, the
original revision/ordinal and the original reading, so a reopened connection can
still bind the pair after the confirmed payloads have been reclaimed.
"""

from dataclasses import asdict, dataclass

from source.feed import ChangeBatch, append_change, read_changes, source_frontiers
from source.normalize import normalize_reading
from source.revisions import RevisionConflict, plan_revision
from source.rows import ObservationImage, ObservationKey, RowHead, read_row_head, save_row_head
from storage import day_id, digest, identifier, index, transaction


@dataclass(frozen=True)
class CrossDayReceipt:
    """Coordinates and preserved original binding of one accepted correction."""

    correction_id: str
    source_day: str
    source_sequence: int
    target_day: str
    target_sequence: int
    source_batch_id: str
    target_batch_id: str
    station: str
    reading_id: str
    source_revision: str
    source_ordinal: int
    reading_ml: int | None
    source_fingerprint: str
    target_fingerprint: str

    @property
    def source_reading(self):
        return normalize_reading(self.reading_ml)

    def to_data(self):
        return asdict(self)


# Columns selected by _select_row, in the order used to rebuild a receipt.
_SELECT = '''SELECT correction_id, fingerprint, source_day, source_sequence,
    target_day, target_sequence, source_batch_id, target_batch_id, station, reading_id,
    source_revision, source_ordinal, reading_ml, delivered,
    source_fingerprint, target_fingerprint FROM cross_day_corrections'''

_RECEIPT_INDEXES = (0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15)


def ensure_cross_day_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS cross_day_corrections (
        correction_id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        source_day TEXT NOT NULL,
        source_sequence INTEGER NOT NULL,
        target_day TEXT NOT NULL,
        target_sequence INTEGER NOT NULL,
        source_batch_id TEXT NOT NULL UNIQUE,
        target_batch_id TEXT NOT NULL UNIQUE,
        station TEXT NOT NULL,
        reading_id TEXT NOT NULL,
        source_revision TEXT NOT NULL,
        source_ordinal INTEGER NOT NULL,
        reading_ml INTEGER,
        delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0, 1)),
        source_fingerprint TEXT NOT NULL,
        target_fingerprint TEXT NOT NULL,
        UNIQUE(source_day, source_sequence),
        UNIQUE(target_day, target_sequence)
    )''')


def cross_day_fingerprint(correction_id, source_day, target_day, station, reading_id, expected_revision):
    identifier(correction_id, 'correction id')
    day_id(source_day)
    day_id(target_day)
    identifier(station, 'station')
    identifier(reading_id, 'reading id')
    identifier(expected_revision, 'expected revision')
    return digest({
        'correction_id': correction_id,
        'source_day': source_day,
        'target_day': target_day,
        'station': station,
        'reading_id': reading_id,
        'expected_revision': expected_revision,
    })


def batch_ids(correction_id):
    identifier(correction_id, 'correction id')
    return f'{correction_id}~source', f'{correction_id}~target'


def _select_row(db, correction_id):
    return db.execute(f'{_SELECT} WHERE correction_id = ?', (correction_id,)).fetchone()


def _row_receipt(row):
    return CrossDayReceipt(*(row[position] for position in _RECEIPT_INDEXES))


def _has_correction_table(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                      "AND name = 'cross_day_corrections'").fetchone() is not None


def _select_coordinate(db, day, sequence):
    if not _has_correction_table(db):
        return None
    return db.execute(
        f'{_SELECT} WHERE (source_day = ? AND source_sequence = ?) '
        'OR (target_day = ? AND target_sequence = ?)',
        (day, sequence, day, sequence)).fetchone()


def _submission_exists(db, batch_id):
    return db.execute('SELECT 1 FROM submissions WHERE batch_id = ?', (batch_id,)).fetchone() is not None


def lookup_cross_day(db, correction_id):
    identifier(correction_id, 'correction id')
    if not _has_correction_table(db):
        return None
    row = _select_row(db, correction_id)
    return None if row is None else _row_receipt(row)


def lookup_cross_day_coordinate(db, day, sequence):
    """Return the correction occupying ``(day, sequence)`` on either side, if any."""

    day_id(day)
    index(sequence, 'cross-day coordinate', minimum=1)
    row = _select_coordinate(db, day, sequence)
    return None if row is None else _row_receipt(row)


def submit_cross_day(db, correction_id, source_day, target_day, station, reading_id, *, expected_revision):
    identifier(correction_id, 'correction id')
    day_id(source_day)
    day_id(target_day)
    identifier(station, 'station')
    identifier(reading_id, 'reading id')
    identifier(expected_revision, 'expected revision')
    if source_day == target_day:
        raise ValueError('cross-day correction requires two different days')

    source_key = ObservationKey(source_day, station, reading_id)
    target_key = ObservationKey(target_day, station, reading_id)
    fingerprint = cross_day_fingerprint(correction_id, source_day, target_day,
                                        station, reading_id, expected_revision)
    source_batch_id, target_batch_id = batch_ids(correction_id)

    with transaction(db, write=True):
        previous = _select_row(db, correction_id)
        if previous is not None:
            if previous[1] != fingerprint:
                raise RevisionConflict('correction id was used with different parameters')
            return _row_receipt(previous)

        if _submission_exists(db, source_batch_id) or _submission_exists(db, target_batch_id):
            raise RevisionConflict('cross-day batch identity is already assigned')

        head = read_row_head(db, source_key)
        if head is None or head.image is None or head.revision != expected_revision:
            raise RevisionConflict('expected revision is not the active revision')
        if read_row_head(db, target_key) is not None:
            raise RevisionConflict('target row identity is already assigned')

        source_latest, _ = source_frontiers(db, source_day)
        source_sequence = index(source_latest + 1, 'next source sequence', minimum=1)
        target_latest, _ = source_frontiers(db, target_day)
        target_sequence = index(target_latest + 1, 'next target sequence', minimum=1)

        original = head.image
        before, withdrawn = plan_revision(source_key, head, 'withdraw', expected_revision,
                                          None, source_batch_id, source_sequence)
        save_row_head(db, source_key, withdrawn)
        source_batch = ChangeBatch(source_day, source_sequence, source_batch_id, before, None)
        append_change(db, source_batch)

        # Copy the reading and its reading-id provenance; the target day owns
        # the actual next ordinal and sequence, but the original revision and
        # reading id stay attached so provenance survives the move.
        target_image = ObservationImage(target_key, target_sequence, original.revision,
                                        target_sequence, original.reading)
        save_row_head(db, target_key, RowHead(target_sequence, original.revision, target_image))
        target_batch = ChangeBatch(target_day, target_sequence, target_batch_id, None, target_image)
        append_change(db, target_batch)

        receipt = CrossDayReceipt(
            correction_id, source_day, source_sequence, target_day, target_sequence,
            source_batch_id, target_batch_id, station, reading_id,
            original.revision, original.ordinal, original.reading.value_ml,
            source_batch.fingerprint, target_batch.fingerprint,
        )
        db.execute('''INSERT INTO cross_day_corrections
            (correction_id, fingerprint, source_day, source_sequence, target_day, target_sequence,
             source_batch_id, target_batch_id, station, reading_id, source_revision,
             source_ordinal, reading_ml, delivered, source_fingerprint, target_fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)''', (
            receipt.correction_id, fingerprint, receipt.source_day, receipt.source_sequence,
            receipt.target_day, receipt.target_sequence, receipt.source_batch_id,
            receipt.target_batch_id, receipt.station, receipt.reading_id,
            receipt.source_revision, receipt.source_ordinal, receipt.reading_ml,
            receipt.source_fingerprint, receipt.target_fingerprint,
        ))
        db.executemany('INSERT INTO submissions VALUES (?, ?, ?, ?)', (
            (source_batch_id, source_day, source_sequence, fingerprint),
            (target_batch_id, target_day, target_sequence, fingerprint),
        ))
        return receipt


def acknowledge_cross_day(db, correction_id):
    """Warehouse committed both sides: mark delivered and confirm both days."""

    identifier(correction_id, 'correction id')
    with transaction(db, write=True):
        row = _select_row(db, correction_id)
        if row is None:
            raise KeyError(f'unknown cross-day correction {correction_id}')
        receipt = _row_receipt(row)
        for day, sequence in ((receipt.source_day, receipt.source_sequence),
                              (receipt.target_day, receipt.target_sequence)):
            latest, confirmed = source_frontiers(db, day)
            if sequence > latest:
                raise ValueError('confirmation exceeds the source sequence')
            if sequence > confirmed:
                db.execute('UPDATE source_days SET confirmed = ? WHERE day = ?', (sequence, day))
        db.execute('UPDATE cross_day_corrections SET delivered = 1 WHERE correction_id = ?',
                   (correction_id,))
        return receipt


def protected_sequences(db, day):
    """Sequences on ``day`` that belong to a pair whose originals must stay."""

    if not _has_correction_table(db):
        return frozenset()
    protected = set()
    rows = db.execute('''SELECT correction_id, source_day, source_sequence,
        target_day, target_sequence, delivered FROM cross_day_corrections
        WHERE source_day = ? OR target_day = ?''', (day, day))
    for correction_id, source_day, source_sequence, target_day, target_sequence, delivered in rows:
        source_confirmed = db.execute('SELECT confirmed FROM source_days WHERE day = ?',
                                      (source_day,)).fetchone()
        target_confirmed = db.execute('SELECT confirmed FROM source_days WHERE day = ?',
                                      (target_day,)).fetchone()
        source_confirmed = 0 if source_confirmed is None else source_confirmed[0]
        target_confirmed = 0 if target_confirmed is None else target_confirmed[0]
        released = (bool(delivered) and source_confirmed >= source_sequence
                    and target_confirmed >= target_sequence)
        if released:
            continue
        if source_day == day:
            protected.add(source_sequence)
        if target_day == day:
            protected.add(target_sequence)
    return frozenset(protected)


def read_pair(db, receipt):
    """Read both expected change batches, checking they are the same originals."""

    source_batch = read_changes(db, receipt.source_day,
                                after=receipt.source_sequence - 1, through=receipt.source_sequence)
    target_batch = read_changes(db, receipt.target_day,
                                after=receipt.target_sequence - 1, through=receipt.target_sequence)
    if (len(source_batch) != 1 or len(target_batch) != 1
            or source_batch[0].fingerprint != receipt.source_fingerprint
            or target_batch[0].fingerprint != receipt.target_fingerprint):
        raise RevisionConflict('cross-day original material is unavailable or differs')
    return source_batch[0], target_batch[0]
