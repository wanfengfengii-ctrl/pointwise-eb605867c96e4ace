from daily.balance import replace_current_balance
from daily.progress import PublishedHead, publish_progress, read_published_head


def activate_candidate(db, checkpoint, manifest, rule_id):
    if not db.in_transaction:
        raise RuntimeError('activation requires the warehouse write transaction')
    if checkpoint.binding != (manifest.day, manifest.baseline_id, rule_id):
        raise ValueError('candidate is bound to a different publication')
    if (checkpoint.covered is None or checkpoint.scanned_rows != manifest.row_count
            or checkpoint.covered < manifest.cut):
        return False

    current = read_published_head(db, manifest.day)
    published = 0 if current is None else current.sequence
    if checkpoint.covered != published:
        return False

    confirmable = 0 if current is None else current.confirmable
    replace_current_balance(db, manifest.day, checkpoint.rows, checkpoint.totals)
    publish_progress(db, PublishedHead(manifest.day, rule_id, checkpoint.covered, confirmable))
    return True
