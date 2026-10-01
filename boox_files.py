"""Exclude stale NeoReader records only when their ebook is confirmed missing."""
import posixpath
import shlex


def retain_existing_books(rows, query):
    """Query existing paths in one read-only ADB shell call.

    Unknown paths and permission failures remain eligible. A missing-file result
    is trusted only when every candidate's parent directory is accessible.
    """
    probes = {}
    for index, row in enumerate(rows):
        paths = sorted({str(row[key]) for key in ('nativeAbsolutePath', 'location', 'nocasePath')
                        if row.get(key) and str(row[key]).startswith('/')})
        if paths:
            probes[index] = paths
    if not probes:
        return rows, 0
    commands = []
    for index, paths in probes.items():
        exists = ' || '.join('[ -f ' + shlex.quote(path) + ' ]' for path in paths)
        accessible = ' && '.join(
            '( [ -d ' + shlex.quote(posixpath.dirname(path)) + ' ] && [ -r '
            + shlex.quote(posixpath.dirname(path)) + ' ] && [ -x '
            + shlex.quote(posixpath.dirname(path)) + ' ] )' for path in paths)
        commands.append(
            f'if {exists}; then printf "{index}:P\\n"; '
            f'elif {accessible}; then printf "{index}:M\\n"; '
            f'else printf "{index}:U\\n"; fi')
    try:
        output = query('\n'.join(commands))
        statuses = {}
        for line in output.splitlines():
            parts = line.strip().split(':')
            if len(parts) != 2 or parts[1] not in ('P', 'M', 'U'):
                return rows, 0
            index = int(parts[0])
            if index not in probes or index in statuses:
                return rows, 0
            statuses[index] = parts[1]
        if set(statuses) != set(probes):
            return rows, 0
        kept = [row for index, row in enumerate(rows) if statuses.get(index) != 'M']
        return kept, len(rows) - len(kept)
    except Exception:
        # Device disconnection, unsupported shell or unreadable storage cannot
        # prove that a book was deleted. Preserve the original collection.
        return rows, 0
