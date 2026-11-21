from dataclasses import dataclass

from source.feed import ChangeBatch, append_change, source_frontiers
from source.normalize import normalize_reading, submission_fingerprint
from source.revisions import RevisionConflict, plan_revision
from source.rows import ObservationKey, read_row_head, save_row_head
from storage import identifier, index, transaction


_UNSET = object()


@dataclass(frozen=True)
class SubmissionReceipt:
    batch_id: str
    day: str
    sequence: int

    @property
    def revision(self):
        return self.batch_id


def ensure_submission_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS submissions (
        batch_id TEXT PRIMARY KEY, day TEXT NOT NULL, sequence INTEGER NOT NULL,
        fingerprint TEXT NOT NULL, UNIQUE(day, sequence)
    )''')


def _find_submission(db, batch_id):
    return db.execute('SELECT day, sequence, fingerprint FROM submissions WHERE batch_id = ?', (batch_id,)).fetchone()


def lookup_submission(db, batch_id):
    identifier(batch_id, 'batch id')
    row = _find_submission(db, batch_id)
    return None if row is None else SubmissionReceipt(batch_id, row[0], row[1])


def submit(db, batch_id, day, station, reading_id, *, operation='add', ml=_UNSET, expected_revision=None):
    identifier(batch_id, 'batch id')
    key = ObservationKey(day, station, reading_id)
    if operation not in ('add', 'correct', 'withdraw'):
        raise ValueError('unknown submission operation')
    if operation == 'add':
        if expected_revision is not None:
            raise ValueError('add requires no expected revision')
    else:
        identifier(expected_revision, 'expected revision')
    if operation == 'withdraw':
        if ml is not _UNSET:
            raise ValueError('withdraw takes no reading')
        reading = None
    else:
        if ml is _UNSET:
            raise ValueError('supply millilitres or explicit null for a missing reading')
        reading = normalize_reading(ml)
    fingerprint = submission_fingerprint(key, operation, expected_revision, reading)

    with transaction(db, write=True):
        previous = _find_submission(db, batch_id)
        if previous is not None:
            old_day, sequence, old_fingerprint = previous
            if old_fingerprint != fingerprint:
                raise RevisionConflict('batch id was used with different parameters')
            return SubmissionReceipt(batch_id, old_day, sequence)

        latest, _ = source_frontiers(db, day)
        sequence = index(latest + 1, 'next source sequence', minimum=1)
        before, head = plan_revision(key, read_row_head(db, key), operation,
                                     expected_revision, reading, batch_id, sequence)
        save_row_head(db, key, head)
        append_change(db, ChangeBatch(day, sequence, batch_id, before, head.image))
        db.execute('INSERT INTO submissions VALUES (?, ?, ?, ?)', (batch_id, day, sequence, fingerprint))
        return SubmissionReceipt(batch_id, day, sequence)
