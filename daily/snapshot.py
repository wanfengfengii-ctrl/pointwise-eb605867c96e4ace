from dataclasses import dataclass

from daily.balance import load_region_totals, load_view_rows
from daily.contribution import Contribution, ViewRow
from daily.progress import read_published_head
from storage import day_id, read_scope


@dataclass(frozen=True)
class PublishedSnapshot:
    day: str
    rule_id: str
    sequence: int
    totals: tuple[Contribution, ...]
    rows: tuple[ViewRow, ...]


def read_published_snapshot(db, day):
    day_id(day)
    with read_scope(db):
        head = read_published_head(db, day)
        if head is None:
            return None
        return PublishedSnapshot(day, head.rule_id, head.sequence,
                                 load_region_totals(db, day), load_view_rows(db, day))
