import copy
import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import warnings
import zipfile

SOURCE = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE))
from epub_identity import identity_from_bytes, IDENTITY_SCHEMA
from reader_push import plan_position
from boox_push import apply_position, restore_position, PushError

CONTAINER = b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>'
PACKAGE = '''<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">old-id</dc:identifier><dc:title>Book</dc:title><dc:language>en</dc:language><meta property="rendition:layout">reflowable</meta></metadata>
<manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/><item id="b" href="b.xhtml" media-type="application/xhtml+xml"/></manifest>
<spine><itemref idref="a"/><itemref idref="b"/></spine></package>'''

def members():
    return {'mimetype': b'application/epub+zip', 'META-INF/container.xml': CONTAINER,
            'book.opf': PACKAGE.encode(), 'a.xhtml': b'<html><body><p>First</p></body></html>',
            'b.xhtml': b'<html><body><p>Second</p></body></html>', 'style.css': b'p {color: black}',
            'cover.png': b'original image', 'nav.xhtml': b'<nav>Contents</nav>'}

def epub(files, compression=zipfile.ZIP_STORED, reverse=False):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=compression) as archive:
        entries = list(files.items())
        for name, value in reversed(entries) if reverse else entries:
            archive.writestr(name, value)
    return result.getvalue()

def fingerprint(files):
    return identity_from_bytes(epub(files))['reading_sha256']

class IdentityTests(unittest.TestCase):
    def test_calibre_metadata_bookmarks_and_zip_packing(self):
        original = members()
        edited = members()
        edited['book.opf'] = PACKAGE.replace('old-id', 'new-id').replace('>Book<', '>New title<').replace('</metadata>', '<meta name="calibre:timestamp" content="today"/></metadata>').encode()
        edited['META-INF/calibre_bookmarks.txt'] = b'new reading location'
        first = identity_from_bytes(epub(original))
        second = identity_from_bytes(epub(edited, zipfile.ZIP_DEFLATED, True))
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertEqual(first['reading_sha256'], second['reading_sha256'])

    def test_manifest_order_is_not_reading_order(self):
        edited = members()
        start = PACKAGE.index('<item id="a"')
        end = PACKAGE.index('</manifest>')
        items = PACKAGE[start:end].split('/>')
        edited['book.opf'] = (PACKAGE[:start] + items[1] + '/>' + items[0] + '/>' + PACKAGE[end:]).encode()
        self.assertEqual(fingerprint(members()), fingerprint(edited))

    def test_reading_resources_are_exact(self):
        for name in ('a.xhtml', 'b.xhtml', 'style.css', 'cover.png', 'nav.xhtml'):
            with self.subTest(resource=name):
                edited = members()
                edited[name] += b'changed'
                self.assertNotEqual(fingerprint(members()), fingerprint(edited))

    def test_structure_language_and_layout_are_retained(self):
        changes = [('idref="a"/><itemref idref="b"', 'idref="b"/><itemref idref="a"'),
                   ('idref="a"', 'idref="a" linear="no"'),
                   ('id="a"', 'id="new-a"'), ('>en<', '>de<'),
                   ('>reflowable<', '>pre-paginated<'),
                   ('</metadata>', '<meta refines="#uid" property="rendition:layout">pre-paginated</meta></metadata>')]
        for before, after in changes:
            with self.subTest(change=after):
                edited = members()
                edited['book.opf'] = PACKAGE.replace(before, after).replace('idref="a"', 'idref="new-a"') .encode() if after == 'id="new-a"' else PACKAGE.replace(before, after).encode()
                self.assertNotEqual(fingerprint(members()), fingerprint(edited))

    def test_obfuscated_font_identifiers_are_retained(self):
        original = members()
        original['META-INF/encryption.xml'] = b'<encryption/>'
        edited = copy.deepcopy(original)
        edited['book.opf'] = PACKAGE.replace('old-id', 'new-id').encode()
        self.assertNotEqual(fingerprint(original), fingerprint(edited))

    def test_ambiguous_zip_and_packages_refused(self):
        for invalid in ('../chapter.xhtml', '/chapter.xhtml', 'foo/../chapter.xhtml', 'foo\\chapter.xhtml'):
            edited = members()
            edited[invalid] = b'bad'
            with self.subTest(path=invalid), self.assertRaises(ValueError):
                fingerprint(edited)
        for bad in (PACKAGE.replace('id="b"', 'id="a"'), PACKAGE.replace('idref="a"', 'idref="absent"'), '<broken>'):
            edited = members()
            edited['book.opf'] = bad.encode()
            with self.subTest(package=bad), self.assertRaises(Exception):
                fingerprint(edited)
        output = io.BytesIO(epub(members()))
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(output, 'a') as archive:
                archive.writestr('a.xhtml', b'duplicate')
        with self.assertRaises(ValueError):
            identity_from_bytes(output.getvalue())

    def test_entity_declarations_refused_in_utf8_and_utf16(self):
        for encoding in ('utf-8', 'utf-16'):
            edited = members()
            edited['book.opf'] = ('<!DOCTYPE package [<!ENTITY title "Book">]>' + PACKAGE).encode(encoding)
            with self.subTest(encoding=encoding), self.assertRaises(ValueError):
                fingerprint(edited)

