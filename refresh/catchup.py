from dataclasses import replace

from catalog.manifest import load_retained_changes
from daily.contribution import adjust_totals, as_view_row, batch_delta
from daily.progress import read_published_head
from daily.receive import lookup_delivery


class MissingChangeMaterial(ValueError):
    pass


def apply_candidate_batch(checkpoint, batch, rule):
    if checkpoint.covered is None or batch.day != checkpoint.day or batch.sequence != checkpoint.covered + 1:
        raise MissingChangeMaterial('change does not extend the candidate prefix')
    image = batch.after if batch.after is not None else batch.before
    if image is None or image.key.day != checkpoint.day:
        raise MissingChangeMaterial('change is missing a valid row image')
    rows = {row.image.key: row for row in checkpoint.rows}
    expected = None if batch.before is None else as_view_row(batch.before, rule)
    if rows.get(image.key) != expected:
        raise MissingChangeMaterial('change before image differs from the candidate row')
    if batch.after is None:
        del rows[image.key]
    else:
        rows[image.key] = as_view_row(batch.after, rule)
    return replace(checkpoint, covered=batch.sequence,
                   rows=tuple(sorted(rows.values(), key=lambda row: row.image.ordinal)),
                   totals=adjust_totals(checkpoint.totals, batch_delta(batch, rule)))


def catch_up_candidate(db, checkpoint, rule, limit):
    head = read_published_head(db, checkpoint.day)
    if checkpoint.covered is None or head is None or head.sequence <= checkpoint.covered:
        return checkpoint
    first = checkpoint.covered + 1
    target = min(head.sequence, checkpoint.covered + limit)
    batches = iter(load_retained_changes(db, checkpoint.day, after=checkpoint.covered, through=target))
    for sequence in range(first, target + 1):
        batch = next(batches, None)
        receipt = lookup_delivery(db, checkpoint.day, sequence)
        if (batch is None or batch.day != checkpoint.day or batch.sequence != sequence
                or receipt is None or batch.fingerprint != receipt.fingerprint):
            if sequence == first:
                raise MissingChangeMaterial(f'no verified change for sequence {sequence}')
            break
        checkpoint = apply_candidate_batch(checkpoint, batch, rule)
    return checkpoint
