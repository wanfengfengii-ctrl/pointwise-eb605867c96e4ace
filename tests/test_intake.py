import sqlite3
from dataclasses import replace

from catalog.bootstrap import initialize_source, initialize_warehouse
from catalog.rules import RuleVersion
from daily.progress import SequenceGap, read_published_head
from daily.receive import DeliveryConflict, advance, lookup_delivery
from handoff.audit import lookup_provenance, verify_statement_totals
from handoff.issue import issue
from handoff.readback import LedgerNotFound, read
from source.feed import read_changes, reclaim_confirmed, source_frontiers
from source.intake import lookup_submission, submit
from source.rows import ObservationKey, read_row_head
from storage import connect
from tests.support import DAY, NEXT_DAY, OLD, LedgerCase, deny_next_commit, report, totals


class IntakeTests(LedgerCase):
    def test_normalized_issue(self):
        self.add('a', '青站', 7000)
        self.add('b', '河站', 11000)
        fixed = issue(self.warehouse, 'shift-A', DAY)
        actual = totals(read(self.warehouse, day=DAY))
        expected = (('湾组', 18, 2),)
        provenance = lookup_provenance(self.warehouse, 'shift-A', region='湾组')
        downstream = tuple((r.image.key.station, r.image.reading.value_ml, r.image.revision) for r in provenance)
        expected_sources = (('青站', 7000, 'a'), ('河站', 11000, 'b'))
        self.assertEqual(expected, actual)
        self.assertEqual(expected, totals(fixed.record.snapshot))
        self.assertEqual(expected_sources, downstream)
        self.assertEqual(('shift-A', DAY, 'old', 2),
                         (fixed.receipt.number, fixed.receipt.day, fixed.receipt.rule_id, fixed.receipt.sequence))
        self.assertEqual(fixed, read(self.warehouse, number='shift-A'))
        self.assertTrue(verify_statement_totals(fixed.record))
        report('n1', [7000, 11000], expected, actual, expected_sources, downstream,
               ['source/normalize.py', 'daily/receive.py', 'handoff/issue.py', 'handoff/audit.py'],
               '手算 7000÷1000 + 11000÷1000 = 18，按原批次复核两条出处')

    def test_exact_units_and_zero(self):
        for number, bad in enumerate((True, False, 7500, 7000.0, '7000')):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.add(f'invalid-{number}', '青站', bad)
                self.assertIsNone(lookup_submission(self.source, f'invalid-{number}'))
        self.assertEqual((0, 0), source_frontiers(self.source, DAY))
        self.assertIsNone(read_published_head(self.warehouse, DAY))
        self.add('zero', '青站', 0)
        self.add('missing', '河站', None)
        fixed = issue(self.warehouse, 'zero-shift', DAY)
        self.assertEqual((('湾组', 0, 1),), totals(fixed.record.snapshot))
        self.assertEqual((0, None), tuple(row.image.reading.value_ml for row in fixed.record.snapshot.rows))
        self.assertTrue(verify_statement_totals(fixed.record))

    def test_gap_then_duplicate(self):
        self.add('a', '青站', 7000, deliver=False)
        self.add('b', '河站', 11000, deliver=False)
        first, second = read_changes(self.source, DAY)
        with self.assertRaises(SequenceGap):
            advance(self.warehouse, self.source, second)
        with self.assertRaises(LedgerNotFound):
            read(self.warehouse, day=DAY)
        self.assertIsNone(lookup_delivery(self.warehouse, DAY, 2))
        self.assertEqual((2, 0), source_frontiers(self.source, DAY))
        self.assertEqual(0, reclaim_confirmed(self.source, DAY))

        advance(self.warehouse, self.source, first)
        receipt = advance(self.warehouse, self.source, second)
        committed = read(self.warehouse, day=DAY)
        self.assertEqual(2, committed.sequence)
        self.assertEqual((('湾组', 18, 2),), totals(committed))
        self.assertEqual(2, reclaim_confirmed(self.source, DAY))
        self.assertEqual(receipt, advance(self.warehouse, self.source, second))
        with self.assertRaises(DeliveryConflict):
            advance(self.warehouse, self.source, replace(second, batch_id='changed'))
        self.assertEqual((2, 2), source_frontiers(self.source, DAY))
        self.assertEqual(committed, read(self.warehouse, day=DAY))
        self.assertEqual((), read_changes(self.source, DAY))
        self.assertEqual(2, self.add('b', '河站', 11000, deliver=False).sequence)
        self.assertEqual(2, lookup_submission(self.source, 'b').sequence)

    def test_source_and_warehouse_commit_failures_and_ack_retry(self):
        with deny_next_commit(self.source), self.assertRaises(sqlite3.DatabaseError):
            self.add('a', '青站', 7000, deliver=False)
        self.assertIsNone(read_row_head(self.source, ObservationKey(DAY, '青站', 'r1')))
        self.assertEqual((0, 0), source_frontiers(self.source, DAY))
        self.assertIsNone(lookup_submission(self.source, 'a'))

        self.add('a', '青站', 7000, deliver=False)
        batch, = read_changes(self.source, DAY)
        with deny_next_commit(self.warehouse), self.assertRaises(sqlite3.DatabaseError):
            advance(self.warehouse, self.source, batch)
        self.assertIsNone(lookup_delivery(self.warehouse, DAY, 1))
        self.assertIsNone(read_published_head(self.warehouse, DAY))
        self.assertEqual((1, 0), source_frontiers(self.source, DAY))
        with deny_next_commit(self.source), self.assertRaises(sqlite3.DatabaseError):
            advance(self.warehouse, self.source, batch)
        receipt = lookup_delivery(self.warehouse, DAY, 1)
        self.assertIsNotNone(receipt)
        self.assertEqual((1, 0), source_frontiers(self.source, DAY))
        self.assertEqual((('湾组', 7, 1),), totals(read(self.warehouse, day=DAY)))
        self.reopen()
        self.assertEqual(receipt, advance(self.warehouse, self.source, batch))
        self.assertEqual((1, 1), source_frontiers(self.source, DAY))
        self.assertEqual((('湾组', 7, 1),), totals(read(self.warehouse, day=DAY)))

    def test_reinitialize_preserves_data(self):
        custom = RuleVersion('custom', {'青站': '上游组', '河站': '下游组'})
        self.warehouse.close()
        self.warehouse_path = self.warehouse_path.with_name('custom.sqlite')
        self.warehouse = connect(self.warehouse_path)
        initialize_warehouse(self.warehouse, custom)
        first = self.add('custom-a', '青站', 7000)
        old = issue(self.warehouse, 'custom-shift', DAY)
        self.reopen()
        initialize_source(self.source)
        initialize_warehouse(self.warehouse, OLD)
        self.assertEqual(first, lookup_submission(self.source, 'custom-a'))
        self.assertEqual((1, 1), source_frontiers(self.source, DAY))
        self.assertEqual(old, read(self.warehouse, number='custom-shift'))
        second = submit(self.source, 'custom-b', DAY, '青站', 'r1', operation='correct',
                        ml=11000, expected_revision=first.revision)
        self.deliver(second.sequence)
        live = read(self.warehouse, day=DAY)
        self.assertEqual(('custom', 2, (('上游组', 11, 1),)), (live.rule_id, live.sequence, totals(live)))
        self.add('custom-c', '河站', 3000, day=NEXT_DAY)
        next_day = read(self.warehouse, day=NEXT_DAY)
        self.assertEqual(('custom', 1, (('下游组', 3, 1),)),
                         (next_day.rule_id, next_day.sequence, totals(next_day)))
        self.assertEqual(old, issue(self.warehouse, 'custom-shift', DAY))
