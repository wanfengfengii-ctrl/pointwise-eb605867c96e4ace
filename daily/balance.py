import json

from daily.contribution import Contribution, ViewRow, as_view_row, batch_delta, merge_total
from source.rows import ObservationImage
from storage import canonical


def ensure_balance_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS current_rows (
        day TEXT NOT NULL, station TEXT NOT NULL, reading_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL, region TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(day, station, reading_id)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS current_regions (
        day TEXT NOT NULL, region TEXT NOT NULL, litres TEXT NOT NULL,
        valid_rows INTEGER NOT NULL, row_count INTEGER NOT NULL, PRIMARY KEY(day, region)
    )''')


def _decode_total(row):
    region, litres, valid_rows, row_count = row
    return Contribution(region, int(litres), valid_rows, row_count)


def _load_total(db, day, region):
    row = db.execute('''SELECT region, litres, valid_rows, row_count FROM current_regions
        WHERE day = ? AND region = ?''', (day, region)).fetchone()
    return None if row is None else _decode_total(row)


def _store_total(db, day, total):
    if total.row_count == 0:
        db.execute('DELETE FROM current_regions WHERE day = ? AND region = ?', (day, total.region))
        return
    db.execute('''INSERT INTO current_regions VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(day, region) DO UPDATE SET litres = excluded.litres,
        valid_rows = excluded.valid_rows, row_count = excluded.row_count''',
        (day, total.region, str(total.litres), total.valid_rows, total.row_count))


def _put_row(db, row):
    image = row.image
    db.execute('''INSERT INTO current_rows VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(day, station, reading_id) DO UPDATE SET
        ordinal = excluded.ordinal, region = excluded.region, payload = excluded.payload''',
        (*image.key.columns(), image.ordinal, row.region, canonical(image.to_data())))


def _decode_view_row(region, payload):
    return ViewRow(region, ObservationImage.from_data(json.loads(payload)))


def apply_current_batch(db, batch, rule):
    image = batch.after if batch.after is not None else batch.before
    key = image.key
    row = db.execute('''SELECT region, payload FROM current_rows
        WHERE day = ? AND station = ? AND reading_id = ?''', key.columns()).fetchone()
    stored = None if row is None else _decode_view_row(*row)
    expected = None if batch.before is None else as_view_row(batch.before, rule)
    if stored != expected:
        raise ValueError('before image differs from the published row')

    for delta in batch_delta(batch, rule):
        _store_total(db, batch.day, merge_total(_load_total(db, batch.day, delta.region), delta))
    if batch.after is None:
        db.execute('DELETE FROM current_rows WHERE day = ? AND station = ? AND reading_id = ?', key.columns())
    else:
        _put_row(db, as_view_row(batch.after, rule))


def load_region_totals(db, day):
    rows = db.execute('''SELECT region, litres, valid_rows, row_count FROM current_regions
        WHERE day = ? ORDER BY region''', (day,))
    return tuple(_decode_total(row) for row in rows)


def load_view_rows(db, day):
    rows = db.execute('''SELECT region, payload FROM current_rows WHERE day = ?
        ORDER BY region, ordinal, station, reading_id''', (day,))
    return tuple(_decode_view_row(*row) for row in rows)


def replace_current_balance(db, day, rows, totals):
    db.execute('DELETE FROM current_rows WHERE day = ?', (day,))
    db.execute('DELETE FROM current_regions WHERE day = ?', (day,))
    for row in rows:
        if row.image.key.day != day:
            raise ValueError('candidate contains a row from another day')
        _put_row(db, row)
    for total in totals:
        _store_total(db, day, total)
