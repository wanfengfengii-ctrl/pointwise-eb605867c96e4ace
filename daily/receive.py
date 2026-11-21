from dataclasses import dataclass

from catalog.manifest import has_earlier_baseline, retain_change
from catalog.rules import load_rule_version
from daily.balance import apply_current_batch
from daily.progress import PublishedHead, check_contiguous, publish_progress, read_initial_rule, read_published_head
from source.cross_day import (
    acknowledge_cross_day, lookup_cross_day_coordinate, read_pair,
)
from source.feed import ChangeBatch, confirm_through, read_changes
from storage import day_id, identifier, index, transaction


class DeliveryConflict(ValueError):
    pass


@dataclass(frozen=True)
class DeliveryReceipt:
    day: str
    sequence: int
    batch_id: str
    fingerprint: str


@dataclass(frozen=True)
class CrossDayDelivery:
    correction_id: str
    status: str
    source_day: str
    source_sequence: int
    target_day: str
    target_sequence: int
    source_batch_id: str
    target_batch_id: str

    @property
    def waiting(self):
        return self.status == 'waiting'

    def to_data(self):
        return {
            'correction_id': self.correction_id, 'status': self.status,
            'source_day': self.source_day, 'source_sequence': self.source_sequence,
            'target_day': self.target_day, 'target_sequence': self.target_sequence,
            'source_batch_id': self.source_batch_id, 'target_batch_id': self.target_batch_id,
        }


def ensure_delivery_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
        day TEXT NOT NULL, sequence INTEGER NOT NULL, batch_id TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL, PRIMARY KEY(day, sequence)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS cross_day_deliveries (
        correction_id TEXT PRIMARY KEY,
        source_day TEXT NOT NULL, source_sequence INTEGER NOT NULL,
        target_day TEXT NOT NULL, target_sequence INTEGER NOT NULL,
        source_batch_id TEXT NOT NULL, target_batch_id TEXT NOT NULL,
        UNIQUE(source_day, source_sequence), UNIQUE(target_day, target_sequence)
    )''')


def lookup_delivery(db, day, sequence):
    day_id(day)
    index(sequence, 'delivery sequence', minimum=1)
    row = db.execute('SELECT day, sequence, batch_id, fingerprint FROM deliveries WHERE day = ? AND sequence = ?',
                     (day, sequence)).fetchone()
    return None if row is None else DeliveryReceipt(*row)


def lookup_cross_day_delivery(db, correction_id):
    identifier(correction_id, 'correction id')
    row = db.execute('''SELECT correction_id, source_day, source_sequence, target_day,
        target_sequence, source_batch_id, target_batch_id
        FROM cross_day_deliveries WHERE correction_id = ?''', (correction_id,)).fetchone()
    if row is None:
        return None
    return CrossDayDelivery(row[0], 'delivered', row[1], row[2], row[3], row[4], row[5], row[6])


def _cross_day_member(source, day, sequence):
    return lookup_cross_day_coordinate(source, day, sequence)


def advance(db, source, batch):
    if not isinstance(batch, ChangeBatch):
        raise ValueError('advance requires a ChangeBatch')
    day_id(batch.day)
    index(batch.sequence, 'delivery sequence', minimum=1)
    identifier(batch.batch_id, 'batch id')
    incoming = DeliveryReceipt(batch.day, batch.sequence, batch.batch_id, batch.fingerprint)
    with transaction(db, write=True):
        if _cross_day_member(source, batch.day, batch.sequence) is not None:
            raise DeliveryConflict('joint correction members require advance_cross_day')
        receipt = lookup_delivery(db, batch.day, batch.sequence)
        if receipt is not None:
            if receipt != incoming:
                raise DeliveryConflict('sequence was already delivered with different content')
        else:
            head = read_published_head(db, batch.day)
            check_contiguous(head, batch.sequence)
            source_batches = read_changes(source, batch.day, after=batch.sequence - 1, through=batch.sequence)
            if source_batches != (batch,) or source_batches[0].fingerprint != incoming.fingerprint:
                raise DeliveryConflict('change differs from the available source batch')

            rule_id = read_initial_rule(db) if head is None else head.rule_id
            apply_current_batch(db, batch, load_rule_version(db, rule_id))
            if has_earlier_baseline(db, batch.day, batch.sequence):
                retain_change(db, batch)
            db.execute('INSERT INTO deliveries VALUES (?, ?, ?, ?)',
                       (incoming.day, incoming.sequence, incoming.batch_id, incoming.fingerprint))
            publish_progress(db, PublishedHead(batch.day, rule_id, batch.sequence, batch.sequence))
            receipt = incoming

    confirm_through(source, receipt.day, receipt.sequence)
    return receipt


def _waiting(db, correction):
    for day, sequence in ((correction.source_day, correction.source_sequence),
                          (correction.target_day, correction.target_sequence)):
        head = read_published_head(db, day)
        next_expected = 1 if head is None else head.sequence + 1
        if next_expected < sequence:
            return True
        if next_expected > sequence:
            raise DeliveryConflict(f'{day} has already published past the correction member')
    return False


def _apply_member(db, batch):
    head = read_published_head(db, batch.day)
    check_contiguous(head, batch.sequence)
    rule_id = read_initial_rule(db) if head is None else head.rule_id
    apply_current_batch(db, batch, load_rule_version(db, rule_id))
    if has_earlier_baseline(db, batch.day, batch.sequence):
        retain_change(db, batch)
    db.execute('INSERT INTO deliveries VALUES (?, ?, ?, ?)',
               (batch.day, batch.sequence, batch.batch_id, batch.fingerprint))
    publish_progress(db, PublishedHead(batch.day, rule_id, batch.sequence, batch.sequence))


def advance_cross_day(db, source, day, sequence):
    """Receive the joint correction from either of its two published coordinates."""

    day_id(day)
    index(sequence, 'delivery sequence', minimum=1)
    correction = _cross_day_member(source, day, sequence)
    if correction is None:
        raise DeliveryConflict('coordinate is not a cross-day correction member')

    existing = lookup_cross_day_delivery(db, correction.correction_id)
    if existing is not None:
        acknowledge_cross_day(source, correction.correction_id)
        return existing

    with transaction(db, write=True):
        existing = lookup_cross_day_delivery(db, correction.correction_id)
        if existing is not None:
            result = existing
        elif _waiting(db, correction):
            result = CrossDayDelivery(correction.correction_id, 'waiting',
                                      correction.source_day, correction.source_sequence,
                                      correction.target_day, correction.target_sequence,
                                      correction.source_batch_id, correction.target_batch_id)
        else:
            source_batch, target_batch = read_pair(source, correction)
            _apply_member(db, source_batch)
            _apply_member(db, target_batch)
            db.execute('''INSERT INTO cross_day_deliveries VALUES (?, ?, ?, ?, ?, ?, ?)''', (
                correction.correction_id, correction.source_day, correction.source_sequence,
                correction.target_day, correction.target_sequence,
                correction.source_batch_id, correction.target_batch_id,
            ))
            result = CrossDayDelivery(correction.correction_id, 'delivered',
                                      correction.source_day, correction.source_sequence,
                                      correction.target_day, correction.target_sequence,
                                      correction.source_batch_id, correction.target_batch_id)

    if result.status == 'delivered':
        acknowledge_cross_day(source, correction.correction_id)
    return result
