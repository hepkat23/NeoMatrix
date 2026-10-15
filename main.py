import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tarfile
import tempfile
import time
import zlib
import zipfile
from pathlib import Path
from datetime import datetime, timezone

from qt.core import (
    QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QTabWidget,
    QTextEdit, QWidget, QHeaderView, QPainter, QPen, QColor, Qt, QMenu
)
from calibre.utils.config import JSONConfig
from calibre_plugins.frankenboox.integration import ensure_columns, integrate_with_reader as integrate, RestartRequired
from calibre_plugins.frankenboox.sync_state import state_signature, combine_book_entries, volume_identity

from calibre_plugins.frankenboox.storage_cleanup import read_metadata_exact
from calibre_plugins.frankenboox.boox_files import check_book_files, exclude_saved_records

SYNC_KEY = 'neomatrix_sync_status_v1'
from calibre_plugins.frankenboox.approval import ApprovalDialog
from calibre_plugins.frankenboox.background import run_background

prefs = JSONConfig('plugins/frankenboox_sync')
prefs.defaults['manual_matches'] = {}
prefs.defaults['excluded_book_records'] = {}

KEY_RE = re.compile(r', (?=[A-Za-z_][A-Za-z0-9_]*=)')
UUID_RE = re.compile(rb'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
RN_DB_RE = re.compile(r'ReaderNoteDatabase-([0-9a-fA-F]{32})\.db$')
DOC_IN_POINT_PATH_RE = re.compile(r'/\.ksync/point/([0-9a-fA-F]{32})/')


def norm(s):
    if s is None:
        return ''
    s = str(s).strip().lower()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'[^0-9a-z\u00c0-\u024f]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def parse_adb_row(line):
    if not line.startswith('Row: '):
        return None
    try:
        payload = line.split(' ', 2)[2]
    except Exception:
        return None
    out = {}
    for part in KEY_RE.split(payload):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        v = v.strip()
        if v == 'NULL':
            v = None
        out[k.strip()] = v
    return out


def rows_from_text(text):
    return [r for r in (parse_adb_row(x) for x in text.splitlines()) if r]


def read_member_text(zf, suffixes):
    if isinstance(suffixes, str):
        suffixes = [suffixes]
    for name in zf.namelist():
        if name.startswith('__MACOSX/'):
            continue
        low = name.lower()
        for suffix in suffixes:
            if low.endswith(suffix.lower()):
                return zf.read(name).decode('utf-8', 'replace')
    return ''


def tolerant_json(raw):
    if raw is None or raw == '':
        return {}
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    s = str(raw).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # NeoReader sometimes emits JSON-shaped position objects with illegal
    # leading-zero integers, e.g. chapterIndex:0000000003. Preserve raw data
    # elsewhere, but normalize these numbers for machine-readable use.
    try:
        fixed = re.sub(r'(?<=[:\[,])0+(?=\d)', '', s)
        return json.loads(fixed)
    except Exception:
        return {}


def _nonempty_position(value):
    """Return a structured NeoReader position, or None for scalar/empty data."""
    parsed = tolerant_json(value)
    return parsed if isinstance(parsed, dict) and parsed else None


def boox_resume_candidate(row):
    """Whether a metadata row contains a position the EPUB converter can try."""
    extra = tolerant_json(row.get('extraAttributes'))
    backend = extra.get('backend') if isinstance(extra, dict) else {}
    backend = tolerant_json(backend)
    if not isinstance(backend, dict):
        return False
    if str(row.get('type','')).lower() in {'cbz','cbr'}:
        return any((type(backend.get(key)) is int and backend[key]>=0) or
                   (isinstance(backend.get(key),str) and re.fullmatch(r'\d+',backend[key]))
                   for key in ('current_page_position_v3','current_page_position_v2','current_page'))
    return any(_nonempty_position(backend.get(key)) for key in (
        'current_page_position_v3', 'current_page_position_v2', 'current_page'))


def state_resume_candidate(state):
    """Whether an archived state still contains a structured BOOX position."""
    progress = state.get('progress') if isinstance(state, dict) else {}
    if not isinstance(progress, dict):
        return False
    if str((state.get('boox_book') or {}).get('format') or '').lower() in {'cbz','cbr'}:
        return any((type(progress.get(key)) is int and progress[key]>=0) or
                   (isinstance(progress.get(key),str) and re.fullmatch(r'\d+',progress[key]))
                   for key in ('current_page_position_v3_raw','current_page_position_v2_raw',
                               'current_page_position_v3','current_page_position_v2'))
    return any(_nonempty_position(progress.get(key)) for key in (
        'current_page_position_v3', 'current_page_position_v2', 'backend_current_page'))


def parse_progress(raw):
    if not raw:
        return None, None
    m = re.match(r'^\s*(\d+)\s*/\s*(\d+)\s*$', str(raw))
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def iso_from_ms(raw):
    try:
        return datetime.fromtimestamp(int(raw) / 1000.0, timezone.utc).isoformat()
    except Exception:
        return None


def _int_or_none(v):
    try:
        return int(v)
    except Exception:
        return None


def stable_boox_key(row):
    extra = tolerant_json(row.get('extraAttributes'))
    backend = extra.get('backend') if isinstance(extra, dict) else {}
    doc_id = backend.get('doc_id') if isinstance(backend, dict) else None
    return doc_id or row.get('hashTag') or row.get('uuid') or row.get('location') or row.get('name') or ''


def boox_doc_id(row):
    extra = tolerant_json(row.get('extraAttributes'))
    backend = extra.get('backend') if isinstance(extra, dict) else {}
    return backend.get('doc_id') if isinstance(backend, dict) else None


def boox_md5(row):
    extra = tolerant_json(row.get('extraAttributes'))
    backend = extra.get('backend') if isinstance(extra, dict) else {}
    if isinstance(backend, dict) and backend.get('md5'):
        return backend.get('md5')
    return row.get('hashTag')


def normalized_annotation(a):
    page = _int_or_none(a.get('pageNumber'))
    return {
        'quote': a.get('quote') or '',
        'note': a.get('note') or '',
        'chapter': a.get('chapter') or '',
        'page_number_zero_based': page,
        'page_number_display': page + 1 if page is not None else None,
        'style_shape': _int_or_none(a.get('shape')),
        'color_android_int': _int_or_none(a.get('color')),
        'created_at_ms': _int_or_none(a.get('createdAt')),
        'created_at_utc': iso_from_ms(a.get('createdAt')),
        'updated_at_ms': _int_or_none(a.get('updatedAt')),
        'status': _int_or_none(a.get('status')),
        'annotation_uuid': a.get('uuid'),
        'document_id': a.get('idString'),
        'position': tolerant_json(a.get('position')),
        'position_v2': tolerant_json(a.get('positionV2')),
        'position_v3': tolerant_json(a.get('positionV3')),
        'location_begin': tolerant_json(a.get('locationBegin')) if str(a.get('locationBegin') or '').startswith('{') else a.get('locationBegin'),
        'location_end': tolerant_json(a.get('locationEnd')) if str(a.get('locationEnd') or '').startswith('{') else a.get('locationEnd'),
        'rectangles': tolerant_json(a.get('rectangles')) if str(a.get('rectangles') or '').startswith('[') else a.get('rectangles'),
        'application': a.get('application'),
        'obj_id': a.get('objId'),
    }


def normalized_bookmark(b):
    return {
        'title': b.get('title') or '',
        'quote': b.get('quote') or '',
        'chapter_or_position': b.get('position'),
        'page_number_zero_based': _int_or_none(b.get('pageNumber')),
        'position_int': _int_or_none(b.get('positionInt')),
        'position_v2': b.get('positionV2'),
        'position_int_v2': _int_or_none(b.get('positionIntV2')),
        'position_v3': b.get('positionV3'),
        'position_int_v3': _int_or_none(b.get('positionIntV3')),
        'xpath': b.get('xpath'),
        'position_version': _int_or_none(b.get('positionVersion')),
        'uuid': b.get('uuid'),
        'guid': b.get('guid'),
        'document_id': b.get('idString'),
        'status': _int_or_none(b.get('status')),
        'created_at_ms': _int_or_none(b.get('createdAt')),
        'created_at_utc': iso_from_ms(b.get('createdAt')),
        'updated_at_ms': _int_or_none(b.get('updatedAt')),
        'raw_position': {
            'position': b.get('position'),
            'positionV2': b.get('positionV2'),
            'positionV3': b.get('positionV3'),
        },
    }


def safe_json_value(v):
    if isinstance(v, bytes):
        return {'blob_length': len(v), 'blob_sha256': hashlib.sha256(v).hexdigest()}
    return v


def sqlite_rows_from_bytes(data):
    out = {'document_rows': [], 'shape_rows': [], 'resource_rows': [], 'integrity': None}
    fd, path = tempfile.mkstemp(prefix='neomatrix-rn-', suffix='.db')
    os.close(fd)
    try:
        with open(path, 'wb') as f:
            f.write(data)
        con = sqlite3.connect(path)
        try:
            try:
                out['integrity'] = con.execute('PRAGMA integrity_check').fetchone()[0]
            except Exception:
                pass
            for table, key in (
                ('ReaderNoteDocumentModel', 'document_rows'),
                ('ReaderNoteShapeModel', 'shape_rows'),
                ('ReaderNoteResourceModel', 'resource_rows'),
            ):
                try:
                    cur = con.execute('SELECT * FROM ' + table)
                    cols = [d[0] for d in cur.description]
                    out[key] = [
                        {cols[i]: safe_json_value(v) for i, v in enumerate(row)}
                        for row in cur.fetchall()
                    ]
                except Exception:
                    pass
        finally:
            con.close()
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    return out


def point_file_summary(data):
    uuids = [m.group(0).decode('ascii') for m in UUID_RE.finditer(data)]
    summary = {
        'byte_length': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
        'embedded_uuids': uuids,
        'file_uuid': uuids[0] if uuids else None,
        'shape_uuids': uuids[1:] if len(uuids) > 1 else [],
        'decoded_point_records': None,
        'decoded_stroke_count': None,
    }
    # Diagnostic decoder derived from actual NeoReader point files. We keep
    # raw files canonical; these counts are intentionally advisory.
    try:
        matches = list(UUID_RE.finditer(data))
        footer_start = matches[1].start() - 4 if len(matches) > 1 else len(data)
        words = [data[i:i+4] for i in range(80, footer_start, 4)]
        pts = []
        for j in range(len(words) - 3):
            x = struct.unpack('>f', words[j])[0]
            y = struct.unpack('>f', words[j+1])[0]
            stamp = struct.unpack('>I', words[j+2])[0]
            elapsed = struct.unpack('>I', words[j+3])[0]
            if 0 <= x <= 10000 and 0 <= y <= 10000 and 0xC0 <= (stamp >> 24) <= 0xFF and elapsed < 10000000:
                pts.append((x, y, elapsed))
        if pts:
            strokes = 1
            prev = pts[0][2]
            for _, _, elapsed in pts[1:]:
                if elapsed < prev:
                    strokes += 1
                prev = elapsed
            summary['decoded_point_records'] = len(pts)
            summary['decoded_stroke_count'] = strokes
    except Exception:
        pass
    return summary


def find_adb():
    on_path = shutil.which('adb')
    if on_path:
        return on_path
    candidates = [
        os.path.expanduser('~/.boox-diagnostic-platform-tools/platform-tools/adb'),
        '/opt/homebrew/bin/adb', '/usr/local/bin/adb', '/usr/bin/adb'
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def adb_text(adb, *args, timeout=60):
    cp = subprocess.run([adb] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=timeout, check=False)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.decode('utf-8', 'replace').strip() or 'ADB command failed')
    return cp.stdout.decode('utf-8', 'replace').replace('\r', '')


def extract_android_backup(ab_path):
    raw = open(ab_path, 'rb').read()
    if not raw.startswith(b'ANDROID BACKUP\n'):
        raise ValueError('NeoReader backup did not have an Android-backup header.')
    pos = len(b'ANDROID BACKUP\n')
    def line():
        nonlocal pos
        e = raw.index(b'\n', pos); v = raw[pos:e].decode('ascii', 'replace'); pos = e + 1; return v
    version = line(); compressed = line(); encryption = line()
    if encryption != 'none':
        raise ValueError('Encrypted Android backups are not supported by NeoMatrix.')
    payload = raw[pos:]
    if compressed == '1':
        payload = zlib.decompress(payload)
    tf = tempfile.NamedTemporaryFile(prefix='neomatrix-backup-', suffix='.tar', delete=False)
    try:
        tf.write(payload); tf.close()
        out = {}
        with tarfile.open(tf.name, 'r:') as tar:
            for m in tar.getmembers():
                if not m.isfile(): continue
                base = os.path.basename(m.name)
                if RN_DB_RE.search(base):
                    f = tar.extractfile(m)
                    if f: out[base] = f.read()
        return out
    finally:
        try: os.remove(tf.name)
        except Exception: pass


def collect_readernote_dbs(adb):
    """Collect ReaderNote DBs without writing to NeoReader.

    Route A uses run-as when firmware permits it. Route B falls back to
    Android's own local backup mechanism, which may require approval on BOOX.
    """
    out = {}

    # Route A: direct read through Android run-as. Some BOOX builds permit this.
    try:
        cp = subprocess.run(
            [adb, 'shell', 'run-as', 'com.onyx.kreader', 'sh', '-c',
             'find databases -maxdepth 1 -type f -name "ReaderNoteDatabase*" -print'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False
        )
        listing = cp.stdout.decode('utf-8', 'replace').replace('\r', '')
        for rel in listing.splitlines():
            rel = rel.strip()
            base = os.path.basename(rel)
            m = RN_DB_RE.search(base)
            if not rel or not m:
                continue
            got = subprocess.run(
                [adb, 'exec-out', 'run-as', 'com.onyx.kreader', 'cat', rel],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False
            )
            if got.returncode == 0 and got.stdout:
                key = m.group(1).lower()
                out[key] = {'name': base, 'bytes': got.stdout,
                            'parsed': sqlite_rows_from_bytes(got.stdout)}
    except Exception:
        pass

    if out:
        return out, 'run-as'

    # Route B: Android backup. This is the same read-only route proven by the
    # standalone ReaderNote extractor on the test BOOX.
    # Use a fresh directory and a path that does not already exist. This mirrors
    # the standalone ReaderNote extractor that successfully collected the BOOX
    # databases, and avoids handing adb a pre-created output file.
    backup_dir = tempfile.mkdtemp(prefix='neomatrix-kreader-')
    ab = os.path.join(backup_dir, 'kreader.ab')
    try:
        cp = subprocess.run(
            [adb, 'backup', '-f', ab, '-noapk', '-nosystem', 'com.onyx.kreader'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=240, check=False
        )
        size = os.path.getsize(ab) if os.path.exists(ab) else 0
        if size <= 100:
            err = cp.stderr.decode('utf-8', 'replace').strip()
            detail = f' (adb: {err})' if err else ''
            raise ValueError(f'Android backup returned no usable NeoReader data ({size} bytes){detail}.')
        for base, data in extract_android_backup(ab).items():
            m = RN_DB_RE.search(base)
            if m:
                key = m.group(1).lower()
                out[key] = {'name': base, 'bytes': data,
                            'parsed': sqlite_rows_from_bytes(data)}
        if not out:
            raise ValueError('Android backup completed, but no ReaderNote databases were present.')
        return out, 'android-backup'
    finally:
        try:
            if os.path.exists(ab):
                os.remove(ab)
            os.rmdir(backup_dir)
        except Exception:
            pass


def pull_point_files(adb, doc_ids):
    """Read handwriting point files only from already-known BOOX document directories.

    Safety rule: do not traverse the entire .ksync/point tree. Every remote path queried here
    is derived from a 32-hex document id previously returned by NeoReader metadata.
    The BOOX is read only: `find` lists files and `exec-out cat` streams bytes to the Mac.
    """
    result = {}
    wanted = sorted({x.lower() for x in doc_ids if x and re.fullmatch(r'[0-9a-fA-F]{32}', x)})
    for docid in wanted:
        root = '/sdcard/.ksync/point/' + docid
        try:
            listing = adb_text(adb, 'shell', 'find', root, '-type', 'f', timeout=30)
        except Exception:
            continue
        for remote in listing.splitlines():
            remote = remote.strip()
            # Refuse to read anything that escaped the exact document directory.
            if not remote.startswith(root + '/'):
                continue
            try:
                cp = subprocess.run([adb, 'exec-out', 'cat', remote], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=60, check=False)
                if cp.returncode != 0 or not cp.stdout:
                    continue
                result.setdefault(docid, []).append({
                    'name': os.path.basename(remote), 'source_path': remote,
                    'bytes': cp.stdout, 'summary': point_file_summary(cp.stdout)
                })
            except Exception:
                continue
    return result


class FrankenDialog(QDialog):
    def __init__(self, gui):
        super().__init__(gui)
        self.gui = gui
        self.db = gui.current_db.new_api
        self.boox_rows = []
        self.stats_rows = []
        self.annotation_rows = []
        self.bookmark_rows = []
        self.readernote_dbs = {}       # key (doc_id or md5) -> {'bytes','name','parsed'}
        self.point_files = {}          # doc_id -> list of {'name','source_path','bytes','summary'}
        self.matches = {}
        self.source_zip = None
        self.supplemental_sources = []
        self.collection_source = None
        self.book_file_check = None
        self.excluded_book_rows = []
        self.collection_serial = None

        self.setWindowTitle('NeoMatrix — v1.4.10')
        self.resize(1180, 720)
        lay = QVBoxLayout(self)
        note = QLabel(
            '<b>NeoMatrix v1.4.10</b><br>'
            'Close your books, connect BOOX by USB, then press <b>Sync</b>. '
            'Sync keeps your farthest reading position when it can match the book. '
            'You’ll be asked before positions are sent to BOOX.'
        )
        note.setWordWrap(True)
        lay.addWidget(note)

        buttons = QHBoxLayout()
        self.collect_btn = QPushButton('Sync')
        self.collect_btn.setMinimumHeight(36)
        self.collect_btn.setDefault(True)
        self.archive_btn = QPushButton('Archive again')
        self.archive_btn.setEnabled(False)
        self.collect_btn.clicked.connect(self.sync_direct)
        self.archive_btn.clicked.connect(self.archive)
        buttons.addWidget(self.collect_btn)
        self.tools_btn = QPushButton('More…')
        self.tools_btn.setMaximumWidth(140)
        self.tools_menu = QMenu(self)
        self.tools_menu.addAction('Sync details', self.reader_report)
        self.tools_menu.addAction('Saved reading data', self.browse_archive)
        self.tools_menu.addAction('BOOX cleanup…', self.storage_cleanup)
        self.tools_menu.addAction('Restore a reading-position backup…', self.restore_reading_position)
        self.tools_menu.addSeparator()
        advanced = self.tools_menu.addMenu('Advanced')
        advanced.addAction('Integrate saved data (all books)', self.integrate_saved)
        advanced.addAction('Use BOOX reading position (selected books)', self.use_boox_position)
        advanced.addAction('Preview reading positions (selected books)', self.push_calibre_position)
        advanced.addAction('Export selected book for diagnosis', self.export_reader_diagnosis)
        self.tools_btn.setMenu(self.tools_menu)
        buttons.addWidget(self.tools_btn)
        lay.addLayout(buttons)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            'BOOX title', 'Author', 'Progress', 'Notes', 'Bookmarks', 'Handwriting',
            'Calibre match', 'Match'
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(False)
        widths = [240, 130, 80, 60, 75, 105, 250, 120]
        for col, width in enumerate(widths):
            self.table.setColumnWidth(col, width)
        lay.addWidget(self.table)

        self.summary = QLabel('Connect BOOX, then press Sync.')
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        close_btn = QPushButton('Close'); close_btn.clicked.connect(self.accept); lay.addWidget(close_btn)

    def accept(self):
        if not getattr(self, '_sync_busy', False):
            super().accept()

    def reject(self):
        if not getattr(self, '_sync_busy', False):
            super().reject()

    def closeEvent(self, event):
        if getattr(self, '_sync_busy', False):
            event.ignore()
        else:
            super().closeEvent(event)

    def storage_cleanup(self):
        adb = find_adb()
        if not adb:
            QMessageBox.warning(self, 'BOOX storage cleanup',
                                'Android Platform Tools (ADB) was not found. Install it and add adb to your system path.')
            return
        from calibre_plugins.frankenboox.cleanup_ui import CleanupDialog
        CleanupDialog(self, adb, settings=prefs).exec()

    def sync_direct(self):
        """Normal-user workflow: collect safely, then archive automatically.

        Collection reads BOOX; position pushes require the separate send choice.
        """
        if not self.integration_ready():
            return
        self.collection_source = None
        self.collection_warning = None
        self.integration_errors = 0
        self.archive_btn.setEnabled(False)
        self.collect_direct()
        if self.collection_source != 'direct-adb' or not self.archive_btn.isEnabled():
            return

        # Transaction preflight. A sync is committed only after the entire
        # collection passes validation. This prevents a partial ReaderNote
        # backup from downgrading an already-richer Calibre archive.
        problems = []
        point_count = sum(len(v) for v in self.point_files.values())
        shape_count = 0
        for rn in self.readernote_dbs.values():
            parsed = rn.get('parsed') or {}
            if parsed.get('integrity') not in (None, 'ok'):
                problems.append(f'ReaderNote database integrity check returned {parsed.get("integrity")!r}.')
            shape_count += len([x for x in (parsed.get('shape_rows') or [])
                                if x.get('status') in (None, 0, '0')])

        # Validate handwriting book-by-book, not just globally. One good
        # ReaderNote DB must never hide a failed backup for another book.
        for m in self.matches.values():
            if m.get('book_id') is None:
                continue
            br = m['boox']
            pts = self._points_for(br)
            rn = self._readernote_for(br)
            if pts and not rn:
                problems.append(
                    f'{br.get("title") or br.get("name") or "Untitled"}: '
                    f'{len(pts)} handwriting data file(s) were found, but the ReaderNote metadata database was not collected.'
                )

        if point_count and shape_count == 0:
            problems.append(
                f'{point_count} handwriting data file(s) were collected, but zero ReaderNote shapes were available.'
            )

        if problems:
            QMessageBox.warning(
                self, 'NeoMatrix — Sync stopped safely',
                'Sync wasn’t completed because NeoMatrix could not safely collect all handwriting information.\n\n'
                'Nothing was changed in Calibre. Your existing saved reading data is safe.\n\n'
                'Technical details:\n' + '\n'.join('• ' + p for p in problems[:8])
                + ('\n• …' if len(problems) > 8 else '')
            )
            self.summary.setText(
                '<b>Sync stopped safely.</b> Collection was incomplete; nothing was written to Calibre. '
                'Your existing saved reading data is untouched.'
            )
            return

        try:
            pending, unchanged = self._pending_archive_entries()
        except ValueError as error:
            QMessageBox.warning(self, 'NeoMatrix — Sync needs attention', str(error))
            self.summary.setText('<b>Sync stopped.</b> ' + str(error) + ' Nothing was written to Calibre.')
            return
        from calibre_plugins.frankenboox.sync_flow import prepare_sync, execute_pushes, default_backup_directory
        entries = combine_book_entries([self._archive_payload(m) for m in self.matches.values() if m.get('book_id') is not None])
        try:
            review = run_background(self, 'Comparing reading positions in Calibre and BOOX…',
                                    lambda status: prepare_sync(self, entries, status))
        except Exception as error:
            self.summary.setText('<b>Sync stopped.</b> Could not compare reading positions: ' + _html(str(error)))
            return
        pending_ids = {entry[0] for entry in pending}
        restore_entries = [entry for entry in entries if entry[0] in review.get('pull', ()) and entry[0] not in pending_ids]
        pending += restore_entries
        unchanged = max(0, unchanged - len(restore_entries))
        send = False
        if review['plans']:
            from calibre_plugins.frankenboox.push_ui import confirm_sync_pushes
            send = confirm_sync_pushes(self, review['plans'], review['issues'])
            if send is None:
                self.summary.setText('<b>Sync cancelled.</b> Nothing was changed in Calibre or BOOX.')
                return
        if not pending and not send:
            resume_issues = self._resume_sync_issues(entries)
            self.summary.setText(
                f'<b>Sync finished.</b> <b>{unchanged}</b> book archives already up to date; nothing rewritten.' +
                self._sync_issue_summary(entries, resume_issues + review['issues']) +
                ('<br>Farther Calibre positions were kept; nothing sent to BOOX.' if review['plans'] else ''))
            return
        done = self.archive(
            skip_confirm=True,
            force_boox_position=False,
            keep_boox_position=False,
            prepared=pending,
            position_choices=review['keep'],
        ) if pending else 0
        if done is not None:
            outcomes = []
            if send and not getattr(self, 'integration_errors', 0):
                outcomes = run_background(self, 'Sending confirmed reading positions to BOOX…',
                    lambda status: execute_pushes(self.db, review['device'], review['plans'], default_backup_directory(), status))
            pushed = sum(x['status'] == 'written' for x in outcomes)
            failures = [x['title'] + ': ' + x['reason'] for x in outcomes if x['status'] == 'failed']
            if send and getattr(self, 'integration_errors', 0):
                failures.append('Calibre updates had errors; no positions were sent to BOOX.')
            resume_issues = self._resume_sync_issues(entries)
            self.summary.setText(
                f'<b>Sync finished.</b> Saved BOOX data for <b>{done} book(s)</b>; <b>{unchanged}</b> already up to date; '
                f'<b>{pushed}</b> position(s) sent to BOOX.' +
                self._sync_issue_summary(entries, resume_issues + review['issues'] + failures) +
                ('<br>Reopen and close the pushed books on BOOX before the next Sync. Backups: ' + _html(str(default_backup_directory())) if pushed else '') +
                ''
            )
            if getattr(self, 'collection_warning', None):
                self.summary.setText(self.summary.text() +
                    ' Fresh handwriting metadata was unavailable; only validated preserved handwriting was used.')

    def collect_direct(self):
        # A failed or cancelled read must not reuse a previous collection.
        self.archive_btn.setEnabled(False)
        self.collection_source = None
        self.book_file_check, self.excluded_book_rows, self.collection_serial = None, [], None
        self.last_sync_issues = []
        adb = find_adb()
        if not adb:
            QMessageBox.critical(self, 'NeoMatrix',
                'ADB was not found. NeoMatrix needs Android Platform Tools (ADB) to communicate with the connected BOOX.\\n\\n'
                'Nothing was changed on the BOOX.')
            return
        try:
            if ApprovalDialog(self, adb).exec() != QDialog.DialogCode.Accepted:
                return
            def collect(status):
                serial = adb_text(adb, 'get-serialno', timeout=20).strip()
                if not serial or serial == 'unknown':
                    raise RuntimeError('The connected BOOX could not be identified.')
                rows = []
                for label, uri in (
                    ('Reading BOOX library…', 'content://com.onyx.content.database.ContentProvider/Metadata'),
                    ('Reading statistics…', 'content://com.onyx.kreader.statistics.provider/OnyxStatisticsModel'),
                    ('Reading annotations…', 'content://com.onyx.content.database.ContentProvider/Annotation'),
                    ('Reading bookmarks…', 'content://com.onyx.content.database.ContentProvider/Bookmark'),
                ):
                    status(label)
                    if uri.endswith('/Metadata'):
                        rows.append(read_metadata_exact(lambda command: adb_text(adb, 'shell', command, timeout=90)))
                    else:
                        rows.append(rows_from_text(adb_text(adb, 'shell', 'content', 'query', '--uri', uri, timeout=90)))
                if not rows[0]:
                    raise RuntimeError('NeoReader Metadata returned no rows.')
                status('Checking current BOOX book files…')
                eligible, excluded = exclude_saved_records(
                    rows[0], serial, (prefs['excluded_book_records'] or {}).get(serial, ()))
                file_check = check_book_files(
                    eligible, lambda command: adb_text(adb, 'shell', command, timeout=90))
                rows[0] = file_check.kept
                status('Reading handwriting files…')
                points = pull_point_files(adb, [boox_doc_id(r) for r in rows[0] if boox_doc_id(r)])
                status('Reading handwriting metadata. If BOOX asks, approve the backup with the password blank.')
                try:
                    dbs, route = collect_readernote_dbs(adb)
                    error = None
                except Exception as e:
                    dbs, error = {}, str(e)
                if adb_text(adb, 'get-serialno', timeout=20).strip() != serial:
                    raise RuntimeError('The connected device changed; collect BOOX data again.')
                return rows, points, dbs, error, serial, file_check, excluded

            rows, points, dbs, rn_error, serial, file_check, excluded = run_background(self, 'Reading BOOX data…', collect)
            self.boox_rows, self.stats_rows, self.annotation_rows, self.bookmark_rows = rows
            self.point_files, self.readernote_dbs, self.matches = points, dbs, {}
            self.collection_warning = rn_error
            self.collection_source = 'direct-adb'
            self.book_file_check, self.excluded_book_rows, self.collection_serial = file_check, excluded, serial
            self.source_zip = None
            self.supplemental_sources = ['direct BOOX collection']
            self._match_books()
            carried = self._carry_forward_unchanged_handwriting()
            if carried:
                self.supplemental_sources.append(f'{carried} unchanged handwriting archive fallback(s)')
            self._populate_table()
            self.summary.setText(self.summary.text() + self._book_file_summary())
            self.archive_btn.setEnabled(any(v.get('book_id') is not None for v in self.matches.values()))
        except Exception as e:
            self.summary.setText('<b>BOOX data could not be collected.</b> Nothing was synced.')
            QMessageBox.critical(self, 'NeoMatrix', 'Could not collect from the connected BOOX:\n\n' + str(e))

    def analyze(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose BOOX diagnostic ZIP', os.path.expanduser('~/Desktop'), 'ZIP archives (*.zip)')
        if not path:
            return
        try:
            with zipfile.ZipFile(path, 'r') as zf:
                meta_text = read_member_text(zf, ['/metadata.txt', 'metadata.txt'])
                stats_text = read_member_text(zf, ['/reading-statistics.txt', 'reading-statistics.txt'])
                ann_text = read_member_text(zf, ['/provider-probes/annotation.txt', '/annotation.txt', 'annotation.txt'])
                bm_text = read_member_text(zf, ['/provider-probes/bookmark.txt', '/bookmark-main-provider.txt', 'bookmark-main-provider.txt'])
            if not meta_text:
                raise ValueError('Metadata.txt/metadata.txt was not found inside this ZIP.')
            self.boox_rows = rows_from_text(meta_text)
            self.stats_rows = rows_from_text(stats_text)
            self.annotation_rows = rows_from_text(ann_text)
            self.bookmark_rows = rows_from_text(bm_text)
            self.source_zip = path
            self.collection_source = 'diagnostic-zip'
            self.book_file_check, self.excluded_book_rows, self.collection_serial = None, [], None
            self._match_books()
            self._populate_table()
            self.archive_btn.setEnabled(any(v.get('book_id') is not None for v in self.matches.values()))
        except Exception as e:
            QMessageBox.critical(self, 'NeoMatrix', 'Could not analyze the diagnostic ZIP:\n\n' + str(e))

    def load_supplemental(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Choose supplemental BOOX ZIP(s)', os.path.expanduser('~/Desktop'), 'ZIP archives (*.zip)'
        )
        if not paths:
            return
        loaded_bm = loaded_db = loaded_points = 0
        errors = []
        for path in paths:
            try:
                with zipfile.ZipFile(path, 'r') as zf:
                    bm_text = read_member_text(zf, ['/bookmark-main-provider.txt', 'bookmark-main-provider.txt', '/bookmark.txt'])
                    if bm_text:
                        rows = rows_from_text(bm_text)
                        if rows:
                            self.bookmark_rows.extend(rows)
                            loaded_bm += len(rows)

                    # ReaderNote databases from Android backup extractor.
                    for name in zf.namelist():
                        if name.startswith('__MACOSX/') or name.endswith('/'):
                            continue
                        base = os.path.basename(name)
                        m = RN_DB_RE.search(base)
                        if m:
                            key = m.group(1).lower()
                            data = zf.read(name)
                            self.readernote_dbs[key] = {
                                'name': base, 'bytes': data, 'parsed': sqlite_rows_from_bytes(data)
                            }
                            loaded_db += 1

                    # Exact/generic point extractors provide manifest.tsv with original source path.
                    manifest_name = next((n for n in zf.namelist() if n.lower().endswith('manifest.tsv') and not n.startswith('__MACOSX/')), None)
                    manifest_rows = []
                    if manifest_name:
                        for line in zf.read(manifest_name).decode('utf-8', 'replace').splitlines():
                            parts = line.split('\t')
                            if len(parts) >= 5 and parts[0] == 'OK':
                                manifest_rows.append(parts)
                    all_names = zf.namelist()
                    for parts in manifest_rows:
                        source_path = parts[3]
                        dm = DOC_IN_POINT_PATH_RE.search(source_path)
                        if not dm:
                            continue
                        docid = dm.group(1).lower()
                        source_base = os.path.basename(parts[4])
                        candidates = [n for n in all_names if not n.startswith('__MACOSX/') and os.path.basename(n) == source_base]
                        if not candidates:
                            continue
                        member = candidates[0]
                        data = zf.read(member)
                        self.point_files.setdefault(docid, []).append({
                            'name': source_base,
                            'source_path': source_path,
                            'bytes': data,
                            'summary': point_file_summary(data),
                        })
                        loaded_points += 1
                self.supplemental_sources.append(os.path.basename(path))
            except Exception as e:
                errors.append(os.path.basename(path) + ': ' + str(e))

        self._populate_table()
        msg = f'Loaded {loaded_bm} bookmark rows, {loaded_db} ReaderNote databases, and {loaded_points} handwriting point files.'
        if errors:
            msg += '\n\nErrors:\n' + '\n'.join(errors[:8])
            QMessageBox.warning(self, 'NeoMatrix', msg)
        else:
            QMessageBox.information(self, 'NeoMatrix', msg)

    def _calibre_records(self):
        recs = []
        for book_id in sorted(self.db.all_book_ids()):
            title = self.db.field_for('title', book_id) or ''
            authors = self.db.field_for('authors', book_id) or ()
            if isinstance(authors, str):
                authors = [authors]
            fmts = self.db.formats(book_id) or ()
            recs.append({'id': int(book_id), 'title': title, 'authors': list(authors),
                         'formats': tuple(str(x).lower() for x in fmts),
                         'ntitle': norm(title), 'nauth': norm(' & '.join(authors)),
                         'volumes': volume_identity(title, *[os.path.basename(self.db.format_abspath(book_id, str(fmt).upper()) or '') for fmt in fmts])})
        return recs

    def _md5_for_format(self, book_id, fmt):
        try:
            f = self.db.format(book_id, fmt.upper(), as_file=True)
            if f is None: return None
            h = hashlib.md5()
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk: break
                h.update(chunk)
            try: f.close()
            except Exception: pass
            return h.hexdigest()
        except Exception:
            return None

    def _best_fallback(self, br, recs):
        nt = norm(br.get('title') or os.path.splitext(br.get('name') or '')[0])
        na = norm(br.get('authors') or '')
        fmt = (br.get('type') or '').lower()
        source_volumes = volume_identity(br.get('title'), br.get('name'), br.get('location'))
        scored = []
        for cr in recs:
            if fmt and cr['formats'] and fmt not in cr['formats']: continue
            target_volumes = cr.get('volumes') or volume_identity(cr.get('title'))
            if source_volumes and target_volumes and source_volumes != target_volumes:
                continue
            score = 0
            if nt and nt == cr['ntitle']: score += 70
            elif nt and cr['ntitle'] and (nt in cr['ntitle'] or cr['ntitle'] in nt): score += 40
            if na and cr['nauth']:
                if na == cr['nauth']: score += 25
                elif na in cr['nauth'] or cr['nauth'] in na: score += 15
            if source_volumes and not target_volumes:
                score = min(score, 89)
            if score: scored.append((score, cr))
        scored.sort(key=lambda x: (-x[0], x[1]['id']))
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return min(scored[0][0], 89), scored[0][1]
        return scored[0] if scored else (0, None)

    def _manual_pick(self, br, recs, suggested=None):
        items, seen = [], set()
        if suggested: items.append(suggested); seen.add(suggested['id'])
        nt = norm(br.get('title') or br.get('name') or '')
        for cr in recs:
            if cr['id'] in seen: continue
            if nt and cr['ntitle'] and (nt in cr['ntitle'] or cr['ntitle'] in nt):
                items.append(cr); seen.add(cr['id'])
        for cr in recs:
            if cr['id'] not in seen: items.append(cr)
        dlg = QDialog(self); dlg.setWindowTitle('Match BOOX book to Calibre'); dlg.resize(760, 520)
        lay = QVBoxLayout(dlg)
        lab = QLabel('<b>Uncertain match. NeoMatrix will not guess.</b><br><br>'
                     f"BOOX title: {br.get('title') or br.get('name') or '[missing]'}<br>"
                     f"BOOX author: {br.get('authors') or '[missing]'}<br>"
                     f"Format: {br.get('type') or '[unknown]'}<br><br>"
                     'Choose the correct Calibre book, or leave it unmatched. A manual choice is remembered.')
        lab.setWordWrap(True); lay.addWidget(lab)
        table = QTableWidget(len(items), 3); table.setHorizontalHeaderLabels(['Calibre title','Author','ID'])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, cr in enumerate(items):
            table.setItem(i,0,QTableWidgetItem(cr['title'])); table.setItem(i,1,QTableWidgetItem(' & '.join(cr['authors']))); table.setItem(i,2,QTableWidgetItem(str(cr['id'])))
        table.horizontalHeader().setStretchLastSection(True)
        if items: table.selectRow(0)
        lay.addWidget(table)
        row = QHBoxLayout(); use=QPushButton('Use selected book'); leave=QPushButton('Leave unmatched'); cancel=QPushButton('Cancel analysis')
        row.addWidget(use); row.addWidget(leave); row.addWidget(cancel); lay.addLayout(row)
        result={'id':None,'cancel':False}
        def choose():
            sel=table.selectionModel().selectedRows()
            if sel: result['id']=items[sel[0].row()]['id']; dlg.accept()
        def unmatched(): dlg.accept()
        def cancel_all(): result['cancel']=True; dlg.reject()
        use.clicked.connect(choose); leave.clicked.connect(unmatched); cancel.clicked.connect(cancel_all); dlg.exec(); return result

    def _match_books(self):
        recs = self._calibre_records(); by_id={r['id']:r for r in recs}; manual=dict(prefs['manual_matches'] or {}); self.matches={}
        active_rows=[r for r in self.boox_rows if r.get('lastAccess') not in (None,'','0') or r.get('progress') not in (None,'','0/0') or self._annotations_for(r) or self._bookmarks_for(r)]
        for br in active_rows:
            key=stable_boox_key(br); match={'book_id':None,'method':'unmatched','confidence':0}; remembered=manual.get(key)
            if remembered is not None:
                try:
                    rid=int(remembered)
                    source_volumes = volume_identity(br.get('title'), br.get('name'), br.get('location'))
                    target_volumes = by_id.get(rid, {}).get('volumes')
                    if rid in by_id and not (source_volumes and target_volumes and source_volumes != target_volumes):
                        match={'book_id':rid,'method':'remembered manual match','confidence':100}
                except Exception: pass
            if match['book_id'] is None:
                target_md5=boox_md5(br); fmt=(br.get('type') or '').lower()
                if target_md5 and fmt:
                    try: bsize=int(br.get('size') or 0)
                    except Exception: bsize=0
                    exact = []
                    source_volumes = volume_identity(br.get('title'), br.get('name'), br.get('location'))
                    for cr in recs:
                        if fmt not in cr['formats']: continue
                        if source_volumes and cr.get('volumes') and source_volumes != cr['volumes']: continue
                        try:
                            md=self.db.format_metadata(cr['id'],fmt.upper(),allow_cache=True); csize=int((md or {}).get('size') or 0)
                        except Exception: csize=0
                        if bsize and csize and bsize != csize: continue
                        if self._md5_for_format(cr['id'],fmt)==target_md5:
                            exact.append(cr['id'])
                    if len(exact) == 1:
                        match={'book_id':exact[0],'method':'exact file MD5','confidence':100}
            if match['book_id'] is None:
                score,cr=self._best_fallback(br,recs)
                if cr is not None and score>=90: match={'book_id':cr['id'],'method':'exact title + author','confidence':score}
                else:
                    picked=self._manual_pick(br,recs,suggested=cr)
                    if picked['cancel']: raise RuntimeError('Analysis cancelled by user.')
                    if picked['id'] is not None:
                        match={'book_id':picked['id'],'method':'manual match','confidence':100}; manual[key]=int(picked['id'])
            self.matches[key]={'boox':br,**match}
        prefs['manual_matches']=manual

    def _carry_forward_unchanged_handwriting(self):
        """Safely reuse archived ReaderNote metadata only when handwriting bytes are unchanged.

        Android's legacy local-backup route can occasionally return no ReaderNote DBs on BOOX.
        For a book that already has a NeoMatrix archive, we may still update progress/highlights/
        bookmarks if every currently collected #points file is byte-for-byte identical to the
        archived set. In that case the preserved raw ReaderNote DB is the correct companion for
        those unchanged strokes. Any new, removed, renamed, or changed point file disables this
        fallback and the normal transaction gate stops Sync instead.
        """
        recovered = 0
        for m in self.matches.values():
            book_id = m.get('book_id')
            if book_id is None:
                continue
            br = m['boox']
            pts = self._points_for(br)
            if not pts or self._readernote_for(br):
                continue
            try:
                extras = self.db.list_extra_files(int(book_id), use_cache=False)
            except Exception:
                continue
            rels = {getattr(x, 'relpath', ''): getattr(x, 'file_path', None) for x in extras}
            state, _ = self._archived_state_for_book(book_id)
            source_states = (state or {}).get('source_states')
            if source_states:
                # Each BOOX document needs its own unchanged handwriting set.
                # Another copy's strokes must never satisfy this fallback.
                doc_id = (boox_doc_id(br) or '').lower()
                source = next((item for item in source_states
                               if str((item.get('boox_identity') or {}).get('document_id') or '').lower() == doc_id), None)
                if source is None:
                    continue
                rels = {original: rels.get(preserved) for original, preserved in source.get('archive_files', {}).items()}
            archived = {}
            for rel, path in rels.items():
                rp = rel.replace('\\', '/')
                if not (rp.startswith('data/neomatrix/handwriting/') and path):
                    continue
                base = os.path.basename(rp)
                base = re.sub(r'^\d{3}_', '', base)
                try:
                    with open(path, 'rb') as f:
                        archived[base] = hashlib.sha256(f.read()).hexdigest()
                except Exception:
                    archived = {}; break
            current = {p['name']: hashlib.sha256(p['bytes']).hexdigest() for p in pts}
            if not archived or archived != current:
                continue

            rn_candidates = [(rel, path) for rel, path in rels.items()
                             if rel.replace('\\', '/').startswith('data/neomatrix/readernote/')
                             and rel.lower().endswith('.db') and path]
            doc_id = (boox_doc_id(br) or '').lower()
            chosen = None
            for rel, path in rn_candidates:
                if doc_id and doc_id in os.path.basename(rel).lower():
                    chosen = (rel, path); break
            if chosen is None and len(rn_candidates) == 1:
                chosen = rn_candidates[0]
            if chosen is None:
                continue
            try:
                with open(chosen[1], 'rb') as f:
                    data = f.read()
                parsed = sqlite_rows_from_bytes(data)
                if parsed.get('integrity') not in (None, 'ok'):
                    continue
                if not (parsed.get('shape_rows') or parsed.get('document_rows')):
                    continue
                key = doc_id or (RN_DB_RE.search(os.path.basename(chosen[0])).group(1).lower()
                                 if RN_DB_RE.search(os.path.basename(chosen[0])) else '')
                if not key:
                    continue
                self.readernote_dbs[key] = {
                    'name': os.path.basename(chosen[0]), 'bytes': data, 'parsed': parsed,
                    'carried_forward_from_archive': True,
                }
                recovered += 1
            except Exception:
                continue
        return recovered

    def _annotations_for(self, br):
        doc_id=boox_doc_id(br)
        return [a for a in self.annotation_rows if doc_id and a.get('idString')==doc_id]

    def _bookmarks_for(self, br):
        doc_id=boox_doc_id(br)
        return [b for b in self.bookmark_rows if doc_id and b.get('idString')==doc_id]

    def _stats_for(self, br):
        md5=boox_md5(br); docid=boox_doc_id(br); out=[]
        for r in self.stats_rows:
            if (md5 and r.get('md5')==md5) or (docid and r.get('docId')==docid): out.append(r)
        return out

    def _readernote_for(self, br):
        docid=(boox_doc_id(br) or '').lower(); md5=(boox_md5(br) or '').lower()
        return self.readernote_dbs.get(docid) or self.readernote_dbs.get(md5)

    def _points_for(self, br):
        return self.point_files.get((boox_doc_id(br) or '').lower(), [])

    def _resume_candidate_count(self):
        return sum(
            1 for match in self.matches.values()
            if match.get('book_id') is not None and boox_resume_candidate(match.get('boox') or {})
        )

    def _choose_resume_policy(self, candidate_count, operation='sync'):
        """Ask once per operation how to handle existing Calibre positions.

        Returns ``apply`` to overwrite mapped Calibre positions, ``keep`` to
        preserve them regardless of timestamps, ``timestamp`` when there is
        no candidate position to ask about, or ``None`` to cancel.
        """
        if not candidate_count:
            return 'timestamp'
        box = QMessageBox(self)
        box.setWindowTitle('NeoMatrix — reading-position choice')
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f'NeoMatrix found structured BOOX reading positions for {candidate_count} matched book(s).'
        )
        box.setInformativeText(
            'Choose once for this operation. Apply is recommended while testing; it overwrites '
            'mapped Calibre positions. Keep preserves existing Calibre positions.'
        )
        apply_btn = box.addButton('Apply BOOX positions (recommended)', QMessageBox.ButtonRole.AcceptRole)
        keep_btn = box.addButton('Keep Calibre positions', QMessageBox.ButtonRole.RejectRole)
        cancel_label = 'Cancel sync' if operation == 'sync' else 'Cancel integration'
        cancel_btn = box.addButton(cancel_label, QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(apply_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is apply_btn:
            return 'apply'
        if clicked is keep_btn:
            return 'keep'
        return None

    def _populate_table(self):
        self.table.setRowCount(0); matched=manual=unmatched=ann_count=bm_count=shape_count=point_count=0
        for _,m in self.matches.items():
            br=m['boox']; anns=self._annotations_for(br); active_anns=[a for a in anns if a.get('status') in (None,'0',0)]
            bms=self._bookmarks_for(br); active_bms=[b for b in bms if b.get('status') in (None,'0',0)]
            rn=self._readernote_for(br); shapes=[]
            if rn: shapes=[s for s in rn['parsed'].get('shape_rows',[]) if s.get('status') in (None,0,'0')]
            pts=self._points_for(br)
            ann_count+=len(active_anns); bm_count+=len(active_bms); shape_count+=len(shapes); point_count+=len(pts)
            row=self.table.rowCount(); self.table.insertRow(row)
            cur,total=parse_progress(br.get('progress')); prog=br.get('progress') or ''
            if cur is not None and total: prog=f'{cur}/{total} ({(100.0*cur/total):.1f}%)'
            book_id=m.get('book_id')
            if book_id is not None:
                ct=self.db.field_for('title',book_id) or ''; ca=self.db.field_for('authors',book_id) or (); cdesc=f'{ct} — {" & ".join(ca)} [Calibre #{book_id}]'; matched+=1
                if 'manual' in m.get('method',''): manual+=1
            else: cdesc='[UNMATCHED]'; unmatched+=1
            match_label = m.get('method') or ''
            confidence = m.get('confidence')
            if confidence:
                match_label = f'{match_label} ({confidence}%)'
            vals=[br.get('title') or br.get('name') or '', br.get('authors') or '', prog,
                  str(len(active_anns)), str(len(active_bms)),
                  f'{len(shapes)} shapes / {len(pts)} points', cdesc, match_label]
            for col,val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                item.setToolTip(str(val))
                self.table.setItem(row,col,item)
        supp = ', '.join(self.supplemental_sources) if self.supplemental_sources else 'direct collection not run yet'
        self.summary.setText(f'<b>{matched} matched</b> ({manual} manually confirmed), <b>{unmatched} unmatched</b>. '
                             f'Linked: <b>{ann_count} active annotations</b>, <b>{bm_count} bookmarks</b>, <b>{shape_count} ReaderNote shapes</b>, <b>{point_count} raw point files</b>.<br>'
                             f'Collection source: {supp}. Nothing has been written to Calibre yet.')

    def _archived_state_for_book(self, book_id):
        """Return archived NeoMatrix state JSON and its path, without modifying Calibre."""
        try:
            extras = self.db.list_extra_files(int(book_id), use_cache=False)
        except Exception:
            return None, None
        preferred = ('data/neomatrix/boox-neoreader-state.json', 'data/boox-neoreader-state.json')
        by_rel = {getattr(x, 'relpath', ''): getattr(x, 'file_path', None) for x in extras}
        for rel in preferred:
            path = by_rel.get(rel)
            if not path:
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f), path
            except Exception:
                continue
        return None, None

    def push_calibre_position(self):
        ids = list(self.gui.library_view.get_selected_ids())
        if not ids:
            self.summary.setText('Select books in the main Calibre library, then reopen NeoMatrix.')
            return
        adb = find_adb()
        if not adb:
            self.summary.setText('Android Platform Tools (ADB) was not found.')
            return
        from calibre_plugins.frankenboox.push_ui import PositionPushDialog
        PositionPushDialog(self, adb, ids).exec()

    def restore_reading_position(self):
        adb = find_adb()
        if not adb:
            self.summary.setText('Android Platform Tools (ADB) was not found.')
            return
        from calibre_plugins.frankenboox.push_ui import restore_position_backup
        restore_position_backup(self, adb)

    def use_boox_position(self):
        from calibre_plugins.frankenboox.reader_bridge import integrate_reader
        ids = list(self.gui.library_view.get_selected_ids())
        if not ids:
            self.summary.setText('Select the book in the main Calibre library, then reopen NeoMatrix.')
            return
        outcomes = []
        for book_id in ids:
            title = self.db.field_for('title', book_id) or str(book_id)
            state, path = self._archived_state_for_book(book_id)
            if state is None:
                outcomes.append(f'{title}: no saved BOOX data. Run Sync first.')
                continue
            try:
                report = run_background(self, f'Applying BOOX position: {title}',
                    lambda status: integrate_reader(self.db, book_id, state, use_boox_position=True))
                if report.get('resume_applied'):
                    outcomes.append(f'{title}: BOOX position applied. Close and reopen the ebook viewer.')
                else:
                    outcomes.append(f'{title}: position could not be applied. Export this book for diagnosis.')
            except Exception as error:
                outcomes.append(f'{title}: {error}')
        self.summary.setText('\n'.join(outcomes))

    def export_reader_diagnosis(self):
        ids = list(self.gui.library_view.get_selected_ids())
        if len(ids) != 1:
            self.summary.setText('Select one book in the main Calibre library, then reopen NeoMatrix.')
            return
        book_id = ids[0]
        state, source = self._archived_state_for_book(book_id)
        if state is None:
            self.summary.setText('No saved BOOX data for this book. Run Sync first.')
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Save book and BOOX reading data for diagnosis',
            os.path.expanduser('~/Desktop/NeoMatrix-reader-diagnosis.zip'), 'ZIP archives (*.zip)')
        if not path:
            return
        def export(status):
            from calibre_plugins.frankenboox.reader_bridge import REPORT_KEY
            source_format=str((state.get('boox_book') or {}).get('format') or '').upper()
            fmt=source_format if source_format in {'PDF','CBZ','CBR'} else 'EPUB'
            epub = self.db.format_abspath(book_id, fmt)
            if not epub:
                raise ValueError('The selected book has no matching '+fmt+' format.')
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as bundle:
                bundle.write(epub, 'book.'+fmt.lower())
                bundle.writestr('boox-neoreader-state.json', json.dumps(state, ensure_ascii=False, indent=2))
                report = self.db.get_custom_book_data(REPORT_KEY).get(book_id, {})
                bundle.writestr('reader-report.json', json.dumps(report, ensure_ascii=False, indent=2))
        try:
            run_background(self, 'Saving reader diagnosis ZIP…', export)
            self.summary.setText('Saved diagnosis ZIP containing this book and its BOOX reading data. You can attach it to the chat.')
        except Exception as error:
            self.summary.setText(f'Could not export reader diagnosis: {error}')

    def reader_report(self):
        from calibre_plugins.frankenboox.reader_bridge import REPORT_KEY
        reports = self.db.get_custom_book_data(REPORT_KEY)
        lines = ['Latest sync'] + getattr(self, 'last_sync_issues', [])
        check = getattr(self, 'book_file_check', None)
        if check is not None:
            lines += ['', 'Book file checks']
            for label, rows in (('Old recorded path is missing; history kept', check.missing),
                                ('Excluded from sync; history kept', self.excluded_book_rows),
                                ('File could not be checked; record kept', check.uncertain)):
                lines.extend(str(row.get('title') or row.get('name') or 'Untitled book') + ': ' + label for row in rows)
            lines.extend(check.warnings)
        current = {match['book_id'] for match in self.matches.values() if match.get('book_id') is not None}
        if self.collection_source == 'direct-adb':
            sections = [('Current books', [(bid, report) for bid, report in sorted(reports.items()) if bid in current]),
                        ('Saved history from other books — not part of this sync', [(bid, report) for bid, report in sorted(reports.items()) if bid not in current])]
        else:
            sections = [('Saved reading positions', sorted(reports.items()))]
        for heading, items in sections:
            if not items:
                continue
            lines += ['', heading]
            for book_id, report in items:
                title = self.db.field_for('title', book_id) or str(book_id)
                position = 'Updated in Calibre' if report.get('resume_applied') else ('Existing Calibre position kept' if report.get('resume') else 'Reading position could not be synced')
                if not report.get('resume'):
                    position = {'image-only-start':'Cover or title page; Calibre position kept',
                                'different-formats':'Different formats; Calibre position kept'}.get(report.get('resume_issue_kind'), position)
                lines.append(str(title) + ': ' + position + '; ' + str(report.get('mapped',0)) + ' note(s) imported')
                lines.extend('  '+str(x) for x in report.get('skipped',[]))
        dlg = QDialog(self); dlg.setWindowTitle('NeoMatrix — Sync details'); dlg.resize(850,600)
        layout = QVBoxLayout(dlg); text = QTextEdit(); text.setReadOnly(True)
        text.setPlainText('\n'.join(lines) or 'No sync has run yet.')
        layout.addWidget(text); close = QPushButton('Close'); close.clicked.connect(dlg.accept); layout.addWidget(close)
        dlg.exec()

    def integration_ready(self):
        try:
            ensure_columns(self.db)
            return True
        except RestartRequired as e:
            QMessageBox.information(self, 'NeoMatrix — restart Calibre', str(e))
        except Exception as e:
            QMessageBox.warning(self, 'NeoMatrix — columns unavailable', str(e))
        return False

    def integrate_saved(self):
        if not self.integration_ready():
            return
        done = 0
        errors = []
        saved = []
        for book_id in sorted(self.db.all_book_ids()):
            state, path = self._archived_state_for_book(book_id)
            if state is None:
                continue
            saved.append((book_id, state))
        candidate_count = sum(1 for _, state in saved if state_resume_candidate(state))
        position_policy = self._choose_resume_policy(candidate_count, operation='integration')
        if position_policy is None:
            self.summary.setText('<b>Integration cancelled.</b> Nothing was written to Calibre.')
            return
        for book_id, state in saved:
            try:
                run_background(self, f'Updating Calibre book #{book_id}…',
                               lambda status: integrate(
                                   self.db, book_id, state,
                                   use_boox_position=position_policy == 'apply',
                                   keep_boox_position=position_policy == 'keep'))
                done += 1
            except Exception as e:
                errors.append(f'Book #{book_id}: {e}')
        self.gui.library_view.model().refresh()
        message = f'Integrated {done} books into Calibre BOOX columns and reading notes.'
        if errors:
            QMessageBox.warning(self, 'NeoMatrix — some books failed', message + '\n\n' + '\n'.join(errors[:10]))
        else:
            position_message = (
                'BOOX reading positions were applied.'
                if position_policy == 'apply' else
                'Existing Calibre reading positions were preserved.'
            )
            QMessageBox.information(self, 'NeoMatrix', message + '\n\n' + position_message + ' Close and reopen the ebook viewer to load mapped locations. Reader integration report lists mapped and unsupported items.')

    def browse_archive(self):
        dlg = ArchiveBrowserDialog(self, self.db)
        dlg.exec()

    def _archive_payload(self, m):
        source_name = os.path.basename(self.source_zip or '')
        br=m['boox']; book_id=int(m['book_id']); cur,total=parse_progress(br.get('progress'))
        extra=tolerant_json(br.get('extraAttributes')); backend=extra.get('backend') if isinstance(extra,dict) else {}
        if not isinstance(backend,dict): backend={}
        raw_anns=self._annotations_for(br); active_anns=[a for a in raw_anns if a.get('status') in (None,'0',0)]
        raw_bms=self._bookmarks_for(br); active_bms=[b for b in raw_bms if b.get('status') in (None,'0',0)]
        rn=self._readernote_for(br); pts=self._points_for(br)
        rn_parsed=rn['parsed'] if rn else None
        state={
            'schema':'neomatrix-neoreader-state-v0.7.1-beta',
            'imported_at_utc':datetime.now(timezone.utc).isoformat(),
            'sources':{'diagnostic':source_name,'supplemental':list(self.supplemental_sources)},
            'safety':{'boox_writes_performed':False,'ebook_files_modified':False,'bibliographic_metadata_modified':False,'calibre_native_annotations_modified':False},
            'match':{'method':m.get('method'),'confidence':m.get('confidence'),'calibre_book_id':book_id},
            'boox_identity':{'document_id':boox_doc_id(br),'file_md5':boox_md5(br),'metadata_uuid':br.get('uuid'),'path':br.get('location')},
            'boox_book':{'name':br.get('name'),'title':br.get('title'),'authors':br.get('authors'),'publisher':br.get('publisher'),'language':br.get('language'),'isbn':br.get('ISBN'),'format':br.get('type'),'size':_int_or_none(br.get('size')),'favorite':_int_or_none(br.get('favorite')),'reading_status':_int_or_none(br.get('readingStatus'))},
            'progress':{
                'raw':br.get('progress'),'current_page':cur,'total_pages':total,
                'last_access_ms':_int_or_none(br.get('lastAccess')),'last_access_utc':iso_from_ms(br.get('lastAccess')),
                'backend_current_page_raw':backend.get('current_page'),
                'backend_current_page':tolerant_json(backend.get('current_page')),
                'current_page_position_v2_raw':backend.get('current_page_position_v2'),
                'current_page_position_v2':tolerant_json(backend.get('current_page_position_v2')),
                'current_page_position_v3_raw':backend.get('current_page_position_v3'),
                'current_page_position_v3':tolerant_json(backend.get('current_page_position_v3')),
                'layout_type':backend.get('layout_type'),'total_page_from_backend':backend.get('total_page')},
            'annotations':{'active_count':len(active_anns),'all_rows_count':len(raw_anns),'active':[normalized_annotation(a) for a in active_anns],'raw_rows':raw_anns},
            'bookmarks':{'active_count':len(active_bms),'all_rows_count':len(raw_bms),'active':[normalized_bookmark(b) for b in active_bms],'raw_rows':raw_bms},
            'reader_note':rn_parsed,
            'handwriting_point_files':[{'name':p['name'],'source_path':p['source_path'],'summary':p['summary']} for p in pts],
            'reading_statistics':self._stats_for(br),
            'raw_metadata_row':br,
        }
        files={'data/neomatrix/boox-neoreader-state.json':io.BytesIO(json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))}
        # Compatibility path: replace old v0.3 companion with the richer v0.4 state too.
        files['data/boox-neoreader-state.json']=io.BytesIO(json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
        if rn:
            files['data/neomatrix/readernote/'+rn['name']]=io.BytesIO(rn['bytes'])
        try:
            old_names = [x.relpath for x in self.db.list_extra_files(book_id, use_cache=False)]
        except Exception:
            old_names = []
        for idx,p in enumerate(sorted(pts, key=lambda p: (p['name'], p['source_path']))):
            clean=re.sub(r'[^A-Za-z0-9._#-]+','_',p['name'])
            rel = f'data/neomatrix/handwriting/{idx:03d}_{clean}'
            previous = [name for name in old_names if name.startswith('data/neomatrix/handwriting/')
                        and re.sub(r'^\d{3}_', '', name.rsplit('/', 1)[-1]) == clean]
            if len(previous) == 1:
                rel = previous[0]
            files[rel] = io.BytesIO(p['bytes'])
        return book_id, state, files

    @staticmethod
    def _reader_sync_complete(report):
        if (not isinstance(report, dict) or not {'mapped', 'resume', 'skipped'}.issubset(report)
                or not isinstance(report.get('skipped'), list)):
            return False
        failures = (
            'Reader integration failed:',
            'Calibre viewer resume sidecar could not be updated:',
            'Viewer resume sidecar was updated, but Calibre position index was not:',
        )
        return not any(str(item).startswith(failures) for item in report.get('skipped', []))

    def _archive_is_current(self, book_id, state, files, saved, report, anchors, sync_status):
        """Skip only a complete archive/integration of the same source data."""
        from calibre_plugins.frankenboox.integration import prepare
        try:
            signature = state_signature(state)
            if sync_status is not None and (
                    not isinstance(sync_status, dict) or not sync_status.get('complete') or
                    sync_status.get('signature') != signature):
                return False
            archived, _ = self._archived_state_for_book(book_id)
            if state_signature(archived) != signature or state_signature(saved) != signature:
                return False
            if not self._reader_sync_complete(report):
                return False
            # Validate values from the actual saved snapshot, so provider row reordering
            # does not reorder otherwise identical reading notes.
            values, _ = prepare(saved)
            if any(self.db.field_for('#' + key, book_id) != value for key, value in values.items()):
                return False
            extras = {x.relpath: x.file_path for x in self.db.list_extra_files(book_id, use_cache=False)}
            for rel, stream in files.items():
                path = extras.get(rel)
                if not path:
                    return False
                content = Path(path).read_bytes()
                if rel in ('data/neomatrix/boox-neoreader-state.json', 'data/boox-neoreader-state.json'):
                    if state_signature(json.loads(content)) != signature:
                        return False
                elif content != stream.getvalue():
                    return False
            source_format=str((state.get('boox_book') or {}).get('format') or '').upper()
            if source_format in {'PDF','CBZ','CBR'}:
                fixed_path=self.db.format_abspath(book_id,source_format)
                if fixed_path:
                    from calibre_plugins.frankenboox.fixed_sync import FIXED_KEY
                    from calibre_plugins.frankenboox.fixed_book import digest_file
                    context=(self.db.get_custom_book_data(FIXED_KEY).get(book_id) or {}).get(source_format) or {}
                    from calibre_plugins.frankenboox.reader_bridge import READER_MAPPING_VERSION
                    return bool(report.get('format')==source_format and report.get('mapping_version')==READER_MAPPING_VERSION
                        and context.get('calibre_sha256')==digest_file(fixed_path)
                        and context.get('identity')==(state.get('boox_identity') or {}))
                elif report.get('format')==source_format:
                    from calibre_plugins.frankenboox.reader_bridge import READER_MAPPING_VERSION
                    return bool(report.get('mapping_version')==READER_MAPPING_VERSION and
                                report.get('resume_issue_kind')=='missing-matching-format')
            epub = self.db.format_abspath(book_id, 'EPUB')
            if epub:
                from calibre_plugins.frankenboox.reader_bridge import READER_MAPPING_VERSION
                if report.get('mapping_version') != READER_MAPPING_VERSION:
                    return False
                if not isinstance(anchors, dict):
                    return False
                from calibre_plugins.frankenboox.epub_identity import reading_digest, IDENTITY_SCHEMA
                if anchors.get('epub_reading_sha256'):
                    if (anchors.get('epub_identity_schema') != IDENTITY_SCHEMA or
                            anchors['epub_reading_sha256'] != reading_digest(epub)):
                        return False
                else:
                    digest = hashlib.sha256()
                    with open(epub, 'rb') as f:
                        for chunk in iter(lambda: f.read(1024 * 1024), b''):
                            digest.update(chunk)
                    if anchors.get('epub_sha256') != digest.hexdigest():
                        return False
            elif 'Native reader integration currently supports EPUB only' not in report.get('skipped', []):
                return False
            return True
        except Exception:
            # Missing, corrupt or incomplete saved data is eligible for repair.
            return False

    def _book_file_summary(self):
        check = getattr(self, 'book_file_check', None)
        if check is None:
            return ''
        messages = []
        if check.missing:
            messages.append(str(len(check.missing)) + ' old BOOX entries skipped; saved history kept.')
        excluded = getattr(self, 'excluded_book_rows', ())
        if excluded:
            messages.append(str(len(excluded)) + ' selected records excluded from sync.')
        if check.uncertain:
            messages.append(str(len(check.uncertain)) + ' book files could not be checked; entries kept. See Sync details.')
        return '<br>' + ' '.join(messages) if messages else ''

    def _sync_issue_summary(self, entries, issues):
        self.last_sync_issues = list(dict.fromkeys(str(issue) for issue in issues))
        if not self.last_sync_issues:
            return self._book_file_summary()
        affected = set()
        for book_id, state, _ in entries:
            title = str(self.db.field_for('title', book_id) or (state.get('boox_book') or {}).get('title') or 'Book ' + str(book_id))
            if any(issue.startswith(title + ':') for issue in self.last_sync_issues):
                affected.add(book_id)
        label = (str(len(affected)) + (' book needs attention.' if len(affected) == 1 else ' books need attention.')) if affected else 'Sync needs attention.'
        return '<br><b>' + label + '</b> See More… → Sync details.' + self._book_file_summary()

    def _resume_sync_issues(self, entries):
        """Surface unresolved saved positions even when source data is unchanged."""
        from calibre_plugins.frankenboox.reader_bridge import REPORT_KEY
        reports = self.db.get_custom_book_data(REPORT_KEY)
        issues = []
        for book_id, state, _ in entries:
            report = reports.get(book_id) or {}
            if not state_resume_candidate(state) or report.get('resume'):
                continue
            title = self.db.field_for('title', book_id) or (state.get('boox_book') or {}).get('title') or 'Book ' + str(book_id)
            fmt=str((state.get('boox_book') or {}).get('format') or '').upper()
            fmt=fmt if fmt in {'PDF','CBZ','CBR'} else 'EPUB'
            if not self.db.format_abspath(book_id, fmt):
                issues.append(str(title) + ': Calibre has no matching '+fmt+' file for reading-position conversion.')
            else:
                reason = report.get('resume_issue') or 'BOOX reading position is unverified; Calibre position was kept. Reader handoff may use an older position.'
                issues.append(str(title) + ': ' + reason)
        return issues

    def _pending_archive_entries(self):
        from calibre_plugins.frankenboox.integration import DATA_KEY
        from calibre_plugins.frankenboox.reader_bridge import ANCHOR_KEY, REPORT_KEY
        entries = combine_book_entries([self._archive_payload(m) for m in self.matches.values() if m.get('book_id') is not None])
        from calibre_plugins.frankenboox.library_push import ensure_pull_ready
        for book_id, state, files in entries:
            ensure_pull_ready(self.db, book_id, state)
        try:
            saved = self.db.get_custom_book_data(DATA_KEY)
            reports = self.db.get_custom_book_data(REPORT_KEY)
            anchors = self.db.get_custom_book_data(ANCHOR_KEY)
            statuses = self.db.get_custom_book_data(SYNC_KEY)
        except Exception:
            return entries, 0
        pending = [entry for entry in entries if not self._archive_is_current(
            *entry, saved.get(entry[0]), reports.get(entry[0]), anchors.get(entry[0]), statuses.get(entry[0]))]
        return pending, len(entries) - len(pending)

    def _archive_files_to_write(self, book_id, files):
        """Preserve the bytes and modification times of unchanged companion files."""
        try:
            extras = {x.relpath: x.file_path for x in self.db.list_extra_files(book_id, use_cache=False)}
        except Exception:
            return files
        changed = {}
        for rel, stream in files.items():
            try:
                current = Path(extras[rel]).read_bytes()
                incoming = stream.getvalue()
                same = current == incoming
                if rel in ('data/neomatrix/boox-neoreader-state.json', 'data/boox-neoreader-state.json'):
                    same = state_signature(json.loads(current)) == state_signature(json.loads(incoming))
                if same:
                    continue
            except Exception:
                pass
            changed[rel] = stream
        return changed

    def archive(self, skip_confirm=False, force_boox_position=False, keep_boox_position=False, prepared=None, position_choices=None):
        matched=[m for m in self.matches.values() if m.get('book_id') is not None]
        if not matched:
            QMessageBox.information(self,'NeoMatrix','There are no matched books to archive.'); return 0
        if not skip_confirm:
            ans=QMessageBox.question(self,'Archive NeoReader state into Calibre',
                'NeoMatrix will replace/update its own companion files under data/neomatrix/ for matched Calibre books.\n\n'
                'It may include state JSON, raw ReaderNote database, and raw handwriting point files when those supplemental sources were loaded.\n\n'
                'It updates NeoMatrix BOOX columns and reading notes in Calibre. EPUB/PDF files, native reader annotations, and BOOX remain unchanged.\n\n'
                f'Proceed for {len(matched)} books?')
            if ans!=QMessageBox.StandardButton.Yes: return None
        if not self.integration_ready(): return None
        done=0; errors=[]
        try:
            entries = prepared if prepared is not None else combine_book_entries([self._archive_payload(m) for m in matched])
        except ValueError as error:
            QMessageBox.warning(self, 'NeoMatrix — check book matches', str(error))
            return 0
        for entry in entries:
            book_id, state, files = entry
            try:
                def save_book(status):
                    # A failed write must remain retryable even when an older report survives.
                    signature = state_signature(state)
                    self.db.add_custom_book_data(SYNC_KEY, {book_id: {'complete': False, 'signature': signature}}, delete_first=False)
                    changed_files = self._archive_files_to_write(book_id, files)
                    if changed_files:
                        self.db.add_extra_files(book_id, changed_files, replace=True, auto_rename=False)
                    report = integrate(
                        self.db, book_id, state,
                        use_boox_position=force_boox_position,
                        keep_boox_position=keep_boox_position or bool((position_choices or {}).get(book_id)))
                    if self._reader_sync_complete(report):
                        self.db.add_custom_book_data(SYNC_KEY, {book_id: {'complete': True, 'signature': signature}}, delete_first=False)
                    return report
                run_background(self, f'Updating Calibre book #{book_id}…', save_book)
                done+=1
            except Exception as e:
                errors.append(f'Calibre #{book_id}: {e}')
        self.integration_errors = len(errors)
        self.gui.library_view.model().refresh()
        if errors:
            QMessageBox.warning(self,'NeoMatrix',f'Integrated reading data for {done} books, but {len(errors)} failed:\n\n'+'\n'.join(errors[:10]))
        elif not skip_confirm:
            QMessageBox.information(self,'NeoMatrix',f'Saved and integrated NeoReader data for {done} Calibre books.\n\nBOOX was never written to. Ebook files and bibliographic metadata were not changed.')
        return done



def decode_point_strokes(data):
    """Decode the observed NeoReader external #points representation.

    Raw bytes remain canonical. This decoder is deliberately read-only and
    bounded to the format validated against the archived BOOX samples.
    """
    try:
        matches=list(UUID_RE.finditer(data))
        footer_start=matches[1].start()-4 if len(matches)>1 else len(data)
        words=[data[i:i+4] for i in range(80,footer_start,4)]
        pts=[]
        for j in range(len(words)-3):
            x=struct.unpack('>f',words[j])[0]; y=struct.unpack('>f',words[j+1])[0]
            stamp=struct.unpack('>I',words[j+2])[0]; elapsed=struct.unpack('>I',words[j+3])[0]
            if 0 <= x <= 10000 and 0 <= y <= 10000 and 0xC0 <= (stamp >> 24) <= 0xFF and elapsed < 10000000:
                pts.append((x,y,elapsed))
        if not pts: return []
        strokes=[]; cur=[]; prev=None
        for x,y,elapsed in pts:
            if prev is not None and elapsed < prev and cur:
                strokes.append(cur); cur=[]
            cur.append((x,y)); prev=elapsed
        if cur: strokes.append(cur)
        return strokes
    except Exception:
        return []


class DuchessDeSadeRenderer(QWidget):
    """Small read-only renderer for archived NeoReader pen strokes.

    Internal codename/memorial only; the public UI deliberately calls this
    simply Handwriting. Duchess forks. Duchess modifies. Duchess spreads.
    """
    def __init__(self, parent=None):
        super().__init__(parent); self.strokes=[]
        self.setMinimumSize(300,220)

    def set_strokes(self, strokes):
        self.strokes=strokes or []; self.update()

    def paintEvent(self, event):
        p=QPainter(self)
        try:
            p.fillRect(self.rect(), QColor('white'))
            if not self.strokes:
                p.setPen(QPen(QColor('gray'),1)); p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), 'No decodable strokes')
                return
            allpts=[pt for stroke in self.strokes for pt in stroke]
            xs=[x for x,_ in allpts]; ys=[y for _,y in allpts]
            minx,maxx=min(xs),max(xs); miny,maxy=min(ys),max(ys)
            dx=max(maxx-minx,1.0); dy=max(maxy-miny,1.0)
            margin=18.0; aw=max(self.width()-2*margin,1.0); ah=max(self.height()-2*margin,1.0)
            scale=min(aw/dx,ah/dy)
            ox=(self.width()-dx*scale)/2.0; oy=(self.height()-dy*scale)/2.0
            pen=QPen(QColor('black'),2.2); pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin); p.setPen(pen)
            for stroke in self.strokes:
                if len(stroke)==1:
                    x,y=stroke[0]; p.drawPoint(int(ox+(x-minx)*scale),int(oy+(y-miny)*scale)); continue
                for a,b in zip(stroke,stroke[1:]):
                    x1,y1=a; x2,y2=b
                    p.drawLine(int(ox+(x1-minx)*scale),int(oy+(y1-miny)*scale),int(ox+(x2-minx)*scale),int(oy+(y2-miny)*scale))
        finally:
            p.end()

