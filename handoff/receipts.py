from dataclasses import dataclass


@dataclass(frozen=True)
class IssueReceipt:
    number: str
    day: str
    rule_id: str
    sequence: int


def ensure_receipt_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS issue_receipts (
        number TEXT PRIMARY KEY REFERENCES statements(number), day TEXT NOT NULL,
        rule_id TEXT NOT NULL, sequence INTEGER NOT NULL
    )''')


def record_issue_receipt(db, receipt):
    db.execute('INSERT INTO issue_receipts VALUES (?, ?, ?, ?)',
               (receipt.number, receipt.day, receipt.rule_id, receipt.sequence))


def load_issue_receipt(db, number):
    row = db.execute('SELECT number, day, rule_id, sequence FROM issue_receipts WHERE number = ?',
                     (number,)).fetchone()
    return None if row is None else IssueReceipt(*row)
