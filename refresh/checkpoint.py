import json
from dataclasses import asdict, dataclass

from daily.contribution import Contribution, ViewRow
from storage import canonical


@dataclass(frozen=True)
class CandidateCheckpoint:
    job_id: str
    day: str
    baseline_id: str
    rule_id: str
    cursor: int = 0
    scanned_rows: int = 0
    covered: int | None = None
    rows: tuple[ViewRow, ...] = ()
    totals: tuple[Contribution, ...] = ()

    @property
    def binding(self):
        return self.day, self.baseline_id, self.rule_id


def ensure_checkpoint_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS candidate_checkpoints (
        job_id TEXT PRIMARY KEY REFERENCES refresh_jobs(job_id), day TEXT NOT NULL,
        baseline_id TEXT NOT NULL REFERENCES baselines(baseline_id),
        rule_id TEXT NOT NULL REFERENCES rule_versions(rule_id), cursor INTEGER NOT NULL,
        scanned_rows INTEGER NOT NULL, covered INTEGER, rows TEXT NOT NULL, totals TEXT NOT NULL
    )''')


def load_candidate_checkpoint(db, job_id):
    row = db.execute('SELECT * FROM candidate_checkpoints WHERE job_id = ?', (job_id,)).fetchone()
    if row is None:
        raise KeyError(f'unknown candidate checkpoint {job_id}')
    rows = tuple(ViewRow.from_data(item) for item in json.loads(row[7]))
    totals = tuple(Contribution(**item) for item in json.loads(row[8]))
    return CandidateCheckpoint(*row[:7], rows, totals)


def store_candidate_checkpoint(db, checkpoint):
    db.execute('''INSERT INTO candidate_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET cursor = excluded.cursor,
        scanned_rows = excluded.scanned_rows, covered = excluded.covered,
        rows = excluded.rows, totals = excluded.totals''', (
        checkpoint.job_id, *checkpoint.binding, checkpoint.cursor, checkpoint.scanned_rows,
        checkpoint.covered, canonical([row.to_data() for row in checkpoint.rows]),
        canonical([asdict(total) for total in checkpoint.totals]),
    ))
