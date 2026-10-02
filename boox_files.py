"""Check recorded book paths without deleting their saved reading history."""
from dataclasses import dataclass
import hashlib
import json
import posixpath
import shlex


@dataclass
class BookFileCheck:
    kept: list
    missing: list
    uncertain: list
    warnings: list


def record_token(serial, row):
    """Exclude only this unchanged record on this device, never just a title.

    A move, replacement or newly saved reading position changes the token and
    makes the record eligible for review again. No private row data is stored
    in preferences: only its digest is retained.
    """
    if not serial or serial == 'unknown' or not isinstance(row, dict):
        return None
    if not (row.get('uuid') or row.get('id')):
        return None
    payload = json.dumps({'device': serial, 'record': row}, sort_keys=True,
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def exclude_saved_records(rows, serial, excluded_tokens):
    excluded_tokens = set(excluded_tokens or ())
    kept, excluded = [], []
    for row in rows:
        token = record_token(serial, row)
        (excluded if token is not None and token in excluded_tokens else kept).append(row)
    return kept, excluded


def check_book_files(rows, query, max_command_bytes=16000):
    """Use bounded, read-only probes; incomplete batches cannot hide books.

    Unknown paths and permission failures remain eligible. A missing-file result
    is trusted only when every candidate's parent directory is accessible.
    """
    rows = list(rows)
    if max_command_bytes < 1:
        raise ValueError('Book check batch size must be positive')
    probes = {}
    for index, row in enumerate(rows):
        paths = sorted({str(row[key]) for key in ('nativeAbsolutePath', 'location', 'nocasePath')
                        if row.get(key) and str(row[key]).startswith('/')})
        if paths:
            probes[index] = paths
    if not probes:
        return BookFileCheck(rows, [], rows, [])
    commands = []
    for index, paths in probes.items():
        exists = ' || '.join('[ -f ' + shlex.quote(path) + ' ]' for path in paths)
        accessible = ' && '.join(
            '( [ -d ' + shlex.quote(posixpath.dirname(path)) + ' ] && [ -r '
            + shlex.quote(posixpath.dirname(path)) + ' ] && [ -x '
            + shlex.quote(posixpath.dirname(path)) + ' ] )' for path in paths)
        commands.append((index,
            f'if {exists}; then printf "{index}:P\\n"; '
            f'elif {accessible}; then printf "{index}:M\\n"; '
            f'else printf "{index}:U\\n"; fi'))
    batches, batch, size = [], [], 0
    statuses, warnings = {}, []
    for index, command in commands:
        length = len(command.encode('utf-8')) + 1
        if length > max_command_bytes:
            warnings.append('A book path was too long to check; its history was kept.')
            continue
        if batch and size + length > max_command_bytes:
            batches.append(batch); batch, size = [], 0
        batch.append((index, command)); size += length
    if batch:
        batches.append(batch)
    for batch in batches:
        expected = {index for index, _ in batch}
        try:
            output = query('\n'.join(command for _, command in batch))
            checked = {}
            for line in output.splitlines():
                parts = line.strip().split(':')
                if len(parts) != 2 or parts[1] not in ('P', 'M', 'U'):
                    raise ValueError('Incomplete book check')
                index = int(parts[0])
                if index not in expected or index in checked:
                    raise ValueError('Ambiguous book check')
                checked[index] = parts[1]
            if set(checked) != expected:
                raise ValueError('Incomplete book check')
            statuses.update(checked)
        except Exception:
            warnings.append('Some book files could not be checked; their records were kept.')
    kept = [row for index, row in enumerate(rows) if statuses.get(index) != 'M']
    missing = [row for index, row in enumerate(rows) if statuses.get(index) == 'M']
    uncertain = [row for index, row in enumerate(rows) if statuses.get(index) not in ('P', 'M')]
    return BookFileCheck(kept, missing, uncertain, list(dict.fromkeys(warnings)))


def retain_existing_books(rows, query):
    """Compatibility interface: confirmed missing records are excluded from sync."""
    result = check_book_files(rows, query)
    return result.kept, len(result.missing)
