"""Exercise the actual sync methods with a write-counting disposable library."""
import ast
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import sqlite3
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

ROOT = Path(__file__).parent
for name in ('calibre_plugins', 'calibre_plugins.frankenboox'):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]
    sys.modules[name] = package
def load(name):
    full = 'calibre_plugins.frankenboox.' + name
    spec = importlib.util.spec_from_file_location(full, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module
signature_module = load('sync_state')
integration = load('integration')
reader = ModuleType('calibre_plugins.frankenboox.reader_bridge')
reader.ANCHOR_KEY = 'neomatrix_portable_anchors_v1'
reader.REPORT_KEY = 'neomatrix_reader_report_v1'
reader.READER_MAPPING_VERSION = 3
sys.modules[reader.__name__] = reader

tree = ast.parse((ROOT / 'main.py').read_text())
dialog = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'FrankenDialog')
method_names = ('sync_direct', '_archive_payload', '_reader_sync_complete', '_archive_is_current',
                '_pending_archive_entries', '_sync_issue_summary', '_resume_sync_issues', '_archive_files_to_write', 'archive',
                '_archived_state_for_book', '_annotations_for', '_bookmarks_for', '_stats_for',
                '_readernote_for', '_points_for', '_best_fallback', '_carry_forward_unchanged_handwriting')
definitions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
for n in dialog.body:
    if isinstance(n, ast.FunctionDef) and n.name in method_names:
        n.decorator_list = []
        definitions.append(n)
environment = dict(globals(), state_signature=signature_module.state_signature, combine_book_entries=signature_module.combine_book_entries, volume_identity=signature_module.volume_identity,
                   SYNC_KEY='neomatrix_sync_status_v1')
environment['run_background'] = lambda parent, title, work: work(lambda status: None)
messages = []
environment['QMessageBox'] = SimpleNamespace(
    warning=lambda *args: messages.append(args), information=lambda *args: messages.append(args))
exec(compile(ast.Module(body=definitions, type_ignores=[]), str(ROOT / 'main.py'), 'exec'), environment)


class FakeDB:
    def __init__(self, root):
        self.root, self.columns, self.data, self.events, self.epubs = root, {}, {}, [], {}

    def field_for(self, field, book):
        return self.columns.get((book, field))

    def set_field(self, field, values):
        for book, value in values.items():
            self.events.append(('column', book, field))
            self.columns[book, field] = copy.deepcopy(value)

    def get_custom_book_data(self, key):
        return copy.deepcopy(self.data.get(key, {}))

    def add_custom_book_data(self, key, values, delete_first=False):
        for book, value in values.items():
            self.events.append(('record', book, key))
            self.data.setdefault(key, {})[book] = copy.deepcopy(value)

    def list_extra_files(self, book, use_cache=False):
        folder = self.root / str(book)
        return [SimpleNamespace(relpath=str(p.relative_to(folder)), file_path=str(p))
                for p in folder.rglob('*') if p.is_file()] if folder.exists() else []

    def add_extra_files(self, book, files, **kwargs):
        for name, stream in files.items():
            self.events.append(('file', book, name))
            path = self.root / str(book) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(stream.getvalue())

    def format_abspath(self, book, fmt):
        return self.epubs.get(book)


class Button:
    def __init__(self):
        self.enabled = True
    def setEnabled(self, value):
        self.enabled = value
    def isEnabled(self):
        return self.enabled