class FakeBook:
    digest = 'a' * 64
    reading_digest = 'c' * 64
    def position(self, value):
        return {'calibre_cfi': 'epubcfi(/6/4!/4/2:0)'} if value.get('chapterID') == 'b' else None

def fixture():
    old = {'chapterID': 'a', 'nodePosition': 0, 'textOffset': 0}
    target = {'chapterID': 'b', 'nodePosition': 0, 'textOffset': 0}
    row = {'uuid': 'record', 'location': '/storage/books/book.epub', 'type': 'epub',
           'hashTag': 'd' * 32, 'lastAccess': '1000',
           'extraAttributes': json.dumps({'backend': {'doc_id': 'document', 'md5': 'd' * 32, 'current_page_position_v3': json.dumps(old)}})}
    state = {'boox_book': {'format': 'epub'},
             'boox_identity': {'metadata_uuid': 'record', 'document_id': 'document', 'path': row['location'], 'file_md5': 'd' * 32},
             'progress': {'last_access_ms': 1000}, 'raw_metadata_row': copy.deepcopy(row),
             'bookmarks': {'active': [{'document_id': 'document', 'position_v3': target}]},
             'epub_sha256': FakeBook.digest, 'epub_identity_schema': IDENTITY_SCHEMA, 'epub_reading_sha256': FakeBook.reading_digest}
    position = {'cfi': 'epubcfi(/6/4!/4/2:0)', 'epoch_ms': 2000}
    return state, row, position

class FakeDevice:
    serial = 'test-serial'
    sha = 'b' * 64
    updates = 0
    change_when_stopped = False
    def __init__(self, row):
        self.row = copy.deepcopy(row)
    def query(self, uuid):
        return copy.deepcopy(self.row)
    def book_sha256(self, path):
        return self.sha
    def stop_reader(self):
        if self.change_when_stopped:
            self.sha = 'f' * 64
    def update(self, uuid, expected, values):
        if self.row != expected:
            return 0
        self.row.update(values)
        self.updates += 1
        return 1

