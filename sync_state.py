"""Compare saved BOOX information independently of collection bookkeeping."""
import copy
import hashlib
import json


_VOLATILE = ('imported_at_utc', 'sources', 'safety', 'match', 'calibre_integration')
_RECORD_LISTS = (
    ('annotations', 'active'), ('annotations', 'raw_rows'),
    ('bookmarks', 'active'), ('bookmarks', 'raw_rows'),
    ('reader_note', 'document_rows'), ('reader_note', 'shape_rows'),
    ('reader_note', 'resource_rows'), ('handwriting_point_files',),
    ('reading_statistics',),
)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def state_signature(state):
    if not isinstance(state, dict):
        raise ValueError('BOOX state must be an object')
    value = copy.deepcopy(state)
    for key in _VOLATILE:
        value.pop(key, None)
    for path in _RECORD_LISTS:
        parent = value
        for key in path[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if isinstance(parent, dict) and isinstance(parent.get(path[-1]), list):
            parent[path[-1]].sort(key=_json)
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _semantic_state(state):
    """Copy the source data without collection bookkeeping."""
    value = copy.deepcopy(state)
    for key in _VOLATILE:
        value.pop(key, None)
    for path in _RECORD_LISTS:
        parent = value
        for key in path[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if isinstance(parent, dict) and isinstance(parent.get(path[-1]), list):
            parent[path[-1]].sort(key=_json)
    return value


def _unique_records(records):
    values = {_json(record): record for record in records}
    return [copy.deepcopy(values[key]) for key in sorted(values)]


def _source_rank(state):
    try:
        last_read = int((state.get('progress') or {}).get('last_access_ms') or 0)
    except (ValueError, TypeError, OverflowError):
        last_read = 0
    identity = state.get('boox_identity') or {}
    return (last_read, str(identity.get('document_id') or ''),
            str(identity.get('path') or ''), state_signature(state))


def combine_book_entries(entries):
    """Combine BOOX copies before comparing or writing their shared Calibre book.

    The most recently read copy supplies the resume position. All source records,
    annotations and raw companion bytes are retained in a stable combined archive.
    """
    from io import BytesIO
    groups = {}
    for entry in entries:
        groups.setdefault(entry[0], []).append(entry)
    result = []
    state_paths = ('data/neomatrix/boox-neoreader-state.json', 'data/boox-neoreader-state.json')
    for book_id in sorted(groups):
        group = groups[book_id]
        if len(group) == 1:
            result.append(group[0])
            continue
        volumes = set()
        for _, source, _ in group:
            book = source.get('boox_book') or {}
            raw = source.get('raw_metadata_row') or {}
            hint = volume_identity(book.get('title'), book.get('name'), raw.get('location'))
            if hint:
                volumes.add(hint)
        if len(volumes) > 1:
            raise ValueError(f'Different BOOX volumes are matched to Calibre book #{book_id}. Match them to separate Calibre entries before syncing.')
        group = sorted(group, key=lambda entry: _source_rank(entry[1]))
        state = copy.deepcopy(group[-1][1])
        source_states, files = [], {}
        for _, source, companions in group:
            record = _semantic_state(source)
            identity = _json(source.get('boox_identity') or {})
            token = hashlib.sha256(identity.encode('utf-8')).hexdigest()
            preserved = {}
            for rel in sorted(companions):
                if rel in state_paths:
                    continue
                stream = companions[rel]
                target = rel
                if target in files and files[target].getvalue() != stream.getvalue():
                    target = 'data/neomatrix/source_files/' + token + '/' + rel
                    # Distinct snapshots of the same identity must also keep all bytes.
                    if target in files and files[target].getvalue() != stream.getvalue():
                        token_bytes = hashlib.sha256(stream.getvalue()).hexdigest()
                        target = 'data/neomatrix/source_files/' + token + '/' + token_bytes + '/' + rel
                files[target] = BytesIO(stream.getvalue())
                preserved[rel] = target
            record['archive_files'] = preserved
            source_states.append(record)
        state['source_states'] = sorted(source_states, key=_json)
        for key in ('annotations', 'bookmarks'):
            state[key] = {
                field: _unique_records(record for _, source, _ in group
                                       for record in (source.get(key) or {}).get(field, []))
                for field in ('active', 'raw_rows')}
            state[key]['active_count'] = len(state[key]['active'])
            state[key]['all_rows_count'] = len(state[key]['raw_rows'])
        notes = [source['reader_note'] for _, source, _ in group if isinstance(source.get('reader_note'), dict)]
        if notes:
            state['reader_note'] = {
                key: _unique_records(record for note in notes for record in note.get(key, []))
                for key in ('document_rows', 'shape_rows', 'resource_rows')}
            integrity = [note.get('integrity') for note in notes]
            failures = sorted(str(value) for value in integrity if value not in (None, 'ok'))
            state['reader_note']['integrity'] = failures[0] if failures else ('ok' if 'ok' in integrity else None)
        for key in ('handwriting_point_files', 'reading_statistics'):
            state[key] = _unique_records(record for _, source, _ in group for record in source.get(key, []))
        serialized = json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8')
        for rel in state_paths:
            files[rel] = BytesIO(serialized)
        result.append((book_id, state, files))
    return result


def volume_identity(*texts):
    """Recognize explicit volume/part labels without merging different numbers."""
    import re
    pattern = re.compile(
        r'\b(vol(?:ume)?s?|tomes?|tomos?|band|parts?|pt|books?|v)\.?\s*'
        r'(\d+|[ivxlcdm]+)(?![a-z0-9])'
        r'(?:\s*(?:[-–—]|to|through|and|&)\s*(\d+|[ivxlcdm]+)(?![a-z0-9]))?', re.I)
    roman = re.compile(r'M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$')
    def number(token):
        if token.isdigit():
            return int(token)
        token = token.upper()
        if not token or not roman.fullmatch(token):
            return None
        values = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
        result, previous = 0, 0
        for char in reversed(token):
            value = values[char]
            result += -value if value < previous else value
            previous = max(previous, value)
        return result
    hints = set()
    for text in texts:
        for match in pattern.finditer(str(text or '')):
            start, end = number(match[2]), number(match[3]) if match[3] else number(match[2])
            if start is None or end is None:
                continue
            label = match[1].lower()
            kind = 'part' if label.startswith(('part', 'pt')) else ('book' if label.startswith('book') else 'volume')
            hints.add((kind, start, end))
    return tuple(sorted(hints))
