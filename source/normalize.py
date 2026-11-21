from dataclasses import dataclass

from storage import digest


@dataclass(frozen=True)
class NormalizedReading:
    value_ml: int | None
    litres: int | None
    unit: str = 'mL'

    @property
    def valid(self):
        return self.litres is not None


def normalize_reading(value_ml):
    if value_ml is None:
        return NormalizedReading(None, None)
    if type(value_ml) is not int or value_ml % 1000:
        raise ValueError('reading must be integer millilitres divisible by 1000 or null')
    return NormalizedReading(value_ml, value_ml // 1000)


def submission_fingerprint(key, operation, expected_revision, reading):
    return digest({
        'key': key.to_data(),
        'operation': operation,
        'expected_revision': expected_revision,
        'millilitres': None if reading is None else reading.value_ml,
    })