class ArchiveBrowserDialog(QDialog):
    """Human-readable view of NeoMatrix state already archived in Calibre."""
    def __init__(self, parent, db):
        super().__init__(parent)
        self.db = db
        self.rows = []
        self.setWindowTitle('NeoMatrix — Calibre reading archive')
        self.resize(1120, 720)
        lay = QVBoxLayout(self)
        intro = QLabel('<b>Archived NeoReader reading state</b><br>'
                       'This view reads NeoMatrix companion files stored with your Calibre books. '
                       '<b>Nothing is written to Calibre or the BOOX.</b> Double-click a book for details.')
        intro.setWordWrap(True); lay.addWidget(intro)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(['Title','Author','Progress','Notes','Bookmarks','Handwriting','Last read','Calibre ID'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self.open_selected)
        hdr = self.table.horizontalHeader()
        try:
            hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            for c in range(2, 8): hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        except Exception:
            pass
        lay.addWidget(self.table)
        row = QHBoxLayout()
        self.open_btn = QPushButton('Open selected book details')
        close = QPushButton('Close')
        self.open_btn.clicked.connect(self.open_selected); close.clicked.connect(self.accept)
        row.addWidget(self.open_btn); row.addStretch(1); row.addWidget(close); lay.addLayout(row)
        self.status = QLabel(''); self.status.setWordWrap(True); lay.addWidget(self.status)
        self.load_rows()

    def _load_state(self, book_id):
        try: extras = self.db.list_extra_files(int(book_id), use_cache=False)
        except Exception: return None, None
        rels = {getattr(x,'relpath',''): getattr(x,'file_path',None) for x in extras}
        for rel in ('data/neomatrix/boox-neoreader-state.json','data/boox-neoreader-state.json'):
            path = rels.get(rel)
            if path:
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        state = json.load(f)

                    # Recovery/read fallback: Beta 2 could replace state JSON with
                    # reader_note=null after an incomplete Android-backup run.
                    # The raw ReaderNote DB companion was not deleted. If present,
                    # parse it in memory and restore the browser's view only.
                    if not state.get('reader_note'):
                        rn_candidates = [
                            fp for rp, fp in rels.items()
                            if rp.startswith('data/neomatrix/readernote/')
                            and rp.lower().endswith('.db') and fp
                        ]
                        for rn_path in rn_candidates:
                            try:
                                with open(rn_path, 'rb') as rf:
                                    parsed = sqlite_rows_from_bytes(rf.read())
                                if parsed and (parsed.get('shape_rows') or parsed.get('document_rows')):
                                    state['reader_note'] = parsed
                                    state['_reader_note_recovered_from_raw_archive'] = True
                                    break
                            except Exception:
                                continue
                    return state, path
                except Exception:
                    pass
        return None, None

    def load_rows(self):
        self.rows = []; self.table.setRowCount(0)
        for book_id in sorted(self.db.all_book_ids()):
            state, path = self._load_state(book_id)
            if not state: continue
            title = self.db.field_for('title', book_id) or state.get('boox_book',{}).get('title') or ''
            authors = self.db.field_for('authors', book_id) or ()
            if isinstance(authors, str): authors = [authors]
            progress = state.get('progress') or {}
            cur, total = progress.get('current_page'), progress.get('total_pages')
            rawp = progress.get('raw') or ''
            if cur is not None and total:
                try: ptxt = f'{cur}/{total} ({100.0*float(cur)/float(total):.1f}%)'
                except Exception: ptxt = rawp
            else: ptxt = rawp
            anns = state.get('annotations') or {}; bms = state.get('bookmarks') or {}
            rn = state.get('reader_note') or {}; shapes = rn.get('shape_rows') or []
            active_shapes = [x for x in shapes if x.get('status') in (None,0,'0')]
            points = state.get('handwriting_point_files') or []
            last = progress.get('last_access_utc') or ''
            if 'T' in last: last = last.replace('T',' ')[:16] + ' UTC'
            vals = [title, ' & '.join(authors), ptxt, str(anns.get('active_count', len(anns.get('active') or []))),
                    str(bms.get('active_count', len(bms.get('active') or []))),
                    f'{len(active_shapes)} shapes / {len(points)} files', last, str(book_id)]
            r=self.table.rowCount(); self.table.insertRow(r)
            for c,v in enumerate(vals):
                it=QTableWidgetItem(str(v)); it.setToolTip(str(v)); self.table.setItem(r,c,it)
            self.rows.append((int(book_id), state, path))
        recovered = sum(1 for _, st, _ in self.rows if st.get('_reader_note_recovered_from_raw_archive'))
        extra = f' <b>{recovered}</b> archive(s) recovered ReaderNote metadata from preserved raw DB files.' if recovered else ''
        self.status.setText(f'<b>{len(self.rows)} books</b> currently have a NeoMatrix archive in this Calibre library.' + extra)
        if self.rows: self.table.selectRow(0)

    def open_selected(self, *args):
        sel=self.table.selectionModel().selectedRows()
        if not sel: return
        idx=sel[0].row()
        if idx < 0 or idx >= len(self.rows): return
        book_id,state,path=self.rows[idx]
        BookStateDialog(self, self.db, book_id, state, path).exec()


class BookStateDialog(QDialog):
    def __init__(self, parent, db, book_id, state, source_path):
        super().__init__(parent)
        self.db=db; self.book_id=book_id; self.state=state
        title=db.field_for('title',book_id) or state.get('boox_book',{}).get('title') or 'Book'
        self.setWindowTitle(f'NeoMatrix — {title}')
        self.resize(1000, 720)
        lay=QVBoxLayout(self)
        tabs=QTabWidget(); lay.addWidget(tabs)
        tabs.addTab(self._overview(title), 'Overview')
        tabs.addTab(self._annotations(), 'Highlights & Notes')
        tabs.addTab(self._bookmarks(), 'Bookmarks')
        tabs.addTab(self._handwriting(), 'Handwriting')
        tabs.addTab(self._raw(), 'Technical details')
        close=QPushButton('Close'); close.clicked.connect(self.accept); lay.addWidget(close)

    def _overview(self,title):
        w=QWidget(); l=QVBoxLayout(w)
        p=self.state.get('progress') or {}; anns=self.state.get('annotations') or {}; bms=self.state.get('bookmarks') or {}
        rn=self.state.get('reader_note') or {}; shapes=rn.get('shape_rows') or []; points=self.state.get('handwriting_point_files') or []
        active_shapes=[x for x in shapes if x.get('status') in (None,0,'0')]
        cur,total=p.get('current_page'),p.get('total_pages')
        pct=''
        if cur is not None and total:
            try: pct=f' — {100.0*float(cur)/float(total):.1f}%'
            except Exception: pass
        ident=self.state.get('boox_identity') or {}
        html=(f'<h2>{_html(title)}</h2>'
              f'<p><b>Progress:</b> {_html(str(p.get("raw") or "—"))}{_html(pct)}<br>'
              f'<b>Last read:</b> {_html(str(p.get("last_access_utc") or "—"))}<br>'
              f'<b>Highlights/notes:</b> {anns.get("active_count",len(anns.get("active") or []))}<br>'
              f'<b>Bookmarks:</b> {bms.get("active_count",len(bms.get("active") or []))}<br>'
              f'<b>Handwriting:</b> {len(active_shapes)} shapes in {len(points)} point files</p>'
              f'<p><b>BOOX document ID:</b> {_html(str(ident.get("document_id") or "—"))}<br>'
              f'<b>File MD5:</b> {_html(str(ident.get("file_md5") or "—"))}<br>'
              f'<b>Archived:</b> {_html(str(self.state.get("imported_at_utc") or "—"))}</p>')
        lab=QLabel(html); lab.setWordWrap(True)
        l.addWidget(lab); l.addStretch(1); return w

    def _annotations(self):
        w=QWidget(); l=QVBoxLayout(w); arr=(self.state.get('annotations') or {}).get('active') or []
        t=QTableWidget(len(arr),5); t.setHorizontalHeaderLabels(['Quote','Note','Chapter','Page','Created'])
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for r,a in enumerate(arr):
            vals=[a.get('quote') or '',a.get('note') or '',a.get('chapter') or '',a.get('page_number_display') or '',a.get('created_at_utc') or '']
            for c,v in enumerate(vals): it=QTableWidgetItem(str(v)); it.setToolTip(str(v)); t.setItem(r,c,it)
        try:
            h=t.horizontalHeader(); h.setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch); h.setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
            for c in (2,3,4): h.setSectionResizeMode(c,QHeaderView.ResizeMode.ResizeToContents)
        except Exception: pass
        l.addWidget(t); l.addWidget(QLabel(f'{len(arr)} active highlight/note record(s).')); return w

    def _bookmarks(self):
        w=QWidget(); l=QVBoxLayout(w); arr=(self.state.get('bookmarks') or {}).get('active') or []
        t=QTableWidget(len(arr),4); t.setHorizontalHeaderLabels(['Quote / title','Page','Position','Created'])
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for r,b in enumerate(arr):
            q=b.get('quote') or b.get('title') or ''; pg=b.get('page_number_zero_based'); pg='' if pg is None else pg+1
            vals=[q,pg,b.get('position_int_v3') or b.get('position_int_v2') or b.get('position_int') or '',b.get('created_at_utc') or '']
            for c,v in enumerate(vals): it=QTableWidgetItem(str(v)); it.setToolTip(str(v)); t.setItem(r,c,it)
        try:
            h=t.horizontalHeader(); h.setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
            for c in (1,2,3): h.setSectionResizeMode(c,QHeaderView.ResizeMode.ResizeToContents)
        except Exception: pass
        l.addWidget(t); l.addWidget(QLabel(f'{len(arr)} active bookmark(s).')); return w

    def _handwriting(self):
        w=QWidget(); l=QVBoxLayout(w); rn=self.state.get('reader_note') or {}; shapes=rn.get('shape_rows') or []
        active=[x for x in shapes if x.get('status') in (None,0,'0')]; pts=self.state.get('handwriting_point_files') or []
        lab=QLabel(f'<b>Archived handwriting</b><br><br>'
                   f'<b>{len(active)} handwriting shapes</b> linked with <b>{len(pts)} raw pen-data files</b>.<br>'
                   'Select an item below to view the handwriting saved from NeoReader. '
                   'Viewing is read-only; NeoMatrix keeps the original pen data unchanged.')
        lab.setWordWrap(True); l.addWidget(lab)
        t=QTableWidget(len(pts),5); t.setHorizontalHeaderLabels(['Handwriting data','Bytes','Pen points','Strokes','Linked shapes'])
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for r,p in enumerate(pts):
            sm=p.get('summary') or {}; vals=[p.get('name') or '',sm.get('byte_length') or '',sm.get('decoded_point_records') or '',sm.get('decoded_stroke_count') or '',len(sm.get('shape_uuids') or [])]
            for c,v in enumerate(vals): it=QTableWidgetItem(str(v)); it.setToolTip(str(v)); t.setItem(r,c,it)
        try:
            h=t.horizontalHeader(); h.setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
            for c in (1,2,3,4): h.setSectionResizeMode(c,QHeaderView.ResizeMode.ResizeToContents)
        except Exception: pass
        l.addWidget(t)
        self.hw_canvas=DuchessDeSadeRenderer()
        self.hw_canvas.setMinimumHeight(280)
        l.addWidget(self.hw_canvas, 1)
        self.hw_info=QLabel('Select a handwriting item above.')
        self.hw_info.setWordWrap(True); l.addWidget(self.hw_info)

        def selected(*args):
            rows=t.selectionModel().selectedRows()
            if not rows: return
            r=rows[0].row()
            if r < 0 or r >= len(pts): return
            pinfo=pts[r]; raw=self._load_archived_point_bytes(pinfo.get('name') or '')
            if not raw:
                self.hw_canvas.set_strokes([])
                self.hw_info.setText('<b>Could not open this handwriting data.</b> The saved raw data remains untouched.')
                return
            strokes=decode_point_strokes(raw)
            self.hw_canvas.set_strokes(strokes)
            sm=pinfo.get('summary') or {}; shape_ids=set(sm.get('shape_uuids') or [])
            linked=[]
            for sh in active:
                vals={str(v).lower() for v in sh.values() if isinstance(v,str)}
                if any(uid.lower() in vals for uid in shape_ids): linked.append(sh)
            page=(pinfo.get('name') or '').split('#',1)[0]
            anchor_html = _duchess_anchor_summary(linked[0]) if linked else ''
            if not anchor_html:
                anchor_html = '<i>Semantic anchor preserved in Raw Archive.</i>'
            self.hw_info.setText(
                f'<b>Page-position group {_html(page)}</b> — {len(strokes)} rendered stroke(s), '
                f'{sum(len(x) for x in strokes)} decoded points, {len(linked)} linked ReaderNote shape(s).'
                f'<br>{anchor_html}<br><small>Full ReaderNote anchor data remains preserved in Raw Archive.</small>'
            )

        t.itemSelectionChanged.connect(selected)
        if pts:
            t.selectRow(0); selected()
        return w

    def _load_archived_point_bytes(self, logical_name):
        try: extras=self.db.list_extra_files(int(self.book_id), use_cache=False)
        except Exception: return None
        # Archive filenames are prefixed 000_, 001_, ...; match the original
        # point filename recorded in state, then fall back to its page/UUID tail.
        for x in extras:
            rel=getattr(x,'relpath','') or ''; path=getattr(x,'file_path',None)
            if not path or '/handwriting/' not in ('/'+rel.replace('\\','/')): continue
            base=os.path.basename(rel)
            if base.endswith(logical_name) or logical_name in base:
                try:
                    with open(path,'rb') as f: return f.read()
                except Exception: return None
        return None

    def _raw(self):
        w=QWidget(); l=QVBoxLayout(w); text=QTextEdit(); text.setReadOnly(True)
        text.setPlainText(json.dumps(self.state,ensure_ascii=False,indent=2)); l.addWidget(text); return w



