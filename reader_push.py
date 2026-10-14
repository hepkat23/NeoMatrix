"""Plan EPUB resume pushes from positions observed and verified in NeoReader.

No renderer offset guesses, percentage guesses, or device writes.
"""
import json
import re

try:
    from calibre_plugins.frankenboox.push_values import parse_object, md5_value, timestamp_ms
except ImportError:
    from push_values import parse_object, md5_value, timestamp_ms
try:
    from calibre_plugins.frankenboox.epub_identity import IDENTITY_SCHEMA
except ImportError:
    from epub_identity import IDENTITY_SCHEMA


def position_object(value):
    if isinstance(value, str):
        # Preserve quoted strings while fixing NeoReader's padded integers.
        pattern = r'"(?:\\.|[^"\\])*"|(?P<number>-?\b0+\d+\b)'
        value = re.sub(pattern, lambda m: str(int(m.group())) if m.group('number') else m.group(), value)
    pos = parse_object(value)
    if not isinstance(pos.get('chapterID'), str) or not pos['chapterID']:
        raise ValueError('No observed chapter identity')
    for key in ('nodePosition', 'textOffset'):
        n = pos.get(key)
        if type(n) is not int or n < 0:
            raise ValueError('No verified NeoReader node/text offset')
    return pos


def _backend(row):
    raw = row.get('extraAttributes')
    if not isinstance(raw, str):
        raise ValueError('Raw BOOX metadata is required')
    attrs = parse_object(raw)
    return raw, attrs, parse_object(attrs.get('backend'))


def _resume(backend):
    for key in ('current_page_position_v3', 'current_page_position_v2', 'current_page'):
        if backend.get(key):
            return position_object(backend[key])
    raise ValueError('Current device position is unavailable')


def observed_positions(state, book):
    """Retain source positions only when their corresponding Calibre CFI verifies."""
    identity = state.get('boox_identity') or {}
    progress = state.get('progress') or {}
    candidates = []

    def add(value, quote=None):
        try:
            pos = position_object(value)
            anchor = book.position(pos)
            if not anchor and quote:
                match = book.quote(quote, pos)
                if match:
                    chapter, start, end = match
                    anchor = chapter.anchor(start, 'observed-bookmark-quote')
            if anchor and isinstance(anchor.get('calibre_cfi'), str):
                candidates.append({'cfi': anchor['calibre_cfi'], 'anchor': anchor, 'position': pos,
                                   'raw_position': value if isinstance(value, str) else json.dumps(pos, separators=(',', ':'))})
        except (ValueError, TypeError, KeyError):
            pass

    for key in ('current_page_position_v3', 'current_page_position_v2', 'backend_current_page'):
        add(progress.get(key + '_raw') or progress.get(key))
    for item in (state.get('bookmarks') or {}).get('active') or []:
        if not isinstance(item, dict) or item.get('document_id') != identity.get('document_id'):
            continue
        for key in ('position_v3', 'position_v2', 'chapter_or_position'):
            add(item.get(key), item.get('quote'))
    return candidates


