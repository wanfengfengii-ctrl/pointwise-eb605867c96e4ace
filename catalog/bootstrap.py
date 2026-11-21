from catalog.manifest import ensure_manifest_schema
from catalog.rules import ensure_rule_schema, register_rule_version
from daily.balance import ensure_balance_schema
from daily.progress import ensure_progress_schema
from daily.receive import ensure_delivery_schema
from handoff.archive import ensure_statement_schema
from handoff.receipts import ensure_receipt_schema
from refresh.checkpoint import ensure_checkpoint_schema
from refresh.jobs import ensure_job_schema
from source.cross_day import ensure_cross_day_schema
from source.feed import ensure_feed_schema
from source.intake import ensure_submission_schema
from source.rows import ensure_row_schema
from storage import transaction


def initialize_source(db):
    with transaction(db, write=True):
        ensure_feed_schema(db)
        ensure_row_schema(db)
        ensure_submission_schema(db)
        ensure_cross_day_schema(db)


def initialize_warehouse(db, initial_rule):
    with transaction(db, write=True):
        ensure_rule_schema(db)
        ensure_manifest_schema(db)
        ensure_progress_schema(db)
        ensure_delivery_schema(db)
        ensure_balance_schema(db)
        ensure_job_schema(db)
        ensure_checkpoint_schema(db)
        ensure_statement_schema(db)
        ensure_receipt_schema(db)

        initialized = db.execute('SELECT 1 FROM warehouse_settings WHERE slot = 1').fetchone()
        if initialized is None:
            register_rule_version(db, initial_rule)
            db.execute('INSERT INTO warehouse_settings VALUES (1, ?)', (initial_rule.rule_id,))
