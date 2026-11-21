import sqlite3
from contextlib import closing

from catalog.baseline import prepare, read_baseline
from catalog.manifest import find_baseline
from catalog.provenance import BaselineIntegrityError
from catalog.rules import RuleConflict, RuleVersion, UnmappedStation, load_rule_version
from daily.receive import advance
from handoff.issue import issue
from handoff.readback import LedgerNotFound, read
from refresh.jobs import JobConflict, load_refresh_job, refresh_step
from source.feed import read_changes, reclaim_confirmed, source_frontiers
from source.intake import submit
from storage import connect
from tests.support import DAY, NEW, NEXT_DAY, OLD, LedgerCase, deny_next_commit, report, totals


class RefreshTests(LedgerCase):
    def test_quiet_activation(self):
        self.add('a', '青站', 7000)
        self.add('b', '河站', 11000)
        old = issue(self.warehouse, 'old', DAY)
        self.assertEqual((('湾组', 18, 2),), totals(old.record.snapshot))
        prepared = self.prepare(2)
        self.step(prepared, 'start')
        self.assertEqual('waiting', self.step(prepared, 'activate').status)
        built = self.step(prepared, 'scan')
        self.assertEqual((2, 2), (built.scanned_rows, built.covered))
        activated = self.step(prepared, 'activate')
        self.assertEqual('activated', activated.status)
        live = read(self.warehouse, day=DAY)
        self.assertEqual(('new', 2, (('北组', 7, 1), ('南组', 11, 1))),
                         (live.rule_id, live.sequence, totals(live)))
        self.assertEqual((('北组', '青站'), ('南组', '河站')),
                         tuple((row.region, row.image.key.station) for row in live.rows))
        self.assertEqual(old, read(self.warehouse, number='old'))
        self.assertEqual(activated, self.step(prepared, 'start'))

    def test_fixed_cut_read(self):
        self.add('a', '青站', 7000)
        submit(self.source, 'b', DAY, '青站', 'r1', operation='correct', expected_revision='a', ml=11000)
        prepared = self.prepare(1)
        self.deliver(2)
        self.assertEqual(2, reclaim_confirmed(self.source, DAY))
        self.reopen(source=False)
        manifest, images = read_baseline(self.warehouse, prepared.manifest.baseline_id)
        self.assertEqual(prepared.manifest, manifest)
        self.assertEqual(((1, 'a', 7000),),
                         tuple((image.ordinal, image.revision, image.reading.value_ml) for image in images))
        self.assertEqual(prepared, prepare(self.warehouse, None, DAY, 1, NEW))
        self.step(prepared, 'start')
        self.assertEqual(1, self.step(prepared, 'scan').covered)
        self.assertEqual('waiting', self.step(prepared, 'activate').status)
        self.assertEqual(2, self.step(prepared, 'catch_up').covered)
        self.step(prepared, 'activate')
        self.assertEqual((('北组', 11, 1),), totals(read(self.warehouse, day=DAY)))

    def test_invalid_prepare(self):
        self.add('a', '青站', 7000)
        with self.assertRaises(BaselineIntegrityError):
            self.prepare(2)
        self.assertIsNone(find_baseline(self.warehouse, DAY, 2))
        with self.assertRaises(UnmappedStation):
            self.prepare(1, rule=RuleVersion('incomplete', {'河站': '南组'}))
        self.assertIsNone(find_baseline(self.warehouse, DAY, 1))
        with self.assertRaises(KeyError):
            load_rule_version(self.warehouse, 'incomplete')
        prepared = self.prepare(1)
        with self.assertRaises(RuleConflict):
            self.prepare(1, rule=RuleVersion('new', {'青站': '南组'}))
        self.assertEqual(prepared, self.prepare(1))
        reclaim_confirmed(self.source, DAY)
        with self.assertRaises(BaselineIntegrityError):
            self.prepare(0)
        self.assertIsNone(find_baseline(self.warehouse, DAY, 0))
        self.assertEqual(('old', 1), (read(self.warehouse, day=DAY).rule_id, read(self.warehouse, day=DAY).sequence))

    def test_job_binding(self):
        self.add('a', '青站', 7000)
        prepared = self.prepare(1)
        start = self.step(prepared, 'start')
        self.assertEqual(start, self.step(prepared, 'start'))
        with self.assertRaises(JobConflict):
            self.step(prepared, 'start', job='another')
        with self.assertRaises(JobConflict):
            refresh_step(self.warehouse, 'job-1', NEXT_DAY, prepared.manifest.baseline_id, NEW.rule_id, action='scan')
        with self.assertRaises(JobConflict):
            refresh_step(self.warehouse, 'job-1', DAY, prepared.manifest.baseline_id, OLD.rule_id, action='scan')
        alternate = self.prepare(1, rule=RuleVersion('alternate', {'青站': '山组'}))
        with self.assertRaises(JobConflict):
            self.step(alternate, 'scan')
        self.assertEqual(start.job, load_refresh_job(self.warehouse, 'job-1'))

    def test_partial_scan_restart(self):
        self.add('a', '青站', 7000)
        self.add('b', '河站', 11000)
        prepared = self.prepare(2)
        self.step(prepared, 'start')
        part = self.step(prepared, 'scan', limit=1)
        self.assertEqual(('scanning', 1, None), (part.status, part.scanned_rows, part.covered))
        with deny_next_commit(self.warehouse), self.assertRaises(sqlite3.DatabaseError):
            self.step(prepared, 'scan', limit=1)
        self.reopen()
        self.assertEqual(part, self.step(prepared, 'start'))
        extra = self.add('c', '石站', 3000, deliver=False)
        batch, = read_changes(self.source, DAY, after=extra.sequence - 1)
        with closing(connect(self.warehouse_path)) as receiver:
            advance(receiver, self.source, batch)
        self.assertEqual((('湾组', 21, 3),), totals(read(self.warehouse, day=DAY)))
        built = self.step(prepared, 'scan', limit=1)
        self.assertEqual(('built', 2, 2), (built.status, built.scanned_rows, built.covered))
        self.assertEqual(3, self.step(prepared, 'catch_up', limit=1).covered)
        self.step(prepared, 'activate')
        self.assertEqual((('北组', 7, 1), ('南组', 14, 2)), totals(read(self.warehouse, day=DAY)))

    def _open_refresh_with_reclaimed_feed(self):
        self.add('base', '青站', 7000)
        original = issue(self.warehouse, 'morning', DAY)
        prepared = self.prepare(1)
        self.assertEqual('scanning', self.step(prepared, 'start').status)
        submit(self.source, 'correct', DAY, '青站', 'r1', operation='correct', expected_revision='base', ml=11000)
        self.add('late', '河站', 3000, deliver=False)
        correction, arrival = read_changes(self.source, DAY, after=1)
        receipt = advance(self.warehouse, self.source, correction)
        advance(self.warehouse, self.source, arrival)
        live = read(self.warehouse, day=DAY)
        self.assertEqual(('old', 3, (('湾组', 14, 2),)), (live.rule_id, live.sequence, totals(live)))
        self.assertEqual((3, 3), source_frontiers(self.source, DAY))
        self.assertEqual(3, reclaim_confirmed(self.source, DAY))
        self.assertEqual((), read_changes(self.source, DAY))
        self.assertEqual(receipt, advance(self.warehouse, self.source, correction))
        self.assertEqual(live, read(self.warehouse, day=DAY))
        return prepared, original, live

    def test_reclaimed_batches(self):
        prepared, original, live = self._open_refresh_with_reclaimed_feed()
        self.reopen(source=False)
        self.assertEqual(live, read(self.warehouse, day=DAY))
        self.step(prepared, 'scan')
        waiting = self.step(prepared, 'activate')
        after_request = read(self.warehouse, day=DAY)
        self.assertEqual(('waiting', live), (waiting.status, after_request), 'QINGJIA_CUTOVER_CONTINUITY')
        self.assertEqual(2, self.step(prepared, 'catch_up', limit=1).covered)
        self.assertEqual(3, self.step(prepared, 'catch_up', limit=1).covered)
        self.assertEqual('activated', self.step(prepared, 'activate').status)
        result = read(self.warehouse, day=DAY)
        expected = ('new', 3, (('北组', 11, 1), ('南组', 3, 1)))
        actual = (result.rule_id, result.sequence, totals(result))
        self.assertEqual(expected, actual, 'QINGJIA_CUTOVER_CONTINUITY')
        self.assertEqual(('correct', 'late'), tuple(row.image.revision for row in result.rows))
        historical = read(self.warehouse, number='morning')
        self.assertEqual(original, historical)
        old_expected = ('old', 1, (('湾组', 7, 1),), 'morning')
        old_actual = (historical.receipt.rule_id, historical.receipt.sequence,
                      totals(historical.record.snapshot), historical.receipt.number)
        self.assertEqual(old_expected, old_actual)
        report('n6', {'cut': 1, 'ml': [7000, 11000, 3000], 'source_feed_after_reclaim': []},
               expected, actual, old_expected, old_actual,
               ['daily/receive.py', 'refresh/catchup.py', 'refresh/activate.py', 'handoff/readback.py'],
               '北组撤回 7 再加入 11，南组新增 3，切点后两个实际序号都兑现')

    def test_failed_activation(self):
        prepared, original, live = self._open_refresh_with_reclaimed_feed()
        self.reopen(source=False)
        self.step(prepared, 'scan')
        self.step(prepared, 'catch_up')
        with deny_next_commit(self.warehouse), self.assertRaises(sqlite3.DatabaseError):
            self.step(prepared, 'activate')
        self.reopen(source=False)
        self.assertEqual(live, read(self.warehouse, day=DAY))
        self.assertEqual(original, read(self.warehouse, number='morning'))
        self.assertFalse(load_refresh_job(self.warehouse, 'job-1').activated)
        self.assertEqual(3, self.step(prepared, 'start').covered)
        self.assertEqual('activated', self.step(prepared, 'activate').status)
        final = read(self.warehouse, day=DAY)
        self.assertEqual(('new', 3, (('北组', 11, 1), ('南组', 3, 1))),
                         (final.rule_id, final.sequence, totals(final)))
        self.assertEqual(original, read(self.warehouse, number='morning'))

    def test_live_advance_before_activation(self):
        prepared, original, _ = self._open_refresh_with_reclaimed_feed()
        self.step(prepared, 'scan')
        self.assertEqual(3, self.step(prepared, 'catch_up').covered)
        submit(self.source, 'withdraw', DAY, '河站', 'r1', operation='withdraw', expected_revision='late')
        batch, = read_changes(self.source, DAY)
        with closing(connect(self.warehouse_path)) as receiver:
            advance(receiver, self.source, batch)
        reached = read(self.warehouse, day=DAY)
        self.assertEqual(('old', 4, (('湾组', 11, 1),)), (reached.rule_id, reached.sequence, totals(reached)))
        self.assertEqual(1, reclaim_confirmed(self.source, DAY))
        self.assertEqual('waiting', self.step(prepared, 'activate').status)
        self.assertEqual(reached, read(self.warehouse, day=DAY))
        self.assertEqual(4, self.step(prepared, 'catch_up').covered)
        self.assertEqual('activated', self.step(prepared, 'activate').status)
        rolled = read(self.warehouse, day=DAY)
        self.assertEqual(('new', 4, (('北组', 11, 1),)), (rolled.rule_id, rolled.sequence, totals(rolled)))
        self.assertEqual(('青站',), tuple(row.image.key.station for row in rolled.rows))

        self.add('next', '河站', 5000, reading_id='r2')
        final = read(self.warehouse, day=DAY)
        numbers = (reached.sequence, rolled.sequence, final.sequence)
        self.assertEqual((4, 4, 5), numbers)
        expected = ('new', 5, (('北组', 11, 1), ('南组', 5, 1)))
        actual = (final.rule_id, final.sequence, totals(final))
        self.assertEqual(expected, actual)
        self.assertEqual(original, read(self.warehouse, number='morning'))
        report('n9', {'cut': 1, 'covered_before_withdrawal': 3, 'withdrawn_ml': 3000, 'next_ml': 5000},
               (4, 4, 5), numbers, expected, actual,
               ['daily/progress.py', 'daily/receive.py', 'refresh/activate.py'],
               '撤销序号 4 必须先兑现，之后序号 5 的河站 5 L 只能归入南组')

    def test_future_source_cut_waits_for_publication(self):
        self.add('a', '青站', 7000, deliver=False)
        prepared = self.prepare(1)
        self.step(prepared, 'start')
        self.assertEqual(1, self.step(prepared, 'scan').covered)
        self.assertEqual('waiting', self.step(prepared, 'activate').status)
        with self.assertRaises(LedgerNotFound):
            read(self.warehouse, day=DAY)
        self.deliver(1)
        self.assertEqual('activated', self.step(prepared, 'activate').status)
        self.assertEqual((('北组', 7, 1),), totals(read(self.warehouse, day=DAY)))
