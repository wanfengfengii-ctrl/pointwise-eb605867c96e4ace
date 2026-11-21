import json
import sqlite3
from pathlib import Path

from catalog.baseline import prepare
from catalog.rules import RuleVersion
from daily.receive import (
    DeliveryConflict, advance, advance_cross_day, lookup_cross_day_delivery, lookup_delivery,
)
from handoff.audit import lookup_provenance
from handoff.issue import issue
from handoff.readback import read
from source.cross_day import protected_sequences
from source.feed import read_changes, reclaim_confirmed, source_frontiers
from source.intake import (
    lookup_cross_day, lookup_cross_day_coordinate, lookup_submission, submit, submit_cross_day,
)
from source.revisions import RevisionConflict
from source.rows import ObservationKey, read_row_head
from tests.support import LedgerCase, deny_next_commit, totals

SOURCE_DAY = '2026-11-12'
TARGET_DAY = '2026-11-13'
OTHER_DAY = '2026-11-14'

NEW = RuleVersion('new', {'青站': '北组', '河站': '南组', '石站': '南组'})


def sample():
    path = Path(__file__).resolve().parents[1] / 'examples' / 'cross_day.json'
    return json.loads(path.read_text(encoding='utf-8'))


class CrossDayCase(LedgerCase):
    def build_sample(self):
        data = sample()
        for opening in data['openings']:
            self.add(opening['batch_id'], opening['station'], opening['ml'],
                     reading_id=opening['reading_id'], day=opening['day'])
        pending = data['pending_target']
        pending_receipt = submit(self.source, pending['batch_id'], pending['day'],
                                 pending['station'], pending['reading_id'], ml=pending['ml'])
        return data, pending_receipt

    def accept_correction(self, data, **overrides):
        correction = {**data['correction'], **overrides}
        return submit_cross_day(
            self.source, correction['correction_id'], correction['source_day'],
            correction['target_day'], correction['station'], correction['reading_id'],
            expected_revision=correction['expected_revision'],
        )

    def source_batch(self, receipt):
        return read_changes(self.source, receipt.source_day,
                            after=receipt.source_sequence - 1,
                            through=receipt.source_sequence)[0]

    def target_batch(self, receipt):
        return read_changes(self.source, receipt.target_day,
                            after=receipt.target_sequence - 1,
                            through=receipt.target_sequence)[0]


