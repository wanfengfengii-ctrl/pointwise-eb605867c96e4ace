import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date


def connect(path):
    db = sqlite3.connect(path, isolation_level=None, cached_statements=0)
    db.execute('PRAGMA foreign_keys = ON')
    return db


@contextmanager
def transaction(db, *, write=False):
    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise


@contextmanager
def read_scope(db):
    if db.in_transaction:
        yield
    else:
        with transaction(db):
            yield


def identifier(value, label):
    if not isinstance(value, str) or not value or value.strip() != value or '\x00' in value:
        raise ValueError(f'invalid {label}')
    value.encode('utf-8')
    return value


def day_id(value):
    identifier(value, 'observation day')
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError('observation day must use YYYY-MM-DD')
    return value


def index(value, label, *, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError(f'invalid {label}')
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()
