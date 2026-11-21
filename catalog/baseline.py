from contextlib import nullcontext
from dataclasses import dataclass

from catalog.manifest import (
    BaselineManifest, bind_prepared_rule, find_baseline, load_baseline_page,
    load_manifest, load_retained_changes, retain_change, store_baseline,
)
from catalog.provenance import BaselineIntegrityError, baseline_digest, restore_source_cut, verify_source_slice
from catalog.rules import region_for_station, register_rule_version
from daily.progress import read_published_head
from daily.receive import lookup_delivery
from source.feed import read_changes
from source.rows import read_source_slice
from storage import day_id, digest, identifier, index, read_scope, transaction


@dataclass(frozen=True)
class PreparedBaseline:
    manifest: BaselineManifest
    rule_id: str


def _seed_published_changes(db, source, manifest):
    head = read_published_head(db, manifest.day)
    if head is None or head.sequence <= manifest.cut:
        return
    retained = {batch.sequence: batch for batch in load_retained_changes(
        db, manifest.day, after=manifest.cut, through=head.sequence,
    )}
    source_batches = {}
    if len(retained) != head.sequence - manifest.cut and source is not None:
        source_batches = {batch.sequence: batch for batch in read_changes(
            source, manifest.day, after=manifest.cut, through=head.sequence,
        )}
    for sequence in range(manifest.cut + 1, head.sequence + 1):
        batch = retained.get(sequence) or source_batches.get(sequence)
        receipt = lookup_delivery(db, manifest.day, sequence)
        if batch is None or receipt is None or batch.fingerprint != receipt.fingerprint:
            raise BaselineIntegrityError(f'published change {sequence} is unavailable for this cut')
        if sequence not in retained:
            retain_change(db, batch)


def prepare(db, source, day, cut, rule):
    day_id(day)
    index(cut, 'baseline cut')
    source_scope = nullcontext() if source is None else read_scope(source)
    with transaction(db, write=True), source_scope:
        manifest = find_baseline(db, day, cut)
        if manifest is None:
            if source is None:
                raise BaselineIntegrityError('a source connection is required for a new cut')
            latest, live_rows = read_source_slice(source, day)
            tail = read_changes(source, day, after=cut, through=latest)
            images = restore_source_cut(day, cut, latest, live_rows, tail)
            row_digest = baseline_digest(images)
            manifest = BaselineManifest(digest([day, cut, row_digest]), day, cut, len(images), row_digest)
            store_baseline(db, manifest, images)
        else:
            _, images = read_baseline(db, manifest.baseline_id)

        for image in images:
            region_for_station(rule, image.key.station)
        register_rule_version(db, rule)
        _seed_published_changes(db, source, manifest)
        bind_prepared_rule(db, manifest.baseline_id, rule.rule_id)
        return PreparedBaseline(manifest, rule.rule_id)


def read_baseline(db, baseline_id):
    identifier(baseline_id, 'baseline id')
    with read_scope(db):
        manifest = load_manifest(db, baseline_id)
        rows = load_baseline_page(db, baseline_id, limit=max(1, manifest.row_count + 1))
        verify_source_slice(manifest.day, manifest.cut, rows)
        if len(rows) != manifest.row_count or baseline_digest(rows) != manifest.digest:
            raise BaselineIntegrityError('fixed rows differ from their manifest')
        return manifest, rows


def read_baseline_page(db, baseline_id, *, after=0, limit=100):
    identifier(baseline_id, 'baseline id')
    with read_scope(db):
        load_manifest(db, baseline_id)
        return load_baseline_page(db, baseline_id, after=after, limit=limit)