def plan_position(state, book, current_row, calibre_position, device_sha256, device_reading_sha256=None):
    """Return an explicit preview; changed device baselines are never overwritten."""
    def result(status, reason):
        return {'status': status, 'reason': reason}
    if not all(isinstance(x, dict) for x in (state, current_row, calibre_position)):
        return result('unsupported', 'Invalid reading-state input')
    try:
        identity = parse_object(state.get('boox_identity'))
        progress = parse_object(state.get('progress'))
        boox_book = parse_object(state.get('boox_book'))
        if boox_book.get('format', '').lower() != 'epub' or str(current_row.get('type', '')).lower() != 'epub':
            return result('unsupported', 'Reading-position push currently supports EPUB only')
        raw, attrs, backend = _backend(current_row)
        if not identity.get('metadata_uuid') or identity['metadata_uuid'] != current_row.get('uuid'):
            return result('conflict', 'BOOX copy identity differs; pull fresh data first')
        if not identity.get('document_id') or identity['document_id'] != backend.get('doc_id'):
            return result('conflict', 'BOOX document identity differs; pull fresh data first')
        if not identity.get('path') or identity['path'] != current_row.get('location'):
            return result('conflict', 'Book location changed; pull fresh data first')
        expected_md5 = md5_value(identity.get('file_md5'))
        if expected_md5 != md5_value(backend.get('md5') or current_row.get('hashTag')):
            return result('conflict', 'BOOX content identity changed; push was stopped')
        reading = getattr(book, 'reading_digest', None)
        verified_reading = state.get('epub_reading_sha256')
        baseline_matches = (state.get('epub_identity_schema') == IDENTITY_SCHEMA and
                            isinstance(reading, str) and re.fullmatch(r'[0-9a-f]{64}', reading) and
                            verified_reading == reading) if verified_reading else state.get('epub_sha256') == book.digest
        if not baseline_matches:
            return result('conflict', 'Calibre EPUB changed since its BOOX anchors were verified')
        if not isinstance(device_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', device_sha256):
            return result('conflict', 'Could not verify the BOOX EPUB; push was stopped')
        if device_sha256 != book.digest and not (isinstance(reading, str) and
                re.fullmatch(r'[0-9a-f]{64}', reading) and device_reading_sha256 == reading):
            return result('conflict', 'BOOX and Calibre EPUB reading content or structure differ; push was stopped')
        cfi = calibre_position.get('cfi')
        if not isinstance(cfi, str) or not cfi.startswith('epubcfi('):
            return result('unsupported', 'No saved Calibre EPUB reading position')
        matching = [x for x in observed_positions(state, book) if x['cfi'] == cfi]
        unique = {json.dumps(x['position'], sort_keys=True): x for x in matching}
        if len(unique) > 1:
            return result('unsupported', 'BOOX locations for this Calibre position are ambiguous')
        if unique:
            target = next(iter(unique.values()))
        else:
            reverse = getattr(book, 'reverse_position', None)
            if reverse is None:
                return result('unsupported', 'Calibre location has not been verified in BOOX data')
            try:
                target = reverse(cfi, state)
            except (ValueError, TypeError, KeyError, OSError) as error:
                return result('unsupported', 'Cannot send this position to BOOX: ' + str(error))
        current = _resume(backend)
        if target['position'] == current:
            return result('unchanged', 'BOOX already has this reading position')
        for key in ('current_page_position_v3', 'current_page_position_v2', 'current_page'):
            if backend.get(key) and position_object(backend[key]) != current:
                return result('unsupported', 'NeoReader engine positions disagree')
        archived_ms = timestamp_ms(progress.get('last_access_ms'))
        device_ms = timestamp_ms(current_row.get('lastAccess'))
        calibre_ms = timestamp_ms(calibre_position.get('epoch_ms'))
        if device_ms != archived_ms:
            return result('conflict', 'BOOX was read or changed since the last pull')
        archived_backend = _backend(state.get('raw_metadata_row') or {})[2]
        if _resume(archived_backend) != current:
            return result('conflict', 'BOOX position changed since the last pull')
        current_anchor = book.position(current)
        if not current_anchor:
            if target.get('method') != 'verified-source-dom':
                return result('unsupported', 'Current BOOX location cannot be compared safely; no position sent')
            farther = book.compare_native_positions(target['position'], current) > 0
        else:
            target_rank = (target['anchor']['spine_index'], target['anchor']['text_offset'])
            current_rank = (current_anchor['spine_index'], current_anchor['text_offset'])
            farther = target_rank > current_rank
        if not farther:
            return result('unchanged', 'BOOX already has the farthest reading position')
    except (ValueError, TypeError, AttributeError, KeyError):
        return result('unsupported', 'Required identity, position or timestamp data is missing or invalid')
    return {'status': 'ready', 'reason': 'Verified EPUB position is ready for explicit push',
            'metadata_uuid': identity['metadata_uuid'], 'document_id': identity['document_id'],
            'book_path': identity['path'], 'file_md5': expected_md5, 'file_sha256': device_sha256,
            'expected_extra_attributes': raw, 'expected_last_access': current_row['lastAccess'],
            'position': target['position'], 'position_raw': target['raw_position'],
            'calibre_epoch_ms': max(calibre_ms, device_ms + 1)}
