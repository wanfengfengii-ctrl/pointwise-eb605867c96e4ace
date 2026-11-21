import sqlite3

from handoff.archive import load_statement
from handoff.audit import lookup_provenance, verify_statement_totals
from handoff.issue import StatementConflict, issue
from handoff.readback import LedgerNotFound, read
from handoff.receipts import load_issue_receipt
from source.feed import reclaim_confirmed
from tests.support import DAY, NEXT_DAY, LedgerCase, deny_next_commit, totals


class HandoffTests(LedgerCase):
    def test_failed_issue(self):
        prepared = self.prepare(0)
        self.step(prepared, 'start')
        self.step(prepared, 'scan')
        self.step(prepared, 'activate')
        self.add('reading', '青站', 7000)
        self.add('missing', '河站', None)
        live = read(self.warehouse, day=DAY)
        self.assertEqual((('北组', 7, 1), ('南组', 0, 0)), totals(live))
        with deny_next_commit(self.warehouse), self.assertRaises(sqlite3.DatabaseError):
            issue(self.warehouse, 'night', DAY)
        self.reopen()
        self.assertEqual(live, read(self.warehouse, day=DAY))
        self.assertIsNone(load_statement(self.warehouse, 'night'))
        self.assertIsNone(load_issue_receipt(self.warehouse, 'night'))
        with self.assertRaises(LedgerNotFound):
            read(self.warehouse, number='night')
        with self.assertRaises(KeyError):
            lookup_provenance(self.warehouse, 'night')

        fixed = issue(self.warehouse, 'night', DAY)
        self.assertTrue(verify_statement_totals(fixed.record))
        missing, = lookup_provenance(self.warehouse, 'night', region='南组')
        self.assertEqual(('河站', 'missing', 'mL', None, None), (
            missing.image.key.station, missing.image.revision, missing.image.reading.unit,
            missing.image.reading.value_ml, missing.image.reading.litres,
        ))
        self.assertEqual((), lookup_provenance(self.warehouse, 'night', station='石站'))
        self.assertEqual(fixed.receipt, load_issue_receipt(self.warehouse, 'night'))
        reclaim_confirmed(self.source, DAY)
        self.reopen(source=False)
        self.assertEqual(fixed, issue(self.warehouse, 'night', DAY))
        self.assertEqual(fixed, read(self.warehouse, number='night'))
        with self.assertRaises(StatementConflict):
            issue(self.warehouse, 'night', NEXT_DAY)
        self.assertEqual(fixed, read(self.warehouse, number='night'))

    def test_unknown_day_and_published_empty_ledger(self):
        with self.assertRaises(LedgerNotFound):
            issue(self.warehouse, 'empty', DAY)
        self.assertIsNone(load_issue_receipt(self.warehouse, 'empty'))
        prepared = self.prepare(0)
        self.step(prepared, 'start')
        self.step(prepared, 'scan')
        self.assertEqual('activated', self.step(prepared, 'activate').status)
        empty = read(self.warehouse, day=DAY)
        self.assertEqual(('new', 0, (), ()), (empty.rule_id, empty.sequence, empty.totals, empty.rows))
        self.assertEqual(empty, issue(self.warehouse, 'empty', DAY).record.snapshot)
        with self.assertRaises(LedgerNotFound):
            read(self.warehouse, day=NEXT_DAY)