class CrossDaySourceTests(CrossDayCase):
    def test_receipt_coordinates_and_original_binding(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.assertEqual(('move-7', SOURCE_DAY, 2, TARGET_DAY, 3),
                         (receipt.correction_id, receipt.source_day, receipt.source_sequence,
                          receipt.target_day, receipt.target_sequence))
        self.assertEqual(2, source_frontiers(self.source, SOURCE_DAY)[0])
        self.assertEqual(3, source_frontiers(self.source, TARGET_DAY)[0])
        self.assertEqual(receipt, lookup_cross_day(self.source, 'move-7'))
        self.assertEqual(receipt, lookup_cross_day_coordinate(self.source, SOURCE_DAY, 2))
        self.assertEqual(receipt, lookup_cross_day_coordinate(self.source, TARGET_DAY, 3))

        source_batch, target_batch = self.source_batch(receipt), self.target_batch(receipt)
        self.assertEqual((7000, None),
                         (source_batch.before.reading.value_ml, source_batch.after))
        self.assertEqual(7000, target_batch.after.reading.value_ml)
        self.assertIsNone(target_batch.before)
        # The original reading revision and reading id survive the move.
        self.assertEqual('source-open', target_batch.after.revision)
        self.assertEqual('r1', target_batch.after.key.reading_id)
        self.assertEqual(TARGET_DAY, target_batch.after.key.day)
        self.assertEqual(receipt.source_fingerprint, source_batch.fingerprint)
        self.assertEqual(receipt.target_fingerprint, target_batch.fingerprint)

    def test_source_rows_and_submissions(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        source_head = read_row_head(self.source, ObservationKey(SOURCE_DAY, '青站', 'r1'))
        self.assertEqual((1, 'move-7~source', None),
                         (source_head.ordinal, source_head.revision, source_head.image))
        target_head = read_row_head(self.source, ObservationKey(TARGET_DAY, '青站', 'r1'))
        self.assertEqual((3, 'source-open', TARGET_DAY),
                         (target_head.ordinal, target_head.revision, target_head.image.key.day))
        self.assertEqual(receipt.source_sequence, lookup_submission(self.source, 'move-7~source').sequence)
        self.assertEqual(receipt.target_sequence, lookup_submission(self.source, 'move-7~target').sequence)

    def test_same_id_replay_and_conflicts(self):
        data, _ = self.build_sample()
        first = self.accept_correction(data)
        self.assertEqual(first, self.accept_correction(data))

        before_source = read_changes(self.source, SOURCE_DAY)
        before_target = read_changes(self.source, TARGET_DAY)
        with self.assertRaises(RevisionConflict):
            self.accept_correction(data, expected_revision='target-open')
        with self.assertRaises(RevisionConflict):
            self.accept_correction(data, station='河站')
        with self.assertRaises(RevisionConflict):
            self.accept_correction(data, target_day=OTHER_DAY)
        with self.assertRaises(ValueError):
            self.accept_correction(data, target_day=SOURCE_DAY)
        # No partial second append and no second correction record.
        self.assertEqual(before_source, read_changes(self.source, SOURCE_DAY))
        self.assertEqual(before_target, read_changes(self.source, TARGET_DAY))
        self.assertEqual(0, source_frontiers(self.source, OTHER_DAY)[0])

    def test_used_target_identity_rejected(self):
        data, _ = self.build_sample()
        # 河站 r1 already occupies the target day; moving 青站 r1 there is fine,
        # but the same 青站 r1 cannot be moved twice under a different id.
        first = self.accept_correction(data)
        with self.assertRaises(RevisionConflict):
            submit_cross_day(self.source, 'move-again', SOURCE_DAY, TARGET_DAY, '青站', 'r1',
                             expected_revision='source-open')
        self.assertEqual(first, lookup_cross_day(self.source, 'move-7'))
        self.assertIsNone(lookup_cross_day(self.source, 'move-again'))

    def test_stale_revision_and_target_in_use(self):
        data, _ = self.build_sample()
        with self.assertRaises(RevisionConflict):
            self.accept_correction(data, expected_revision='ghost')
        self.assertIsNone(lookup_cross_day(self.source, 'move-7'))
        # A reading already living on the target day cannot be overwritten.
        submit(self.source, 'dup-target', TARGET_DAY, '青站', 'r1', ml=1000)
        with self.assertRaises(RevisionConflict):
            self.accept_correction(data)
        self.assertIsNone(lookup_cross_day(self.source, 'move-7'))
        self.assertEqual((1, 1), source_frontiers(self.source, SOURCE_DAY))
        self.assertEqual(3, source_frontiers(self.source, TARGET_DAY)[0])

    def test_batch_identity_collision_rejected(self):
        data, _ = self.build_sample()
        submit(self.source, 'move-7~target', TARGET_DAY, '河站', 'r3', ml=5000)
        before_source = read_changes(self.source, SOURCE_DAY)
        before_target = read_changes(self.source, TARGET_DAY)
        with self.assertRaises(RevisionConflict):
            self.accept_correction(data)
        self.assertIsNone(lookup_cross_day(self.source, 'move-7'))
        self.assertEqual(before_source, read_changes(self.source, SOURCE_DAY))
        self.assertEqual(before_target, read_changes(self.source, TARGET_DAY))

    def test_empty_target_day_takes_first_sequence(self):
        # Move onto a target day that has no batches yet: both sides are 1.
        self.add('only', '石站', 4000)
        receipt = submit_cross_day(self.source, 'move-first', SOURCE_DAY, OTHER_DAY,
                                   '石站', 'r1', expected_revision='only')
        # Target day has never had a batch, so it takes sequence 1.
        self.assertEqual((2, 1), (receipt.source_sequence, receipt.target_sequence))
        delivered = advance_cross_day(self.warehouse, self.source, OTHER_DAY, 1)
        self.assertEqual('delivered', delivered.status)
        self.assertEqual((), read(self.warehouse, day=SOURCE_DAY).totals)
        self.assertEqual((('湾组', 4, 1),), totals(read(self.warehouse, day=OTHER_DAY)))

    def test_submit_rollback_leaves_nothing(self):
        data, _ = self.build_sample()
        with deny_next_commit(self.source), self.assertRaises(sqlite3.DatabaseError):
            self.accept_correction(data)
        self.assertIsNone(lookup_cross_day(self.source, 'move-7'))
        self.assertIsNone(lookup_submission(self.source, 'move-7~source'))
        self.assertIsNone(lookup_submission(self.source, 'move-7~target'))
        self.assertEqual(1, source_frontiers(self.source, SOURCE_DAY)[0])
        self.assertEqual(2, source_frontiers(self.source, TARGET_DAY)[0])
        self.assertEqual('source-open',
                         read_row_head(self.source, ObservationKey(SOURCE_DAY, '青站', 'r1')).revision)
        self.assertIsNone(read_row_head(self.source, ObservationKey(TARGET_DAY, '青站', 'r1')))
        self.assertEqual(frozenset(), protected_sequences(self.source, SOURCE_DAY))


class CrossDayDeliveryTests(CrossDayCase):
    def test_waiting_then_joint_apply_from_either_side(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        before_source = read(self.warehouse, day=SOURCE_DAY)
        before_target = read(self.warehouse, day=TARGET_DAY)
        waiting = advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        self.assertEqual(('waiting', 'move-7'), (waiting.status, waiting.correction_id))
        self.assertEqual(before_source, read(self.warehouse, day=SOURCE_DAY))
        self.assertEqual(before_target, read(self.warehouse, day=TARGET_DAY))
        self.assertEqual(waiting, advance_cross_day(self.warehouse, self.source,
                                                   TARGET_DAY, receipt.target_sequence))
        self.assertEqual(before_source, read(self.warehouse, day=SOURCE_DAY))
        self.assertEqual(before_target, read(self.warehouse, day=TARGET_DAY))
        self.assertIsNone(lookup_delivery(self.warehouse, SOURCE_DAY, 2))
        self.assertIsNone(lookup_delivery(self.warehouse, TARGET_DAY, 3))

        # Another day keeps flowing while the pair waits.
        other = data['ordinary_other_day']
        self.add(other['batch_id'], other['station'], other['ml'],
                 reading_id=other['reading_id'], day=other['day'])
        self.assertEqual((('湾组', 1, 1),), totals(read(self.warehouse, day=OTHER_DAY)))

        # Fill the missing ordinary predecessor on the target day.
        pending_batch = read_changes(self.source, TARGET_DAY, after=1, through=2)[0]
        self.assertEqual(2, advance(self.warehouse, self.source, pending_batch).sequence)
        self.assertEqual(2, read(self.warehouse, day=TARGET_DAY).sequence)

        delivered = advance_cross_day(self.warehouse, self.source, TARGET_DAY, receipt.target_sequence)
        self.assertEqual('delivered', delivered.status)
        source_live = read(self.warehouse, day=SOURCE_DAY)
        target_live = read(self.warehouse, day=TARGET_DAY)
        self.assertEqual((2, (), ()), (source_live.sequence, source_live.totals, source_live.rows))
        self.assertEqual((3, (('湾组', 12, 3),)), (target_live.sequence, totals(target_live)))
        self.assertEqual(3, len(target_live.rows))
        self.assertEqual(('r2', 'r1'),
                         (target_live.rows[1].image.key.reading_id,
                          target_live.rows[2].image.key.reading_id))
        self.assertEqual('青站', target_live.rows[2].image.key.station)
        self.assertEqual(3, len(target_live.rows))
        # The delivered correction is idempotent from either coordinate.
        self.assertEqual(delivered, advance_cross_day(self.warehouse, self.source,
                                                     SOURCE_DAY, receipt.source_sequence))
        self.assertEqual(delivered, lookup_cross_day_delivery(self.warehouse, 'move-7'))
        self.assertEqual(target_live, read(self.warehouse, day=TARGET_DAY))

    def test_target_day_three_rows_and_provenance(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        issued = issue(self.warehouse, 'target-shift', TARGET_DAY)
        rows = lookup_provenance(self.warehouse, 'target-shift', station='青站')
        self.assertEqual(1, len(rows))
        moved = rows[0].image
        self.assertEqual(('r1', 'source-open', 7000),
                         (moved.key.reading_id, moved.revision, moved.reading.value_ml))
        self.assertEqual(('湾组', 12, 3),
                         (issued.record.snapshot.totals[0].region,
                          issued.record.snapshot.totals[0].litres,
                          issued.record.snapshot.totals[0].valid_rows))

    def test_ordinary_advance_cannot_publish_one_side(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        source_batch = self.source_batch(receipt)
        target_batch = self.target_batch(receipt)
        before_source = read(self.warehouse, day=SOURCE_DAY)
        before_target = read(self.warehouse, day=TARGET_DAY)
        with self.assertRaises(DeliveryConflict):
            advance(self.warehouse, self.source, source_batch)
        with self.assertRaises(DeliveryConflict):
            advance(self.warehouse, self.source, target_batch)
        self.assertIsNone(lookup_delivery(self.warehouse, SOURCE_DAY, 2))
        self.assertIsNone(lookup_delivery(self.warehouse, TARGET_DAY, 3))
        self.assertEqual(before_source, read(self.warehouse, day=SOURCE_DAY))
        self.assertEqual(before_target, read(self.warehouse, day=TARGET_DAY))

    def test_unknown_coordinate_rejected(self):
        data, _ = self.build_sample()
        self.accept_correction(data)
        with self.assertRaises(DeliveryConflict):
            advance_cross_day(self.warehouse, self.source, SOURCE_DAY, 1)

    def test_third_day_ordinary_batches_keep_flowing(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        other = data['ordinary_other_day']
        other_receipt = submit(self.source, other['batch_id'], other['day'],
                               other['station'], other['reading_id'], ml=other['ml'])
        batch, = read_changes(self.source, OTHER_DAY,
                              after=other_receipt.sequence - 1, through=other_receipt.sequence)
        advance(self.warehouse, self.source, batch)
        third = read(self.warehouse, day=OTHER_DAY)
        self.assertEqual((1, (('湾组', 1, 1),)), (third.sequence, totals(third)))
        # A later source day may also review its own correction normally.
        submit(self.source, 'third-fix', OTHER_DAY, '石站', 'r1', operation='correct',
               ml=2000, expected_revision='other-day')
        self.deliver(2, day=OTHER_DAY)
        self.assertEqual((('湾组', 2, 1),), totals(read(self.warehouse, day=OTHER_DAY)))
        self.assertEqual(2, reclaim_confirmed(self.source, OTHER_DAY))

    def test_delivery_rollback(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        before_source = read(self.warehouse, day=SOURCE_DAY)
        before_target = read(self.warehouse, day=TARGET_DAY)
        with deny_next_commit(self.warehouse), self.assertRaises(sqlite3.DatabaseError):
            advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        self.reopen()
        self.assertIsNone(lookup_cross_day_delivery(self.warehouse, 'move-7'))
        self.assertEqual(before_source, read(self.warehouse, day=SOURCE_DAY))
        self.assertEqual(before_target, read(self.warehouse, day=TARGET_DAY))
        self.assertIsNone(lookup_delivery(self.warehouse, SOURCE_DAY, 2))
        self.assertIsNone(lookup_delivery(self.warehouse, TARGET_DAY, 3))
        delivered = advance_cross_day(self.warehouse, self.source, TARGET_DAY, receipt.target_sequence)
        self.assertEqual('delivered', delivered.status)
        self.assertEqual((('湾组', 12, 3),), totals(read(self.warehouse, day=TARGET_DAY)))

    def test_ack_failure_reopen_and_confirm_from_other_side(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        with deny_next_commit(self.source), self.assertRaises(sqlite3.DatabaseError):
            advance_cross_day(self.warehouse, self.source, TARGET_DAY, receipt.target_sequence)
        # Joint receipt committed even though the source acknowledgement failed.
        stored = lookup_cross_day_delivery(self.warehouse, 'move-7')
        self.assertIsNotNone(stored)
        self.assertEqual((2, 3), (stored.source_sequence, stored.target_sequence))
        self.assertEqual((1, 2), (source_frontiers(self.source, SOURCE_DAY)[1],
                                  source_frontiers(self.source, TARGET_DAY)[1]))
        target_live = read(self.warehouse, day=TARGET_DAY)
        self.assertEqual((('湾组', 12, 3),), totals(target_live))

        self.reopen()
        confirmed = advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        self.assertEqual(stored, confirmed)
        self.assertEqual((2, 3), (source_frontiers(self.source, SOURCE_DAY)[1],
                                  source_frontiers(self.source, TARGET_DAY)[1]))
        self.assertEqual(target_live, read(self.warehouse, day=TARGET_DAY))
        self.assertEqual((2, ), (read(self.warehouse, day=SOURCE_DAY).sequence,))


class CrossDayReclaimTests(CrossDayCase):
    def delivered_pair(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        return data, receipt

    def test_protection_until_both_sides_confirmed(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        # Ordinary confirmation of the source day alone must not release originals.
        from source.feed import confirm_through
        confirm_through(self.source, SOURCE_DAY, receipt.source_sequence)
        self.assertIn(receipt.source_sequence, protected_sequences(self.source, SOURCE_DAY))
        reclaim_confirmed(self.source, SOURCE_DAY)
        # Only the ordinary predecessor went; the paired original remains.
        self.assertEqual((receipt.source_sequence,),
                         tuple(batch.sequence for batch in read_changes(self.source, SOURCE_DAY)))
        self.assertEqual(1, len(read_changes(self.source, SOURCE_DAY, after=1)))
        # Target day still unconfirmed: its original also stays.
        self.assertIn(receipt.target_sequence, protected_sequences(self.source, TARGET_DAY))
        reclaim_confirmed(self.source, TARGET_DAY)
        self.assertEqual(1, len(read_changes(self.source, TARGET_DAY, after=receipt.target_sequence - 1)))

        # Joint delivery acknowledges both sides, then both may be reclaimed.
        advance_cross_day(self.warehouse, self.source, TARGET_DAY, receipt.target_sequence)
        self.assertEqual(frozenset(), protected_sequences(self.source, SOURCE_DAY))
        self.assertEqual(frozenset(), protected_sequences(self.source, TARGET_DAY))
        self.assertEqual(1, reclaim_confirmed(self.source, SOURCE_DAY))
        self.assertEqual(1, reclaim_confirmed(self.source, TARGET_DAY))
        self.assertEqual((), read_changes(self.source, SOURCE_DAY))
        self.assertEqual((), read_changes(self.source, TARGET_DAY))
        self.assertEqual(receipt, lookup_cross_day(self.source, 'move-7'))

    def test_no_reclaim_before_joint_delivery_even_if_confirmed(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        from source.feed import confirm_through
        confirm_through(self.source, SOURCE_DAY, receipt.source_sequence)
        confirm_through(self.source, TARGET_DAY, receipt.target_sequence)
        reclaim_confirmed(self.source, SOURCE_DAY)
        reclaim_confirmed(self.source, TARGET_DAY)
        self.assertEqual((receipt.source_sequence,),
                         tuple(batch.sequence for batch in read_changes(self.source, SOURCE_DAY)))
        self.assertEqual((receipt.target_sequence,),
                         tuple(batch.sequence for batch in read_changes(self.source, TARGET_DAY)))
        self.assertEqual(receipt, lookup_cross_day(self.source, 'move-7'))
        # Originals remain readable and the joint correction can still be delivered.
        delivered = advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        self.assertEqual('delivered', delivered.status)
        self.assertEqual(1, reclaim_confirmed(self.source, SOURCE_DAY))
        self.assertEqual(1, reclaim_confirmed(self.source, TARGET_DAY))
        self.assertEqual(receipt, lookup_cross_day(self.source, 'move-7'))

    def test_ordinary_sequences_still_reclaim(self):
        self.add('a', '青站', 7000, day=OTHER_DAY)
        self.add('b', '河站', 2000, day=OTHER_DAY)
        self.assertEqual(2, reclaim_confirmed(self.source, OTHER_DAY))

    def test_replay_after_both_sides_reclaimed_still_idempotent(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        delivered = advance_cross_day(self.warehouse, self.source, TARGET_DAY, receipt.target_sequence)
        reclaim_confirmed(self.source, SOURCE_DAY)
        reclaim_confirmed(self.source, TARGET_DAY)
        self.assertEqual((), read_changes(self.source, SOURCE_DAY))
        self.assertEqual((), read_changes(self.source, TARGET_DAY))
        self.assertEqual(delivered, advance_cross_day(self.warehouse, self.source,
                                                     SOURCE_DAY, receipt.source_sequence))
        self.assertEqual(receipt, lookup_cross_day(self.source, 'move-7'))
        self.assertEqual((('湾组', 12, 3),), totals(read(self.warehouse, day=TARGET_DAY)))

    def test_missing_reading_moves_as_missing(self):
        data, _ = self.build_sample()
        missing = submit(self.source, 'missing-open', SOURCE_DAY, '石站', 'r1', ml=None)
        batch, = read_changes(self.source, SOURCE_DAY,
                              after=missing.sequence - 1, through=missing.sequence)
        advance(self.warehouse, self.source, batch)
        pending, = read_changes(self.source, TARGET_DAY, after=1, through=2)
        advance(self.warehouse, self.source, pending)
        receipt = submit_cross_day(self.source, 'move-missing', SOURCE_DAY, TARGET_DAY,
                                   '石站', 'r1', expected_revision='missing-open')
        self.assertIsNone(receipt.reading_ml)
        delivered = advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        self.assertEqual('delivered', delivered.status)
        source_live = read(self.warehouse, day=SOURCE_DAY)
        target_live = read(self.warehouse, day=TARGET_DAY)
        self.assertEqual((('湾组', 7, 1),), totals(source_live))
        self.assertEqual(3, len(target_live.rows))
        self.assertEqual((('湾组', 5, 2),), totals(target_live))
        missing_row = [row for row in target_live.rows if row.image.key.station == '石站'][0]
        self.assertIsNone(missing_row.image.reading.value_ml)
        self.assertEqual('r1', missing_row.image.key.reading_id)


class CrossDayStatementTests(CrossDayCase):
    def test_baseline_prepared_after_correction_keeps_both_days(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        # Baselines fixed at the post-correction cut see the moved reading only
        # on the target day and an empty source day.
        source_prepared = prepare(self.warehouse, self.source, SOURCE_DAY, 2, NEW)
        target_prepared = prepare(self.warehouse, self.source, TARGET_DAY, 3, NEW)
        source_rows = self._baseline_rows(source_prepared)
        target_rows = self._baseline_rows(target_prepared)
        self.assertEqual((), source_rows)
        self.assertEqual(3, len(target_rows))
        self.assertIn(('r1', 7000), [(image.key.reading_id, image.reading.value_ml)
                                     for image in target_rows])
        self.step(source_prepared, 'start', job='source-job')
        self.step(target_prepared, 'start', job='target-job')
        self.step(source_prepared, 'scan', job='source-job')
        self.step(target_prepared, 'scan', job='target-job')
        self.assertEqual('activated',
                         self.step(source_prepared, 'activate', job='source-job').status)
        self.assertEqual('activated',
                         self.step(target_prepared, 'activate', job='target-job').status)
        self.assertEqual((), read(self.warehouse, day=SOURCE_DAY).totals)
        self.assertEqual((('北组', 7, 1), ('南组', 5, 2)),
                         totals(read(self.warehouse, day=TARGET_DAY)))
        self.assertEqual(NEW.rule_id, read(self.warehouse, day=SOURCE_DAY).rule_id)

    def _baseline_rows(self, prepared):
        from catalog.baseline import read_baseline
        return read_baseline(self.warehouse, prepared.manifest.baseline_id)[1]

    def test_morning_statement_survives_before_correction(self):
        data, _ = self.build_sample()
        morning = issue(self.warehouse, 'morning', SOURCE_DAY)
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        reclaim_confirmed(self.source, SOURCE_DAY)
        reclaim_confirmed(self.source, TARGET_DAY)
        self.reopen(source=False)
        historical = read(self.warehouse, number='morning')
        self.assertEqual(morning, historical)
        self.assertEqual((('湾组', 7, 1),), totals(historical.record.snapshot))
        rows = lookup_provenance(self.warehouse, 'morning', station='青站')
        self.assertEqual(('source-open', 7000),
                         (rows[0].image.revision, rows[0].image.reading.value_ml))
        # Re-issue by number still returns the original.
        self.assertEqual(morning, issue(self.warehouse, 'morning', SOURCE_DAY))

    def test_two_day_baselines_then_cutover(self):
        data, _ = self.build_sample()
        receipt = self.accept_correction(data)
        self.deliver(2, day=TARGET_DAY)
        source_prepared = prepare(self.warehouse, self.source, SOURCE_DAY, 1, NEW)
        target_prepared = prepare(self.warehouse, self.source, TARGET_DAY, 1, NEW)
        self.step(source_prepared, 'start', job='source-job')
        self.step(target_prepared, 'start', job='target-job')
        self.assertEqual('built', self.step(source_prepared, 'scan', job='source-job').status)
        self.assertEqual('built', self.step(target_prepared, 'scan', job='target-job').status)

        delivered = advance_cross_day(self.warehouse, self.source, SOURCE_DAY, receipt.source_sequence)
        self.assertEqual('delivered', delivered.status)
        reclaim_confirmed(self.source, SOURCE_DAY)
        reclaim_confirmed(self.source, TARGET_DAY)

        self.assertEqual(2, self.step(source_prepared, 'catch_up', job='source-job').covered)
        self.assertEqual(3, self.step(target_prepared, 'catch_up', job='target-job').covered)
        self.assertEqual('activated', self.step(source_prepared, 'activate', job='source-job').status)
        self.assertEqual('activated', self.step(target_prepared, 'activate', job='target-job').status)

        source_live = read(self.warehouse, day=SOURCE_DAY)
        target_live = read(self.warehouse, day=TARGET_DAY)
        self.assertEqual((NEW.rule_id, 2, ()),
                         (source_live.rule_id, source_live.sequence, source_live.totals))
        self.assertEqual((NEW.rule_id, 3, (('北组', 7, 1), ('南组', 5, 2))),
                         (target_live.rule_id, target_live.sequence, totals(target_live)))
        provenance = lookup_provenance(
            self.warehouse, issue(self.warehouse, 'moved', TARGET_DAY).receipt.number,
            station='青站')
        self.assertEqual('source-open', provenance[0].image.revision)


class CrossDaySampleTests(CrossDayCase):
    """The exact numbered walk-through from CROSS_DAY.md on examples/cross_day.json."""

    def test_full_sample_sequence(self):
        data = sample()
        expected = data['expected']
        self.assertEqual([2, 3], [expected['source_sequence'], expected['target_sequence']])

        for opening in data['openings']:
            receipt = submit(self.source, opening['batch_id'], opening['day'],
                             opening['station'], opening['reading_id'], ml=opening['ml'])
            batch, = read_changes(self.source, opening['day'],
                                  after=receipt.sequence - 1, through=receipt.sequence)
            advance(self.warehouse, self.source, batch)
        before = [read(self.warehouse, day=day).totals[0].litres
                  for day in (SOURCE_DAY, TARGET_DAY)]
        self.assertEqual([ml // 1000 for ml in expected['before_ml']], before)

        pending = data['pending_target']
        submit(self.source, pending['batch_id'], pending['day'],
               pending['station'], pending['reading_id'], ml=pending['ml'])

        correction = data['correction']
        receipt = submit_cross_day(self.source, correction['correction_id'],
                                   correction['source_day'], correction['target_day'],
                                   correction['station'], correction['reading_id'],
                                   expected_revision=correction['expected_revision'])
        self.assertEqual(SOURCE_DAY, receipt.source_day)
        self.assertEqual(expected['source_sequence'], receipt.source_sequence)
        self.assertEqual(TARGET_DAY, receipt.target_day)
        self.assertEqual(expected['target_sequence'], receipt.target_sequence)
        # Step 5: both sides wait.
        for day, sequence in ((SOURCE_DAY, receipt.source_sequence),
                              (TARGET_DAY, receipt.target_sequence)):
            self.assertEqual('waiting',
                             advance_cross_day(self.warehouse, self.source, day, sequence).status)
        self.assertEqual([ml // 1000 for ml in expected['before_ml']],
                         [read(self.warehouse, day=d).totals[0].litres if read(self.warehouse, day=d).totals
                          else 0 for d in (SOURCE_DAY, TARGET_DAY)])

        pending_batch, = read_changes(self.source, TARGET_DAY, after=1, through=2)
        advance(self.warehouse, self.source, pending_batch)
        delivered = advance_cross_day(self.warehouse, self.source, TARGET_DAY, receipt.target_sequence)
        self.assertEqual('delivered', delivered.status)
        after = [read(self.warehouse, day=d).totals[0].litres if read(self.warehouse, day=d).totals else 0
                 for d in (SOURCE_DAY, TARGET_DAY)]
        self.assertEqual([ml // 1000 for ml in expected['after_ml']], after)
        target_live = read(self.warehouse, day=TARGET_DAY)
        self.assertEqual(expected['target_rows_after'], len(target_live.rows))
        self.assertEqual(receipt, lookup_cross_day(self.source, 'move-7'))
        self.assertEqual(delivered, advance_cross_day(self.warehouse, self.source,
                                                     SOURCE_DAY, receipt.source_sequence))
