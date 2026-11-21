from dataclasses import dataclass

from catalog.rules import region_for_station
from source.rows import ObservationImage


@dataclass(frozen=True)
class Contribution:
    region: str
    litres: int
    valid_rows: int
    row_count: int

    def negative(self):
        return Contribution(self.region, -self.litres, -self.valid_rows, -self.row_count)


@dataclass(frozen=True)
class ViewRow:
    region: str
    image: ObservationImage

    def to_data(self):
        return {'region': self.region, 'image': self.image.to_data()}

    @classmethod
    def from_data(cls, data):
        return cls(data['region'], ObservationImage.from_data(data['image']))


def as_view_row(image, rule):
    return ViewRow(region_for_station(rule, image.key.station), image)


def image_contribution(row):
    reading = row.image.reading
    return Contribution(row.region, reading.litres if reading.valid else 0, int(reading.valid), 1)


def batch_delta(batch, rule):
    deltas = []
    if batch.before is not None:
        deltas.append(image_contribution(as_view_row(batch.before, rule)).negative())
    if batch.after is not None:
        deltas.append(image_contribution(as_view_row(batch.after, rule)))
    return tuple(deltas)


def merge_total(previous, delta):
    if previous is None:
        previous = Contribution(delta.region, 0, 0, 0)
    if previous.region != delta.region:
        raise ValueError('cannot mix region contributions')
    total = Contribution(delta.region, previous.litres + delta.litres,
                         previous.valid_rows + delta.valid_rows, previous.row_count + delta.row_count)
    if not 0 <= total.valid_rows <= total.row_count or (total.valid_rows == 0 and total.litres != 0):
        raise ValueError('contributions are inconsistent with the row counts')
    return total


def adjust_totals(totals, deltas):
    by_region = {total.region: total for total in totals}
    for delta in deltas:
        total = merge_total(by_region.get(delta.region), delta)
        if total.row_count:
            by_region[total.region] = total
        else:
            by_region.pop(total.region, None)
    return tuple(by_region[region] for region in sorted(by_region))
