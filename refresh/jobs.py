from dataclasses import dataclass

from catalog.manifest import has_prepared_rule, load_manifest
from catalog.rules import load_rule_version
from refresh.activate import activate_candidate
from refresh.build import scan_baseline_page
from refresh.catchup import catch_up_candidate
from refresh.checkpoint import CandidateCheckpoint, load_candidate_checkpoint, store_candidate_checkpoint
from storage import day_id, identifier, index, transaction


class JobConflict(ValueError):
    pass


@dataclass(frozen=True)
class RefreshJob:
    job_id: str
    day: str
    baseline_id: str
    cut: int
    rule_id: str
    activated: bool


@dataclass(frozen=True)
class RefreshResult:
    job: RefreshJob
    status: str
    scanned_rows: int
    covered: int | None


def ensure_job_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS refresh_jobs (
        job_id TEXT PRIMARY KEY, day TEXT NOT NULL, baseline_id TEXT NOT NULL REFERENCES baselines(baseline_id),
        cut INTEGER NOT NULL, rule_id TEXT NOT NULL REFERENCES rule_versions(rule_id),
        activated INTEGER NOT NULL CHECK(activated IN (0, 1))
    )''')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_open_refresh ON refresh_jobs(day) WHERE activated = 0')


def load_refresh_job(db, job_id):
    identifier(job_id, 'refresh job id')
    row = db.execute('SELECT * FROM refresh_jobs WHERE job_id = ?', (job_id,)).fetchone()
    return None if row is None else RefreshJob(*row[:5], bool(row[5]))


def _start_job(db, job_id, manifest, rule_id):
    occupied = db.execute('SELECT job_id FROM refresh_jobs WHERE day = ? AND activated = 0',
                          (manifest.day,)).fetchone()
    if occupied is not None:
        raise JobConflict(f'{manifest.day} already has an open refresh')
    job = RefreshJob(job_id, manifest.day, manifest.baseline_id, manifest.cut, rule_id, False)
    db.execute('INSERT INTO refresh_jobs VALUES (?, ?, ?, ?, ?, 0)',
               (job_id, job.day, job.baseline_id, job.cut, rule_id))
    store_candidate_checkpoint(db, CandidateCheckpoint(job_id, job.day, job.baseline_id, rule_id))
    return job


def refresh_step(db, job_id, day, baseline_id, rule_id, *, action, limit=100):
    identifier(job_id, 'refresh job id')
    day_id(day)
    identifier(baseline_id, 'baseline id')
    identifier(rule_id, 'rule id')
    index(limit, 'refresh step size', minimum=1)
    if action not in ('start', 'scan', 'catch_up', 'activate'):
        raise ValueError('unknown refresh action')

    with transaction(db, write=True):
        manifest = load_manifest(db, baseline_id)
        if manifest.day != day or not has_prepared_rule(db, baseline_id, rule_id):
            raise JobConflict('baseline and rule have not been prepared for this day')
        job = load_refresh_job(db, job_id)
        if job is None:
            if action != 'start':
                raise KeyError(f'unknown refresh job {job_id}')
            job = _start_job(db, job_id, manifest, rule_id)
        if (job.day, job.baseline_id, job.cut, job.rule_id) != (day, baseline_id, manifest.cut, rule_id):
            raise JobConflict('refresh job id already has a different binding')
        checkpoint = load_candidate_checkpoint(db, job_id)
        if checkpoint.binding != (day, baseline_id, rule_id):
            raise JobConflict('candidate checkpoint has a different binding')
        if job.activated:
            return RefreshResult(job, 'activated', checkpoint.scanned_rows, checkpoint.covered)

        if action == 'activate':
            if not activate_candidate(db, checkpoint, manifest, rule_id):
                return RefreshResult(job, 'waiting', checkpoint.scanned_rows, checkpoint.covered)
            db.execute('UPDATE refresh_jobs SET activated = 1 WHERE job_id = ?', (job_id,))
            activated = load_refresh_job(db, job_id)
            return RefreshResult(activated, 'activated', checkpoint.scanned_rows, checkpoint.covered)

        if action in ('scan', 'catch_up'):
            rule = load_rule_version(db, rule_id)
            if action == 'scan':
                updated = scan_baseline_page(db, checkpoint, manifest, rule, limit)
            else:
                updated = catch_up_candidate(db, checkpoint, rule, limit)
            if updated != checkpoint:
                store_candidate_checkpoint(db, updated)
            checkpoint = updated
        status = 'scanning' if checkpoint.covered is None else 'built'
        return RefreshResult(job, status, checkpoint.scanned_rows, checkpoint.covered)
