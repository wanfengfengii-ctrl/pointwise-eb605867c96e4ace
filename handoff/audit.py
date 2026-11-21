from daily.contribution import adjust_totals, image_contribution
from handoff.archive import load_provenance_rows
from storage import identifier, read_scope


def build_provenance_index(snapshot):
    return tuple(sorted(snapshot.rows, key=lambda row: (
        row.region, row.image.ordinal, row.image.key.station, row.image.key.reading_id,
    )))


def lookup_provenance(db, number, *, region=None, station=None):
    identifier(number, 'statement number')
    if region is not None:
        identifier(region, 'region')
    if station is not None:
        identifier(station, 'station')
    with read_scope(db):
        if db.execute('SELECT 1 FROM statements WHERE number = ?', (number,)).fetchone() is None:
            raise KeyError(f'unknown statement {number}')
        return load_provenance_rows(db, number, region=region, station=station)


def verify_statement_totals(record):
    snapshot = record.snapshot
    keys = {row.image.key for row in snapshot.rows}
    if len(keys) != len(snapshot.rows) or any(key.day != snapshot.day for key in keys):
        return False
    calculated = adjust_totals((), map(image_contribution, snapshot.rows))
    return calculated == snapshot.totals