class Harness:
    for _name in method_names:
        if _name != '_reader_sync_complete':
            locals()[_name] = environment[_name]
    _reader_sync_complete = staticmethod(environment['_reader_sync_complete'])

    def __init__(self, db):
        self.db, self.archive_btn = db, Button()
        self.summary = SimpleNamespace(setText=lambda text: setattr(self, 'summary_text', text), text=lambda: self.summary_text)
        self.summary_text = ''
        self.source_zip, self.supplemental_sources = None, ['direct BOOX collection']
        self.annotation_rows, self.bookmark_rows, self.stats_rows = [], [], []
        self.point_files, self.readernote_dbs = {}, {}
        self.matches = {}
        for book in (1, 2):
            doc = str(book) * 32
            row = {'title': 'Book ' + str(book), 'name': 'book.pdf', 'type': 'pdf', 'progress': '10/100',
                   'lastAccess': '1000000', 'extraAttributes': json.dumps({'backend': {'doc_id': doc}})}
            self.matches[doc] = {'book_id': book, 'boox': row, 'method': 'exact', 'confidence': 100}
        self.choices = []
        self.gui = SimpleNamespace(library_view=SimpleNamespace(model=lambda: SimpleNamespace(refresh=lambda: None)))
        self.collection_warning = None
        self.fail_reader = False
        environment['integrate'] = self.integrate

    def integration_ready(self):
        return True

    def collect_direct(self):
        self.collection_source = 'direct-adb'
        self.archive_btn.setEnabled(True)

    def _choose_resume_policy(self, count):
        self.choices.append(count)
        return 'timestamp'

    def integrate(self, db, book, state, **kwargs):
        integration.integrate(db, book, state)
        db.events.append(('reader', book, None))
        if self.fail_reader:
            raise RuntimeError('simulated reader failure')
        if db.format_abspath(book, 'EPUB'):
            digest = hashlib.sha256(Path(db.epubs[book]).read_bytes()).hexdigest()
            db.add_custom_book_data(reader.ANCHOR_KEY, {book: {'epub_sha256': digest}}, delete_first=False)
            report = {'mapping_version':reader.READER_MAPPING_VERSION, 'mapped': 0, 'resume': False, 'skipped': []}
        else:
            report = {'mapped': 0, 'resume': False, 'skipped': ['Native reader integration currently supports EPUB only']}
        db.add_custom_book_data(reader.REPORT_KEY, {book: report}, delete_first=False)
        return report


class IncrementalSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = FakeDB(Path(self.temp.name))
        self.ui = Harness(self.db)
        self.ui.sync_direct()
        self.db.events.clear()
        self.ui.choices.clear()

    def tearDown(self):
        self.temp.cleanup()

    def add_duplicate(self):
        duplicate = copy.deepcopy(self.ui.matches['1' * 32])
        duplicate['boox']['lastAccess'] = '999999'
        duplicate['boox']['progress'] = '90/100'
        duplicate['boox']['location'] = '/Documents/old.pdf'
        duplicate['boox']['extraAttributes'] = json.dumps({'backend': {'doc_id': 'a' * 32}})
        self.ui.matches['a' * 32] = duplicate
        return duplicate

    def test_duplicate_sources_sync_once_then_zero_writes_even_if_reordered(self):
        self.add_duplicate()
        self.ui.sync_direct()
        self.assertEqual({event[1] for event in self.db.events}, {1})
        self.assertEqual(sum(event[0] == 'reader' for event in self.db.events), 1)
        saved = self.db.data[integration.DATA_KEY][1]
        self.assertEqual(saved['progress']['raw'], '10/100')
        self.assertEqual(len(saved['source_states']), 2)
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.db.root.rglob('*') if p.is_file()}
        for _ in range(3):
            self.ui.matches = dict(reversed(list(self.ui.matches.items())))
            self.db.events.clear()
            self.ui.choices.clear()
            self.ui.sync_direct()
            self.assertEqual(self.db.events, [])
            self.assertEqual(self.ui.choices, [])
            self.assertIn('2</b> book archives already up to date', self.ui.summary_text)
            self.assertEqual(before, {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in before})

    def test_duplicate_source_changes_preserve_other_source_data(self):
        duplicate = self.add_duplicate()
        self.ui.annotation_rows = [{'idString': '1' * 32, 'uuid': 'new', 'quote': 'new copy'},
                                   {'idString': 'a' * 32, 'uuid': 'old', 'quote': 'old copy'}]
        self.ui.readernote_dbs = {
            '1' * 32: {'name': 'ReaderNoteDatabase-new.db', 'bytes': b'new metadata',
                      'parsed': {'integrity': 'ok', 'document_rows': [{'id': 'new'}], 'shape_rows': [{'uuid': 'new shape'}], 'resource_rows': []}},
            'a' * 32: {'name': 'ReaderNoteDatabase-old.db', 'bytes': b'old metadata',
                      'parsed': {'integrity': 'ok', 'document_rows': [{'id': 'old'}], 'shape_rows': [{'uuid': 'old shape'}], 'resource_rows': []}}}
        # The same point-file name is present in two different BOOX documents.
        for doc, data in [('1' * 32, b'new strokes'), ('a' * 32, b'old strokes')]:
            self.ui.point_files[doc] = [{'name': 'same#points', 'source_path': '/' + doc + '/same#points',
                                       'bytes': data, 'summary': {'sha256': hashlib.sha256(data).hexdigest()}}]
        self.ui.sync_direct()
        saved = self.db.data[integration.DATA_KEY][1]
        self.assertEqual({a['quote'] for a in saved['annotations']['active']}, {'new copy', 'old copy'})
        self.assertEqual({r['uuid'] for r in saved['reader_note']['shape_rows']}, {'new shape', 'old shape'})
        self.assertEqual(len(saved['handwriting_point_files']), 2)
        contents = {Path(e.file_path).read_bytes() for e in self.db.list_extra_files(1)}
        self.assertTrue({b'new metadata', b'old metadata', b'new strokes', b'old strokes'}.issubset(contents))
        self.db.events.clear()
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        self.ui.annotation_rows[-1]['note'] = 'changed on the older copy'
        self.ui.sync_direct()
        self.assertEqual({event[1] for event in self.db.events}, {1})
        self.assertEqual(self.db.data[integration.DATA_KEY][1]['progress']['raw'], '10/100')
        self.db.events.clear()
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        duplicate['boox']['lastAccess'] = '2000000'
        self.ui.sync_direct()
        self.assertEqual(self.db.data[integration.DATA_KEY][1]['progress']['raw'], '90/100')
        self.db.events.clear()
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])

    def test_each_duplicate_reuses_only_its_own_unchanged_handwriting(self):
        self.add_duplicate()
        for doc, data in [('1' * 32, b'new strokes'), ('a' * 32, b'old strokes')]:
            path = Path(self.temp.name) / ('ReaderNoteDatabase-' + doc + '.db')
            with sqlite3.connect(path) as con:
                con.execute('create table ReaderNoteDocumentModel(uniqueId text, uuid text)')
                con.execute('insert into ReaderNoteDocumentModel values (?, ?)', (doc, 'document-' + doc))
                con.execute('create table ReaderNoteShapeModel(uuid text, status integer)')
                con.execute('insert into ReaderNoteShapeModel values (?, 0)', ('shape-' + doc,))
            raw = path.read_bytes()
            self.ui.readernote_dbs[doc] = {'name': path.name, 'bytes': raw, 'parsed': environment['sqlite_rows_from_bytes'](raw)}
            self.ui.point_files[doc] = [{'name': 'same#points', 'source_path': '/' + doc + '/same#points',
                                       'bytes': data, 'summary': {'sha256': hashlib.sha256(data).hexdigest()}}]
        self.ui.sync_direct()
        self.db.events.clear()
        self.ui.readernote_dbs = {}
        self.assertEqual(self.ui._carry_forward_unchanged_handwriting(), 2)
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        self.ui.readernote_dbs = {}
        self.ui.point_files['a' * 32][0]['bytes'] = b'changed old strokes'
        self.assertEqual(self.ui._carry_forward_unchanged_handwriting(), 1)
        self.assertNotIn('a' * 32, self.ui.readernote_dbs)
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        self.assertIn('Sync stopped safely', self.ui.summary_text)

    def test_distinct_volume_entries_are_not_combined(self):
        self.ui.matches['1' * 32]['boox']['title'] = 'Encyclopedia, Volume I'
        self.ui.matches['2' * 32]['boox']['title'] = 'Encyclopedia, Volume II'
        self.ui.sync_direct()
        self.assertNotIn('source_states', self.db.data[integration.DATA_KEY][1])
        self.assertNotIn('source_states', self.db.data[integration.DATA_KEY][2])
        self.db.events.clear()
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])

    def test_different_volumes_cannot_be_consolidated_into_one_book(self):
        self.ui.matches['1' * 32]['boox']['title'] = 'Encyclopedia Volume I'
        duplicate = self.add_duplicate()
        duplicate['boox']['title'] = 'Encyclopedia Volume II'
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        self.assertIn('Different BOOX volumes', self.ui.summary_text)

    def test_volume_number_in_filename_disambiguates_generic_titles(self):
        norm = environment['norm']
        records = [{'id': n, 'title': 'Encyclopedia', 'formats': ('epub',),
                    'ntitle': norm('Encyclopedia'), 'nauth': norm('Editors'),
                    'volumes': signature_module.volume_identity('Encyclopedia Volume ' + v)}
                   for n, v in ((1, 'I'), (2, 'II'))]
        row = {'title': 'Encyclopedia', 'authors': 'Editors', 'type': 'epub', 'name': 'Encyclopedia Vol. 2.epub'}
        score, match = self.ui._best_fallback(row, records)
        self.assertEqual(score, 95)
        self.assertEqual(match['id'], 2)
        records[1]['volumes'] = ()
        score, match = self.ui._best_fallback(row, records)
        self.assertLess(score, 90)
        self.assertEqual(match['id'], 2)

    def test_ambiguous_titles_require_a_manual_match(self):
        records = [{'id': n, 'title': 'Encyclopedia', 'formats': ('epub',),
                    'ntitle': 'encyclopedia', 'nauth': 'editors', 'volumes': ()} for n in (1, 2)]
        score, match = self.ui._best_fallback({'title': 'Encyclopedia', 'authors': 'Editors', 'type': 'epub'}, records)
        self.assertLess(score, 90)

    def test_volume_labels_distinguish_numbers_and_ranges(self):
        hint = signature_module.volume_identity
        self.assertEqual(hint('Volume II'), hint('Vol. 2'))
        self.assertNotEqual(hint('Volume I'), hint('Volume II'))
        self.assertNotEqual(hint('Volumes I–III'), hint('Volume I'))
        self.assertEqual(hint('On the Tragic'), ())
        self.assertEqual(hint('The Works / Index Of The Five Volumes'), ())

    def test_duplicate_source_missing_metadata_blocks_all_writes(self):
        self.add_duplicate()
        self.ui.point_files['a' * 32] = [{'name': 'missing#points', 'source_path': '/old', 'bytes': b'old', 'summary': {}}]
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        self.assertIn('Sync stopped safely', self.ui.summary_text)

    def test_unchanged_epub_is_rechecked_once_after_mapper_update(self):
        epub = Path(self.temp.name) / 'book.epub'
        epub.write_bytes(b'unchanged EPUB bytes')
        self.db.epubs[1] = str(epub)
        self.ui.sync_direct()
        report = self.db.data[reader.REPORT_KEY][1]
        report.pop('mapping_version')  # Archive saved before mapper revisions existed.
        self.db.events.clear()
        self.ui.sync_direct()
        self.assertEqual(sum(e[0] == 'reader' for e in self.db.events), 1)
        self.assertEqual({e[1] for e in self.db.events}, {1})
        self.assertEqual(self.db.data[reader.REPORT_KEY][1]['mapping_version'], 3)
        self.db.events.clear()
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        self.db.data[reader.REPORT_KEY][1]['mapping_version'] = 1
        self.ui.sync_direct()
        self.assertEqual(sum(e[0] == 'reader' for e in self.db.events), 1)

    def test_unmapped_resume_warning_persists_without_rewriting_book(self):
        epub = Path(self.temp.name) / 'book.epub'
        epub.write_bytes(b'unchanged EPUB bytes')
        self.db.epubs[1] = str(epub)
        row = self.ui.matches['1' * 32]['boox']
        extra = json.loads(row['extraAttributes'])
        extra['backend']['current_page_position_v3'] = {'chapterID':'chapter', 'nodePosition':123, 'textOffset':4}
        row['extraAttributes'] = json.dumps(extra)
        self.ui.sync_direct()
        self.db.events.clear()
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        self.assertIn('1 book needs attention', self.ui.summary_text)
        self.assertIn('More… → Sync details', self.ui.summary_text)
        self.assertTrue(any('BOOX reading position is unverified' in x for x in self.ui.last_sync_issues))
        self.db.data[reader.REPORT_KEY][1]['resume'] = True
        self.ui.sync_direct()
        self.assertNotIn('need attention', self.ui.summary_text)
        self.assertFalse(any('BOOX reading position is unverified' in x for x in self.ui.last_sync_issues))

    def test_unchanged_archive_reintegrates_when_boox_is_farther(self):
        module = sys.modules['calibre_plugins.frankenboox.sync_flow']
        with patch.object(module, 'prepare_sync', return_value={'plans':[], 'keep':{}, 'issues':[], 'device':None, 'pull':[1]}):
            self.ui.sync_direct()
        self.assertEqual(sum(event[0]=='reader' for event in self.db.events),1)
        self.assertEqual({event[1] for event in self.db.events},{1})

    def test_repeat_sync_has_zero_writes_and_does_not_prompt(self):
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.db.root.rglob('*') if p.is_file()}
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        self.assertEqual(self.ui.choices, [])
        self.assertIn('2</b> book archives already up to date', self.ui.summary_text)
        self.assertEqual(before, {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in before})

    def test_only_changed_book_and_column_are_written(self):
        self.ui.matches['1' * 32]['boox']['progress'] = '11/100'
        self.ui.sync_direct()
        self.assertEqual({event[1] for event in self.db.events}, {1})
        changed_columns = [event[2] for event in self.db.events if event[0] == 'column']
        self.assertEqual(set(changed_columns), {'#nm_progress', '#nm_reading_notes'})
        self.assertIn('<b>1</b> already up to date', self.ui.summary_text)
        self.db.events.clear()
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])

    def test_new_book_is_imported_without_rewriting_existing_books(self):
        self.ui.matches['3' * 32] = copy.deepcopy(self.ui.matches['1' * 32])
        self.ui.matches['3' * 32]['book_id'] = 3
        self.ui.matches['3' * 32]['boox']['extraAttributes'] = json.dumps({'backend': {'doc_id': '3' * 32}})
        self.ui.sync_direct()
        self.assertEqual({event[1] for event in self.db.events}, {3})

    def test_existing_version_archives_can_be_skipped_without_migration_writes(self):
        self.db.data.pop(environment['SYNC_KEY'])
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])

    def test_missing_and_corrupt_archives_are_repaired(self):
        path = self.db.root / '1/data/neomatrix/boox-neoreader-state.json'
        for content in (None, b'broken json'):
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                self.db.events.clear()
                self.ui.sync_direct()
                self.assertEqual({event[1] for event in self.db.events}, {1})
                json.loads(path.read_bytes())

    def test_failed_reader_integration_is_retried(self):
        self.ui.matches['1' * 32]['boox']['progress'] = '12/100'
        self.ui.fail_reader = True
        self.ui.sync_direct()
        self.assertFalse(self.db.data[environment['SYNC_KEY']][1]['complete'])
        self.ui.fail_reader = False
        self.db.events.clear()
        self.ui.sync_direct()
        self.assertIn(('reader', 1, None), self.db.events)
        self.assertTrue(self.db.data[environment['SYNC_KEY']][1]['complete'])

    def test_operational_report_failure_is_retried_but_unsupported_location_is_not(self):
        self.db.data[reader.REPORT_KEY][1]['skipped'].append('Reader integration failed: failed merge')
        self.ui.sync_direct()
        self.assertIn(('reader', 1, None), self.db.events)
        self.db.events.clear()
        self.db.data[reader.REPORT_KEY][1]['skipped'].append('Resume position: BOOX location could not be verified; existing Calibre position retained')
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])

    def test_changed_epub_reintegrates_and_preserves_companion_files(self):
        path = self.db.root / 'book.epub'
        path.write_bytes(b'epub version one')
        self.db.epubs[1] = str(path)
        self.ui.sync_direct()
        self.db.events.clear()
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        path.write_bytes(b'epub version two')
        self.ui.sync_direct()
        self.assertIn(('reader', 1, None), self.db.events)
        self.assertFalse(any(event[0] in ('file', 'column') for event in self.db.events))

    def test_provider_record_reordering_does_not_rewrite_reading_notes(self):
        self.ui.annotation_rows = [{'idString': '1' * 32, 'uuid': 'a', 'quote': 'one'}, {'idString': '1' * 32, 'uuid': 'b', 'quote': 'two'}]
        self.ui.sync_direct()
        self.db.events.clear()
        self.ui.annotation_rows.reverse()
        self.ui.supplemental_sources = ['a different collection route']
        self.ui.matches['1' * 32]['confidence'] = 90
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])

    def test_missing_handwriting_file_and_changed_strokes_are_detected(self):
        points = [{'name': 'a#points', 'source_path': '/a', 'bytes': b'one', 'summary': {'sha256': 'one'}},
                  {'name': 'b#points', 'source_path': '/b', 'bytes': b'two', 'summary': {'sha256': 'two'}}]
        self.ui.point_files['1' * 32] = points
        self.ui.readernote_dbs['1' * 32] = {'name': 'ReaderNote.db', 'bytes': b'database',
                                          'parsed': {'integrity': 'ok', 'shape_rows': [{'status': 0}]}}
        self.ui.sync_direct()
        self.db.events.clear()
        points.reverse()
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        points[0]['bytes'] = b'changed'
        points[0]['summary']['sha256'] = 'changed'
        self.ui.sync_direct()
        written = [event[2] for event in self.db.events if event[0] == 'file']
        self.assertEqual(sum('/handwriting/' in name for name in written), 1)
        self.db.events.clear()
        extra = next(x for x in self.db.list_extra_files(1) if '/handwriting/' in x.relpath)
        Path(extra.file_path).unlink()
        self.ui.sync_direct()
        self.assertIn(('file', 1, extra.relpath), self.db.events)

    def test_calibre_timestamp_precision_does_not_trigger_rewrites(self):
        self.ui.matches['1' * 32]['boox']['lastAccess'] = '1000459'
        self.ui.sync_direct()
        value = self.db.columns[1, '#nm_last_read']
        self.assertEqual(value.microsecond, 0)
        self.assertEqual(self.db.data[integration.DATA_KEY][1]['progress']['last_access_ms'], 1000459)
        self.assertIn('1970-01-01T00:16:40.459000+00:00', self.db.columns[1, '#nm_reading_notes'])
        # Model reading the whole-second value back from Calibre's database.
        self.db.columns[1, '#nm_last_read'] = value.replace(microsecond=0)
        self.db.events.clear()
        self.ui.choices.clear()
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        self.assertEqual(self.ui.choices, [])

    def test_handwriting_preflight_still_blocks_all_writes(self):
        self.ui.point_files['1' * 32] = [{'name': 'a', 'source_path': '/a', 'bytes': b'x', 'summary': {}}]
        self.ui.sync_direct()
        self.assertEqual(self.db.events, [])
        self.assertIn('Sync stopped safely', self.ui.summary_text)


if __name__ == '__main__':
    unittest.main()
