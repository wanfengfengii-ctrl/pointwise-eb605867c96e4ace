from source.rows import ObservationImage, RowHead


class RevisionConflict(ValueError):
    pass


def plan_revision(key, head, operation, expected_revision, reading, batch_id, sequence):
    if operation == 'add':
        if head is not None:
            raise RevisionConflict('station reading identity is already assigned')
        before = None
        ordinal = sequence
    else:
        if head is None or head.image is None or head.revision != expected_revision:
            raise RevisionConflict('expected revision is not the active revision')
        before = head.image
        ordinal = head.ordinal

    after = None
    if operation != 'withdraw':
        after = ObservationImage(key, ordinal, batch_id, sequence, reading)
    return before, RowHead(ordinal, batch_id, after)
