from dataclasses import replace

from daily.snapshot import read_published_snapshot
from handoff.archive import StatementRecord, load_statement, store_statement
from handoff.audit import build_provenance_index, verify_statement_totals
from handoff.readback import LedgerNotFound, assemble_issued
from handoff.receipts import IssueReceipt, load_issue_receipt, record_issue_receipt
from storage import day_id, identifier, transaction


class StatementConflict(ValueError):
    pass


def issue(db, number, day):
    identifier(number, 'statement number')
    day_id(day)
    with transaction(db, write=True):
        existing = load_statement(db, number)
        if existing is not None:
            if existing.snapshot.day != day:
                raise StatementConflict('statement number already belongs to another day')
            return assemble_issued(existing, load_issue_receipt(db, number))
        snapshot = read_published_snapshot(db, day)
        if snapshot is None:
            raise LedgerNotFound(f'no published ledger for {day}')
        snapshot = replace(snapshot, rows=build_provenance_index(snapshot))
        record = StatementRecord(number, snapshot)
        if not verify_statement_totals(record):
            raise ValueError('published totals differ from the published row provenance')
        receipt = IssueReceipt(number, day, snapshot.rule_id, snapshot.sequence)
        store_statement(db, record)
        record_issue_receipt(db, receipt)
        return assemble_issued(record, receipt)
