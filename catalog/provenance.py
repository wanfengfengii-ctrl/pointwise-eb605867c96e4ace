from storage import digest


class BaselineIntegrityError(ValueError):
    pass


def verify_source_slice(day, cut, images):
    identities = set()
    previous_ordinal = 0
    for image in images:
        if image.key.day != day or not previous_ordinal < image.ordinal <= image.sequence <= cut:
            raise BaselineIntegrityError('row identity, order or sequence differs from baseline cut')
        if image.key in identities:
            raise BaselineIntegrityError('duplicate baseline row identity')
        identities.add(image.key)
        previous_ordinal = image.ordinal


def baseline_digest(images):
    return digest([image.to_data() for image in images])


def restore_source_cut(day, cut, latest, images, tail):
    if cut > latest:
        raise BaselineIntegrityError('source has not reached the requested cut')
    verify_source_slice(day, latest, images)
    if len(tail) != latest - cut:
        raise BaselineIntegrityError('source material for the requested cut was reclaimed')

    current = {image.key: image for image in images}
    for sequence, batch in zip(range(latest, cut, -1), reversed(tail)):
        if batch.day != day or batch.sequence != sequence:
            raise BaselineIntegrityError('source material has a noncontiguous sequence')
        image = batch.after if batch.after is not None else batch.before
        if image is None or image.key.day != day or current.get(image.key) != batch.after:
            raise BaselineIntegrityError('source change does not match the later snapshot')
        if batch.before is None:
            del current[image.key]
        else:
            if batch.before.key != image.key or batch.before.sequence >= sequence:
                raise BaselineIntegrityError('source change contains an invalid before image')
            current[image.key] = batch.before

    result = tuple(sorted(current.values(), key=lambda image: image.ordinal))
    verify_source_slice(day, cut, result)
    return result