def _duchess_anchor_summary(shape):
    """Return concise, human-readable EPUB anchor information.

    Raw ReaderNote values remain untouched in the archive/Raw Archive tab.
    """
    anchor = tolerant_json(shape.get('anchor'))
    pos = tolerant_json(shape.get('position'))
    page = tolerant_json(shape.get('pagePositionV3')) or tolerant_json(shape.get('pagePositionV2'))

    quote = ''
    if isinstance(anchor, dict):
        quote = anchor.get('quote') or ''

    def first(key):
        for obj in (pos, page):
            if isinstance(obj, dict) and obj.get(key) not in (None, ''):
                return obj.get(key)
        if shape.get(key) not in (None, ''):
            return shape.get(key)
        return None

    chapter = first('chapterIndex')
    chapter_id = first('chapterID')
    node = first('nodePosition')
    offset = first('textOffset')
    bookpos = shape.get('bookPosition')

    parts = []
    if quote:
        parts.append(f'<b>Nearby text:</b> “{_html(quote)}”')
    loc = []
    if chapter not in (None, ''): loc.append(f'chapter {chapter}')
    if chapter_id not in (None, ''): loc.append(f'ID {_html(chapter_id)}')
    if node not in (None, ''): loc.append(f'node {node}')
    if offset not in (None, ''): loc.append(f'offset {offset}')
    if bookpos not in (None, ''): loc.append(f'book position {bookpos}')
    if loc:
        parts.append('<b>EPUB anchor:</b> ' + ' · '.join(map(str, loc)))
    return '<br>'.join(parts)

def _html(v):
    return (str(v).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;'))
