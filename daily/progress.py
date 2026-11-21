from dataclasses import dataclass


class SequenceGap(ValueError):
    pass


@dataclass(frozen=True)
class PublishedHead:
    day: str
    rule_id: str
    sequence: int
    confirmable: int


def ensure_progress_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS warehouse_settings (
        slot INTEGER PRIMARY KEY CHECK(slot = 1), initial_rule TEXT NOT NULL REFERENCES rule_versions(rule_id)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS published_heads (
        day TEXT PRIMARY KEY, rule_id TEXT NOT NULL REFERENCES rule_versions(rule_id),
        sequence INTEGER NOT NULL, confirmable INTEGER NOT NULL
    )''')


def read_initial_rule(db):
    row = db.execute('SELECT initial_rule FROM warehouse_settings WHERE slot = 1').fetchone()
    if row is None:
        raise RuntimeError('warehouse is not initialized')
    return row[0]


def read_published_head(db, day):
    row = db.execute('SELECT day, rule_id, sequence, confirmable FROM published_heads WHERE day = ?',
                     (day,)).fetchone()
    return None if row is None else PublishedHead(*row)


def check_contiguous(head, sequence):
    expected = 1 if head is None else head.sequence + 1
    if sequence != expected:
        raise SequenceGap(f'expected sequence {expected}, received {sequence}')


def publish_progress(db, head):
    db.execute('''INSERT INTO published_heads VALUES (?, ?, ?, ?)
        ON CONFLICT(day) DO UPDATE SET rule_id = excluded.rule_id,
        sequence = excluded.sequence, confirmable = excluded.confirmable''',
        (head.day, head.rule_id, head.sequence, head.confirmable))
