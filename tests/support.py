import json
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from catalog.baseline import prepare
from catalog.bootstrap import initialize_source, initialize_warehouse
from catalog.rules import RuleVersion
from daily.receive import advance
from refresh.jobs import refresh_step
from source.feed import read_changes
from source.intake import submit
from storage import connect


DAY = '2026-11-12'
NEXT_DAY = '2026-11-13'
OLD = RuleVersion('old', {'青站': '湾组', '河站': '湾组', '石站': '湾组'})
NEW = RuleVersion('new', {'青站': '北组', '河站': '南组', '石站': '南组'})


def totals(snapshot):
    return tuple((area.region, area.litres, area.valid_rows) for area in snapshot.totals)


def report(case_id, inputs, expected, actual, downstream_expected, downstream_actual, code_paths, oracle):
    payload = dict(case_id=case_id, input=inputs, expected=expected, actual=actual,
                   downstream_expected=downstream_expected, downstream_actual=downstream_actual,
                   code_paths=code_paths, oracle=oracle)
    print('POINTWISE_SCENARIO ' + json.dumps(payload, ensure_ascii=False, sort_keys=True))


@contextmanager
def deny_next_commit(db):
    denied = False

    def authorize(action, operation, *_):
        nonlocal denied
        if not denied and (action, operation) == (sqlite3.SQLITE_TRANSACTION, 'COMMIT'):
            denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorize)
    try:
        yield
    finally:
        db.set_authorizer(None)
    if not denied:
        raise AssertionError('expected a real SQLite COMMIT attempt')


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.source_path = root / 'source.sqlite'
        self.warehouse_path = root / 'warehouse.sqlite'
        self.source = connect(self.source_path)
        self.warehouse = connect(self.warehouse_path)
        self.addCleanup(self.close_connections)
        initialize_source(self.source)
        initialize_warehouse(self.warehouse, OLD)

    def close_connections(self):
        if self.source is not None:
            self.source.close()
            self.source = None
        if self.warehouse is not None:
            self.warehouse.close()
            self.warehouse = None

    def reopen(self, *, source=True):
        self.close_connections()
        self.warehouse = connect(self.warehouse_path)
        if source:
            self.source = connect(self.source_path)

    def add(self, batch_id, station, ml, *, reading_id='r1', day=DAY, deliver=True):
        receipt = submit(self.source, batch_id, day, station, reading_id, ml=ml)
        if deliver:
            self.deliver(receipt.sequence, day=day)
        return receipt

    def deliver(self, sequence, *, day=DAY):
        batch, = read_changes(self.source, day, after=sequence - 1, through=sequence)
        return advance(self.warehouse, self.source, batch)

    def prepare(self, cut, *, day=DAY, rule=NEW):
        return prepare(self.warehouse, self.source, day, cut, rule)

    def step(self, prepared, action, *, job='job-1', limit=100):
        manifest = prepared.manifest
        return refresh_step(self.warehouse, job, manifest.day, manifest.baseline_id,
                            prepared.rule_id, action=action, limit=limit)
