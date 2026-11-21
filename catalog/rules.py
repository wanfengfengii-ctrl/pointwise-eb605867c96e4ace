import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from storage import canonical, identifier


class RuleConflict(ValueError):
    pass


class UnmappedStation(ValueError):
    pass


@dataclass(frozen=True)
class RuleVersion:
    rule_id: str
    station_regions: Mapping[str, str]

    def __post_init__(self):
        identifier(self.rule_id, 'rule id')
        if not isinstance(self.station_regions, Mapping):
            raise ValueError('station regions must be a mapping')
        mapping = {}
        for station, region in self.station_regions.items():
            identifier(station, 'station')
            identifier(region, 'region')
            if station in mapping and mapping[station] != region:
                raise RuleConflict(f'station has conflicting regions: {station}')
            mapping[station] = region
        object.__setattr__(self, 'station_regions', MappingProxyType(mapping))


def ensure_rule_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS rule_versions (rule_id TEXT PRIMARY KEY, mapping TEXT NOT NULL)')


def register_rule_version(db, rule):
    payload = canonical(dict(rule.station_regions))
    existing = db.execute('SELECT mapping FROM rule_versions WHERE rule_id = ?', (rule.rule_id,)).fetchone()
    if existing is None:
        db.execute('INSERT INTO rule_versions VALUES (?, ?)', (rule.rule_id, payload))
    elif existing[0] != payload:
        raise RuleConflict(f'conflicting mapping for rule {rule.rule_id}')
    return rule


def load_rule_version(db, rule_id):
    identifier(rule_id, 'rule id')
    row = db.execute('SELECT mapping FROM rule_versions WHERE rule_id = ?', (rule_id,)).fetchone()
    if row is None:
        raise KeyError(f'unknown rule {rule_id}')
    return RuleVersion(rule_id, json.loads(row[0]))


def region_for_station(rule, station):
    try:
        return rule.station_regions[station]
    except KeyError:
        raise UnmappedStation(f'{station} has no region in rule {rule.rule_id}') from None
