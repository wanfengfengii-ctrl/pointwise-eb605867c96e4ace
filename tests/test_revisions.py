from handoff.audit import lookup_provenance
from handoff.issue import issue
from handoff.readback import read
from source.feed import confirm_through, read_changes, reclaim_confirmed, source_frontiers
from source.intake import lookup_submission, submit
from source.revisions import RevisionConflict
from source.rows import ObservationKey, read_row_head
from tests.support import DAY, LedgerCase, totals


class RevisionTests(LedgerCase):
    def test_fixed_statement_survives(self):
        first = self.add('initial', '青站', 7000)
        old = issue(self.warehouse, 'original', DAY)
        changed = submit(self.source, 'fix', DAY, '青站', 'r1', operation='correct',
                         expected_revision=first.revision, ml=11000)
        change, = read_changes(self.source, DAY, after=1)
        self.assertEqual((7000, 11000), (change.before.reading.value_ml, change.after.reading.value_ml))
        self.assertEqual((1, 1), (change.before.ordinal, change.after.ordinal))
        self.deliver(changed.sequence)
        live = read(self.warehouse, day=DAY)
        self.assertEqual((('湾组', 11, 1),), totals(live))
        self.assertEqual(4, live.totals[0].litres - old.record.snapshot.totals[0].litres)
        self.assertEqual(('fix', 2), (live.rows[0].image.revision, live.rows[0].image.sequence))
        reclaim_confirmed(self.source, DAY)
        self.reopen(source=False)
        self.assertEqual(old, read(self.warehouse, number='original'))
        self.assertEqual(old, issue(self.warehouse, 'original', DAY))
        row, = lookup_provenance(self.warehouse, 'original', station='青站')
        self.assertEqual(('initial', 7000), (row.image.revision, row.image.reading.value_ml))

    def test_conflicting_revision(self):
        first = self.add('initial', '青站', 7000)
        submit(self.source, 'fix', DAY, '青站', 'r1', operation='correct',
               ml=11000, expected_revision=first.revision)
        self.deliver(2)
        live = read(self.warehouse, day=DAY)
        source_before = read_changes(self.source, DAY)
        with self.assertRaises(RevisionConflict):
            submit(self.source, 'stale', DAY, '青站', 'r1', operation='correct', ml=9000, expected_revision='initial')
        with self.assertRaises(RevisionConflict):
            submit(self.source, 'fix', DAY, '青站', 'r1', operation='correct', ml=9000, expected_revision='initial')
        self.assertIsNone(lookup_submission(self.source, 'stale'))
        self.assertEqual(source_before, read_changes(self.source, DAY))
        self.assertEqual((2, 2), source_frontiers(self.source, DAY))
        self.assertEqual(live, read(self.warehouse, day=DAY))
        self.assertEqual('fix', read_row_head(self.source, ObservationKey(DAY, '青站', 'r1')).revision)

    def test_withdraw_after_issue(self):
        self.add('initial', '青站', 7000)
        old = issue(self.warehouse, 'original', DAY)
        withdrawn = submit(self.source, 'withdraw', DAY, '青站', 'r1', operation='withdraw', expected_revision='initial')
        batch, = read_changes(self.source, DAY, after=1)
        self.assertEqual(7, batch.before.reading.litres)
        self.assertIsNone(batch.after)
        self.deliver(withdrawn.sequence)
        live = read(self.warehouse, day=DAY)
        self.assertEqual((2, (), ()), (live.sequence, live.totals, live.rows))
        tombstone = read_row_head(self.source, ObservationKey(DAY, '青站', 'r1'))
        self.assertEqual((1, 'withdraw', None), (tombstone.ordinal, tombstone.revision, tombstone.image))
        with self.assertRaises(RevisionConflict):
            submit(self.source, 'late', DAY, '青站', 'r1', operation='correct', ml=1000, expected_revision='initial')
        self.assertEqual(2, confirm_through(self.source, DAY, 1))
        self.assertEqual(old, read(self.warehouse, number='original'))
