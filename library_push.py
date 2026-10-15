"""Calibre-side preview and pending-push safeguards; no device writes here."""
import copy
import math
from datetime import datetime, timezone

try:
    from calibre_plugins.frankenboox.reader_push import plan_position
    from calibre_plugins.frankenboox.reader_bridge import (ANCHOR_KEY, load_book, _sidecar_entries, _viewer_sidecar_path)
except ImportError:
    from reader_push import plan_position
    from reader_bridge import ANCHOR_KEY, load_book, _sidecar_entries, _viewer_sidecar_path

PUSH_KEY = 'neomatrix_position_push_v1'


def _epoch_ms(value):
    if isinstance(value, str):
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('Reading-position timestamp has no timezone')
        value = stamp.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('Invalid reading-position timestamp')
    return round(value * 1000)


def newest_position(records):
    candidates = []
    for record in records:
        if not isinstance(record, dict):
            continue
        cfi = record.get('cfi') or record.get('pos')
        if not isinstance(cfi, str) or not cfi.startswith('epubcfi('):
            continue
        try:
            when = _epoch_ms(record.get('epoch') if 'epoch' in record else record.get('timestamp'))
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        candidates.append({'cfi': cfi, 'epoch_ms': when})
    if not candidates:
        raise ValueError('No saved Calibre EPUB reading position; close the viewer after reading')
    latest = max(x['epoch_ms'] for x in candidates)
    current = [x for x in candidates if x['epoch_ms'] == latest]
    if len({x['cfi'] for x in current}) != 1:
        raise ValueError('Saved Calibre positions disagree at the same time')
    return current[0]


def farthest_position(records, path):
    """Choose the farthest resolvable CFI; time only breaks equal-position ties."""
    try:
        from calibre_plugins.frankenboox.epub_position import EpubPositionMapper
    except ImportError:
        from epub_position import EpubPositionMapper
    candidates = []
    mapper = EpubPositionMapper(path)
    try:
        for record in records:
            if not isinstance(record, dict):
                continue
            cfi = record.get('cfi') or record.get('pos')
            if not cfi:
                continue
            if '%' in cfi or '@' in cfi:
                raise ValueError('Spatial reading positions cannot be compared exactly')
            rank = mapper.cfi_to_offset(cfi)
            try:
                when = _epoch_ms(record.get('epoch') if 'epoch' in record else record.get('timestamp'))
            except (ValueError, TypeError, OverflowError, OSError):
                when = 0
            candidates.append((rank, when, cfi))
    finally:
        mapper.close()
    if not candidates:
        raise ValueError('No saved Calibre EPUB reading position; close the viewer after reading')
    rank, when, cfi = max(candidates)
    return {'cfi': cfi, 'epoch_ms': when}


def calibre_position(db, book_id, path):
    from calibre.db.constants import EBOOK_VIEWER_USER, EBOOK_VIEWER_DEVICE
    records = list(db.get_last_read_positions(book_id, fmt='EPUB', user=EBOOK_VIEWER_USER))
    records += [x for x in db.annotations_map_for_book(book_id, 'EPUB', user_type='local', user='viewer').get('last-read', [])
                if x.get('pos_type') == 'epubcfi']
    records += [x for x in _sidecar_entries(_viewer_sidecar_path(path))
                if x.get('type') == 'last-read' and x.get('pos_type') == 'epubcfi']
    return farthest_position(records, path)


def preview(db, book_id, state, device, current_row=None):
    if not isinstance(state, dict):
        return {'status': 'unsupported', 'reason': 'No saved BOOX data; run Sync first'}
    fmt=str((state.get('boox_book') or {}).get('format') or '').upper()
    if fmt in {'PDF','CBZ','CBR'}:
        try:
            from calibre_plugins.frankenboox.fixed_sync import preview as fixed_preview
        except ImportError:
            from fixed_sync import preview as fixed_preview
        return fixed_preview(db,book_id,state,device,current_row)
    path = db.format_abspath(book_id, 'EPUB')
    if not path:
        return {'status': 'unsupported', 'reason': 'Reading-position push currently supports EPUB only'}
    verified = db.get_custom_book_data(ANCHOR_KEY).get(book_id) or {}
    state = copy.deepcopy(state)
    state['epub_sha256'] = verified.get('epub_sha256')
    state['epub_identity_schema'] = verified.get('epub_identity_schema')
    state['epub_reading_sha256'] = verified.get('epub_reading_sha256')
    identity = state.get('boox_identity') or {}
    uuid = identity.get('metadata_uuid')
    if not uuid:
        return {'status': 'unsupported', 'reason': 'Saved BOOX copy identity is missing; run Sync first'}
    book = load_book(path)
    book.calibrate(state)
    row = current_row if current_row is not None else device.query(uuid)
    position = calibre_position(db, book_id, path)
    device_sha256 = device.book_sha256(identity.get('path'))
    device_reading_sha256 = None
    if device_sha256 != book.digest:
        device_identity = device.book_identity(identity.get('path'))
        if device_identity['sha256'] != device_sha256:
            raise ValueError('BOOX EPUB changed while comparing; try Sync again')
        device_reading_sha256 = device_identity['reading_sha256']
    plan = plan_position(state, book, row, position, device_sha256, device_reading_sha256)
    if plan['status'] == 'ready':
        plan.update(serial=device.serial, calibre_position=position, calibre_sha256=book.digest)
    return plan


def ensure_pull_ready(db, book_id, state):
    """Do not import the old page fraction until NeoReader consumes a pushed CFI."""
    marker = db.get_custom_book_data(PUSH_KEY).get(book_id)
    if not isinstance(marker, dict) or marker.get('status') not in ('pending', 'written'):
        return
    copies = [state] + (state.get('source_states') or [])
    identity = marker.get('identity') or {}
    same = [copy for copy in copies if all((copy.get('boox_identity') or {}).get(k) == v for k, v in identity.items())]
    if not same:
        raise ValueError('A pushed BOOX copy is absent from this archive. Reconnect it and run Sync before importing positions.')
    try:
        consumed = any(int((copy.get('progress') or {}).get('last_access_ms') or 0) > marker['written_last_access_ms'] for copy in same)
    except (ValueError, TypeError, KeyError):
        consumed = False
    if not consumed:
        raise ValueError('Reopen and close the pushed book in NeoReader, then run Sync. Its page percentage has not been refreshed yet.')
