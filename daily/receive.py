from dataclasses import dataclass

from catalog.manifest import has_earlier_baseline, retain_change
from catalog.rules import load_rule_version
from daily.balance import apply_current_batch
from daily.progress import PublishedHead, check_contiguous, publish_progress, read_initial_rule, read_published_head
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


def ensure_delivery_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
        day TEXT NOT NULL, sequence INTEGER NOT NULL, batch_id TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL, PRIMARY KEY(day, sequence)
    )''')


def lookup_delivery(db, day, sequence):
    day_id(day)
    index(sequence, 'delivery sequence', minimum=1)
    row = db.execute('SELECT day, sequence, batch_id, fingerprint FROM deliveries WHERE day = ? AND sequence = ?',
                     (day, sequence)).fetchone()
    return None if row is None else DeliveryReceipt(*row)


def advance(db, source, batch):
    if not isinstance(batch, ChangeBatch):
        raise ValueError('advance requires a ChangeBatch')
    day_id(batch.day)
    index(batch.sequence, 'delivery sequence', minimum=1)
    identifier(batch.batch_id, 'batch id')
    incoming = DeliveryReceipt(batch.day, batch.sequence, batch.batch_id, batch.fingerprint)
    with transaction(db, write=True):
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