class PositionIdentityTests(unittest.TestCase):
    def plan(self, state=None, device_sha='b' * 64, reading='c' * 64):
        saved, row, position = fixture()
        return plan_position(state or saved, FakeBook(), row, position, device_sha, reading)

    def test_equivalent_books_keep_exact_device_checksum(self):
        plan = self.plan()
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['file_sha256'], 'b' * 64)

    def test_content_baseline_and_schema_changes_refuse_push(self):
        self.assertEqual(self.plan(reading='f' * 64)['status'], 'conflict')
        for key, value in [('epub_reading_sha256', 'f' * 64), ('epub_identity_schema', 'unknown')]:
            saved = fixture()[0]
            saved[key] = value
            self.assertEqual(self.plan(state=saved)['status'], 'conflict')
        self.assertEqual(self.plan(device_sha=None)['status'], 'conflict')

    def test_legacy_baseline(self):
        saved = fixture()[0]
        saved.pop('epub_reading_sha256')
        self.assertEqual(self.plan(state=saved, device_sha=FakeBook.digest)['status'], 'ready')
        saved['epub_sha256'] = 'f' * 64
        self.assertEqual(self.plan(state=saved)['status'], 'conflict')

    def test_changed_state_and_unknown_positions_refused(self):
        saved, row, position = fixture()
        row['lastAccess'] = '1500'
        self.assertEqual(plan_position(saved, FakeBook(), row, position, 'b'*64, 'c'*64)['status'], 'conflict')
        row = fixture()[1]
        position['cfi'] = 'epubcfi(/unknown)'
        self.assertEqual(plan_position(saved, FakeBook(), row, position, 'b'*64, 'c'*64)['status'], 'unsupported')

    def test_backup_push_and_restore_use_device_checksum(self):
        device = FakeDevice(fixture()[1])
        plan = dict(self.plan(), serial=device.serial)
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'backup.json'
            self.assertEqual(apply_position(plan, device, backup)['status'], 'written')
            self.assertEqual(json.loads(backup.read_text())['file_sha256'], device.sha)
            self.assertEqual(restore_position(device, backup)['status'], 'restored')

    def test_device_change_during_push_or_restore_is_refused(self):
        for stage in ('before', 'during', 'restore', 'during_restore'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                device = FakeDevice(fixture()[1])
                plan = dict(self.plan(), serial=device.serial)
                backup = Path(directory) / 'backup.json'
                if stage == 'before':
                    device.sha = 'f' * 64
                elif stage == 'during':
                    device.change_when_stopped = True
                if stage in ('restore', 'during_restore'):
                    apply_position(plan, device, backup)
                    if stage == 'restore':
                        device.sha = 'f' * 64
                    else:
                        device.change_when_stopped = True
                    with self.assertRaises(PushError):
                        restore_position(device, backup)
                    self.assertEqual(device.updates, 1)
                else:
                    with self.assertRaises(PushError):
                        apply_position(plan, device, backup)
                    self.assertEqual(device.updates, 0)

class IncrementalArchiveTests(unittest.TestCase):
    def test_viewer_metadata_change_skips_archive_but_content_change_repairs(self):
        # Execute the actual GUI method with a temporary library fixture.
        # Extracting this method avoids starting Qt or loading the real library.
        import epub_identity
        import sync_state
        package = types.ModuleType('calibre_plugins.frankenboox')
        package.__path__ = [str(SOURCE)]
        modules = {'calibre_plugins': types.ModuleType('calibre_plugins'),
                   'calibre_plugins.frankenboox': package,
                   'calibre_plugins.frankenboox.sync_state': sync_state,
                   'calibre_plugins.frankenboox.epub_identity': epub_identity}
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location('calibre_plugins.frankenboox.integration', SOURCE / 'integration.py')
            integration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(integration)
            sys.modules[spec.name] = integration
            tree = ast.parse((SOURCE / 'main.py').read_text())
            method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == '_archive_is_current')
            namespace = {'state_signature': sync_state.state_signature, 'Path': Path, 'json': json, 'hashlib': hashlib}
            exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE / 'main.py'), 'exec'), namespace)
            check = namespace['_archive_is_current']
            state = {'progress': {'raw': '1/10', 'last_access_ms': 1000}}
            values = integration.prepare(state)[0]
            with tempfile.TemporaryDirectory() as directory:
                book = Path(directory) / 'book.epub'
                book.write_bytes(epub(members()))
                anchors = {'epub_identity_schema': IDENTITY_SCHEMA, 'epub_reading_sha256': epub_identity.reading_digest(book)}
                db = types.SimpleNamespace(field_for=lambda key, bid: values[key[1:]], list_extra_files=lambda *a, **kw: [], format_abspath=lambda *a: str(book))
                owner = types.SimpleNamespace(db=db, _archived_state_for_book=lambda bid: (state, None), _reader_sync_complete=lambda report: True)
                status = {'complete': True, 'signature': sync_state.state_signature(state)}
                from reader_bridge import READER_MAPPING_VERSION
                args = (owner, 1, state, {}, state, {'mapping_version': READER_MAPPING_VERSION}, anchors, status)
                self.assertTrue(check(*args))
                edited = members()
                edited['META-INF/calibre_bookmarks.txt'] = b'viewer position changed'
                edited['book.opf'] = PACKAGE.replace('old-id', 'new-id').encode()
                book.write_bytes(epub(edited, zipfile.ZIP_DEFLATED, True))
                self.assertTrue(check(*args))
                edited['a.xhtml'] += b'new text'
                book.write_bytes(epub(edited))
                self.assertFalse(check(*args))

if __name__ == '__main__':
    unittest.main()
