from dataclasses import replace

from catalog.baseline import read_baseline_page
from catalog.provenance import BaselineIntegrityError, baseline_digest, verify_source_slice
from daily.contribution import adjust_totals, as_view_row, image_contribution


def finish_baseline_scan(checkpoint, manifest):
    images = tuple(row.image for row in checkpoint.rows)
    verify_source_slice(manifest.day, manifest.cut, images)
    if len(images) != manifest.row_count or baseline_digest(images) != manifest.digest:
        raise BaselineIntegrityError('candidate scan differs from the fixed manifest')
    return replace(checkpoint, covered=manifest.cut)


def scan_baseline_page(db, checkpoint, manifest, rule, limit):
    if checkpoint.covered is not None:
        return checkpoint
    images = read_baseline_page(db, manifest.baseline_id, after=checkpoint.cursor, limit=limit)
    verify_source_slice(manifest.day, manifest.cut, images)
    rows = tuple(as_view_row(image, rule) for image in images)
    scanned = checkpoint.scanned_rows + len(rows)
    if scanned > manifest.row_count or (len(rows) < limit and scanned < manifest.row_count):
        raise BaselineIntegrityError('baseline page count differs from the fixed manifest')

    checkpoint = replace(checkpoint,
                         cursor=checkpoint.cursor if not images else images[-1].ordinal,
                         scanned_rows=scanned, rows=checkpoint.rows + rows,
                         totals=adjust_totals(checkpoint.totals, map(image_contribution, rows)))
    if scanned == manifest.row_count:
        checkpoint = finish_baseline_scan(checkpoint, manifest)
    return checkpoint
