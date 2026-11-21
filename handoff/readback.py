from dataclasses import dataclass

from daily.snapshot import read_published_snapshot
from handoff.archive import StatementRecord, load_statement
from handoff.receipts import IssueReceipt, load_issue_receipt
from storage import day_id, identifier, read_scope


class LedgerNotFound(LookupError):
    pass


@dataclass(frozen=True)
class IssuedStatement:
    record: StatementRecord
    receipt: IssueReceipt


def assemble_issued(record, receipt):
    snapshot = record.snapshot
    expected = IssueReceipt(record.number, snapshot.day, snapshot.rule_id, snapshot.sequence)
    if receipt != expected:
        raise ValueError('statement and receipt identity differ')
    return IssuedStatement(record, receipt)


def read(db, *, day=None, number=None):
    if (day is None) == (number is None):
        raise ValueError('select exactly one day or statement number')
    if day is not None:
        day_id(day)
        snapshot = read_published_snapshot(db, day)
        if snapshot is None:
            raise LedgerNotFound(f'no published ledger for {day}')
        return snapshot

    identifier(number, 'statement number')
    with read_scope(db):
        record = load_statement(db, number)
        if record is None:
            raise LedgerNotFound(f'unknown statement {number}')
        return assemble_issued(record, load_issue_receipt(db, number))
