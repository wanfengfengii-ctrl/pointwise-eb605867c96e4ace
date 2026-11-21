import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from catalog.baseline import prepare
from catalog.bootstrap import initialize_source, initialize_warehouse
from catalog.rules import RuleVersion
from daily.receive import advance
from handoff.audit import verify_statement_totals
from handoff.issue import issue
from handoff.readback import read
from refresh.jobs import refresh_step
from source.feed import read_changes, reclaim_confirmed, source_frontiers
from source.intake import submit
from storage import connect


def _scenario_data(name):
    path = Path(__file__).with_name(name)
    return json.loads(path.read_text(encoding='utf-8'))


def _submit_and_publish(warehouse, source, day, request):
    receipt = submit(source, day=day, **request)
    batch, = read_changes(source, day, after=receipt.sequence - 1, through=receipt.sequence)
    return advance(warehouse, source, batch)


def _view(snapshot):
    return {
        'rule': snapshot.rule_id, 'sequence': snapshot.sequence,
        'groups': [[row.region, row.litres, row.valid_rows] for row in snapshot.totals],
    }


def run_demo(root, samples, initial, replacement):
    day = samples['day']
    warehouse_path = root / 'warehouse.sqlite'
    source_path = root / 'source.sqlite'
    with closing(connect(warehouse_path)) as warehouse, closing(connect(source_path)) as source:
        initialize_source(source)
        initialize_warehouse(warehouse, initial)
        opening = _submit_and_publish(warehouse, source, day, samples['opening'])
        original = issue(warehouse, 'morning', day)
        prepared = prepare(warehouse, source, day, opening.sequence, replacement)
        binding = ('afternoon', day, prepared.manifest.baseline_id, prepared.rule_id)
        assert refresh_step(warehouse, *binding, action='start').status == 'scanning'
        for request in samples['during_scan']:
            _submit_and_publish(warehouse, source, day, request)
        before = _view(read(warehouse, day=day))
        assert before == {'rule': 'old', 'sequence': 3, 'groups': [['湾组', 14, 2]]}, before
        assert source_frontiers(source, day) == (3, 3)
        reclaimed = reclaim_confirmed(source, day)
        assert reclaimed == 3
        assert read_changes(source, day) == ()

    with closing(connect(warehouse_path)) as warehouse:
        refresh_step(warehouse, *binding, action='scan', limit=1)
        still_waiting = refresh_step(warehouse, *binding, action='activate')
        assert still_waiting.status == 'waiting' and _view(read(warehouse, day=day)) == before
        for sequence in (2, 3):
            progress = refresh_step(warehouse, *binding, action='catch_up', limit=1)
            assert progress.covered == sequence
        assert refresh_step(warehouse, *binding, action='activate').status == 'activated'
        actual = _view(read(warehouse, day=day))
        expected = {'rule': 'new', 'sequence': 3, 'groups': [['北组', 11, 1], ['南组', 3, 1]]}
        assert actual == expected, ('QINGJIA_CUTOVER_CONTINUITY', actual)
        historical = read(warehouse, number='morning')
        assert historical == original and verify_statement_totals(historical.record)
        downstream = _view(historical.record.snapshot)
        downstream_expected = {'rule': 'old', 'sequence': 1, 'groups': [['湾组', 7, 1]]}
        assert downstream == downstream_expected
        afternoon = issue(warehouse, 'afternoon', day)
        assert _view(afternoon.record.snapshot) == actual and verify_statement_totals(afternoon.record)

    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'n6', 'input': {**samples, 'reclaimed': reclaimed},
        'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream,
        'code_paths': ['source/feed.py', 'daily/receive.py', 'refresh/jobs.py', 'refresh/activate.py', 'handoff/readback.py'],
        'oracle': '切点青站 7 L，依序减 7 加 11 以及加河站 3，旧定稿仍为 7 L',
    }, ensure_ascii=False, sort_keys=True))


def main():
    samples = _scenario_data('batches.json')
    rules = _scenario_data('rules.json')
    with TemporaryDirectory(prefix='qingjia-ledger-') as directory:
        run_demo(Path(directory), samples, RuleVersion(**rules['initial']), RuleVersion(**rules['replacement']))


if __name__ == '__main__':
    main()
