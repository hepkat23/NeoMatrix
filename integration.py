"""Calibre library integration. Original companion archives remain canonical."""
import copy
import html
import re
from datetime import datetime, timezone
from calibre_plugins.frankenboox.sync_state import state_signature

DATA_KEY = 'neomatrix_reading_state_v1'
COLUMNS = (
    ('nm_progress', 'BOOX progress (%)', 'float'),
    ('nm_last_read', 'BOOX last read', 'datetime'),
    ('nm_status', 'BOOX reading status', 'text'),
    ('nm_highlights', 'BOOX highlights', 'int'),
    ('nm_notes', 'BOOX typed notes', 'int'),
    ('nm_bookmarks', 'BOOX bookmarks', 'int'),
    ('nm_reading_notes', 'BOOX reading notes', 'comments'),
)

class RestartRequired(Exception):
    pass


def ensure_columns(db):
    if getattr(db, '_neomatrix_restart_required', False):
        raise RestartRequired('Restart Calibre to activate the newly created BOOX columns, then integrate saved data.')
    # Preflight every existing field before creating anything.
    for label, name, kind in COLUMNS:
        field = db.field_metadata.get('#' + label)
        if field and (field.get('datatype') != kind or field.get('is_multiple')):
            raise ValueError('Column #' + label + ' has an incompatible type; no reading data was changed.')
    missing = [(label, name, kind) for label, name, kind in COLUMNS
               if db.field_metadata.get('#' + label) is None]
    for label, name, kind in missing:
        db.create_custom_column(label, name, kind, False)
        db._neomatrix_restart_required = True
    if missing:
        raise RestartRequired('Created BOOX columns. Restart Calibre once, then click Integrate saved data. '
                              'For a fresh BOOX collection, run Sync connected BOOX again after restarting.')


def number(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def active_items(state, name):
    block = state.get(name) or {}
    return [a for a in (block.get('active') or []) if a.get('status') in (None, 0, '0')]


def prepare(state):
    if not isinstance(state, dict) or not isinstance(state.get('progress'), dict):
        raise ValueError('This archive has no valid reading-state object.')
    p = state['progress']
    current, total = number(p.get('current_page')), number(p.get('total_pages'))
    # Old exports contain {} instead of a page number; the raw fraction survives.
    m = re.fullmatch(r'\s*(\d+)\s*/\s*(\d+)\s*', str(p.get('raw') or ''))
    if m:
        current, total = map(int, m.groups())
    percent = round(100 * current / total, 2) if total and current is not None and 0 <= current <= total else None
    last = None
    ms = number(p.get('last_access_ms'))
    if ms and ms > 0:
        try:
            last = datetime.fromtimestamp(ms / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
    if last is None and p.get('last_access_utc'):
        try:
            last = datetime.fromisoformat(p['last_access_utc'].replace('Z', '+00:00'))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    # Calibre persists datetime custom columns at whole-second precision.
    # Keep device milliseconds in the state archive, but compare/write the column
    # in the same representation that survives closing and reopening the library.
    column_last = last.replace(microsecond=0) if last is not None else None
    anns, bookmarks = active_items(state, 'annotations'), active_items(state, 'bookmarks')
    # Retain the actual device enum instead of guessing what each enum means.
    status = (state.get('boox_book') or {}).get('reading_status')
    status_text = 'Device status ' + str(status) if status is not None else None
    def esc(v):
        return html.escape(str(v if v is not None else ''), quote=True).replace('\n', '<br>')
    lines = ['<h2>BOOX reading notes</h2>', '<p>Progress: ' + esc(p.get('raw')) + '</p>']
    if last:
        lines.append('<p>Last read: ' + esc(last.isoformat()) + '</p>')
    lines.append('<h3>Highlights and typed notes</h3>')
    for a in anns:
        lines.append('<p><b>' + esc(a.get('chapter')) + '</b></p><blockquote>' + esc(a.get('quote')) + '</blockquote>')
        if a.get('note'):
            lines.append('<p>' + esc(a['note']) + '</p>')
    lines.append('<h3>Bookmarks</h3>')
    for b in bookmarks:
        page = number(b.get('page_number_zero_based'))
        lines.append('<p>' + esc(b.get('title') or b.get('quote') or 'Bookmark') +
                     (' — BOOX page ' + str(page + 1) if page is not None else '') + '</p>')
    lines.append('<p>Handwriting and original position data are available in NeoMatrix → Browse saved reading data.</p>')
    values = dict(zip((x[0] for x in COLUMNS), (
        percent, column_last, status_text, sum(bool(a.get('quote')) for a in anns),
        sum(bool(a.get('note')) for a in anns), len(bookmarks), '\n'.join(lines))))
    payload = copy.deepcopy(state)
    payload['calibre_integration'] = {'schema': 1, 'percentage': percent,
                                    'native_reader_locations_converted': False}
    return values, payload


def integrate(db, book_id, state):
    from calibre_plugins.frankenboox.library_push import ensure_pull_ready
    ensure_pull_ready(db, book_id, state)
    values, payload = prepare(state)
    # Save previous values for recovery if any database write fails.
    old = {label: db.field_for('#' + label, book_id) for label in values}
    previous = db.get_custom_book_data(DATA_KEY).get(book_id)
    written = []
    try:
        for label, value in values.items():
            if old[label] == value:
                continue
            db.set_field('#' + label, {book_id: value})
            written.append(label)
        # Shared, versioned representation for future consumers, including CrossPoint.
        # delete_first=False preserves other books' records.
        if (not isinstance(previous, dict) or state_signature(previous) != state_signature(payload) or
                previous.get('calibre_integration') != payload.get('calibre_integration')):
            db.add_custom_book_data(DATA_KEY, {book_id: payload}, delete_first=False)
    except Exception as error:
        failures = []
        for label in reversed(written):
            try:
                db.set_field('#' + label, {book_id: old[label]})
            except Exception as restore_error:
                failures.append(str(restore_error))
        if failures:
            raise RuntimeError(str(error) + '; restoring columns also failed: ' + '; '.join(failures)) from error
        raise


def integrate_with_reader(db, book_id, state, use_boox_position=False, keep_boox_position=False):
    # Library integration is independently useful even when no EPUB anchor can be mapped.
    integrate(db, book_id, state)
    from calibre_plugins.frankenboox.reader_bridge import integrate_reader, REPORT_KEY
    try:
        return integrate_reader(
            db, book_id, state,
            use_boox_position=use_boox_position,
            keep_boox_position=keep_boox_position,
        )
    except Exception as e:
        report = {'mapped':0,'resume':False,'skipped':['Reader integration failed: '+str(e)]}
        db.add_custom_book_data(REPORT_KEY,{book_id:report},delete_first=False)
        return report
