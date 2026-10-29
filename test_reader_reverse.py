import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from reader_bridge import Book, Chapter
from reader_reverse import SourceChapter, NativeSource
from reader_push import plan_position
from boox_push import _values


class ReversePositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'book.epub'
        self.one = '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title></head><body><p>Alpha café &amp; more</p><p>Beta text</p></body></html>'.encode()
        self.two = '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Two</title></head><body><p>Gamma chapter</p><p>Delta text</p></body></html>'.encode()
        with zipfile.ZipFile(self.path, 'w') as archive:
            archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
            archive.writestr('OPS/book.opf', '<package><manifest><item id="one" href="one.xhtml"/><item id="two" href="two.xhtml"/></manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>')
            archive.writestr('OPS/one.xhtml', self.one)
            archive.writestr('OPS/two.xhtml', self.two)
        self.book = Book([Chapter('OPS/one.xhtml', 0, ET.fromstring(self.one)),
                          Chapter('OPS/two.xhtml', 1, ET.fromstring(self.two))],
                         {'one': 0, 'two': 1}, hashlib.sha256(self.path.read_bytes()).hexdigest(),
                         source_path=str(self.path))
        self.book.xpath_modes = {'document0'}
        self.alpha = self.position('Alpha', [1,1,0,0])
        self.beta = self.position('Beta', [1,1,1,0])
        self.row = {'uuid': 'uuid', 'location': '/book.epub', 'type': 'epub', 'lastAccess': '9000',
                    'extraAttributes': json.dumps({'backend': {'doc_id': 'doc', 'md5': 'a'*32,
                    'current_page_position_v3': json.dumps(self.alpha)}, 'theme': 'keep'})}
        self.state = {'boox_identity': {'metadata_uuid': 'uuid', 'document_id': 'doc',
                      'path': '/book.epub', 'file_md5': 'a'*32}, 'boox_book': {'format': 'epub'},
                      'progress': {'last_access_ms': 9000, 'current_page_position_v3': self.alpha},
                      'annotations': {'active': [{'document_id': 'doc', 'location_begin': self.beta}]},
                      'raw_metadata_row': copy.deepcopy(self.row), 'epub_sha256': self.book.digest}

    def position(self, phrase, route):
        raw = self.one.decode()
        return {'chapterIndex': 0, 'chapterID': 'one', 'chapterStartPosition': 0,
                'chapterEndPosition': len(self.one), 'nodePosition': raw.index(phrase),
                'textOffset': 0, 'xpath': route}

    def plan(self, cfi='epubcfi(/4/2/4/2/1:3)', epoch_ms=1):
        return plan_position(self.state, self.book, self.row, {'cfi': cfi, 'epoch_ms': epoch_ms}, self.book.digest)

    def test_unseen_farther_text_location_is_ready_despite_older_clock(self):
        before = copy.deepcopy((self.state, self.row))
        plan = self.plan()
        self.assertEqual(plan['status'], 'ready', plan)
        self.assertEqual(plan['position']['chapterID'], 'two')
        self.assertEqual(plan['position']['nodePosition'], self.two.decode().index('Gamma'))
        self.assertEqual(plan['position']['textOffset'], 3)
        self.assertGreater(plan['calibre_epoch_ms'], 9000)
        self.assertEqual(before, (self.state, self.row))

    def test_converted_values_retain_non_position_metadata(self):
        values = _values(self.plan())
        self.assertEqual(json.loads(values['extraAttributes'])['theme'], 'keep')
        self.assertEqual(set(values), {'extraAttributes','lastAccess'})

    def test_mid_chapter_target_round_trips_to_exact_cfi(self):
        target = NativeSource(self.path, self.book, self.state).target('epubcfi(/2/2/4/2/1:14)')
        self.assertEqual(self.book.position(target['position'])['calibre_cfi'], 'epubcfi(/2/2/4/2/1:14)')
        self.assertEqual(target['position']['nodePosition'], self.one.decode().index('Alpha'))

    def test_later_current_chapter_is_never_rewound(self):
        source = NativeSource(self.path, self.book, self.state)
        later = source.target('epubcfi(/4/2/4/2/1:3)')['position']
        self.assertLess(source.compare(self.alpha, later), 0)

    def test_newer_but_earlier_location_is_not_pushed(self):
        current = dict(self.beta, textOffset=4)
        self.state['progress']['current_page_position_v3'] = current
        self.state['annotations']['active'][0]['location_begin'] = self.alpha
        attrs=json.loads(self.row['extraAttributes']);attrs['backend']['current_page_position_v3']=json.dumps(current)
        self.row['extraAttributes']=json.dumps(attrs);self.state['raw_metadata_row']=copy.deepcopy(self.row)
        self.assertEqual(self.plan('epubcfi(/2/2/4/2/1:1)', epoch_ms=10000)['status'], 'unchanged')

    def test_changed_source_coordinates_block_conversion(self):
        self.state['annotations']['active'][0]['location_begin']['nodePosition'] += 1
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_changed_chapter_bounds_block_conversion(self):
        self.state['annotations']['active'][0]['location_begin']['chapterEndPosition'] += 1
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_one_text_node_is_not_enough_calibration(self):
        self.state['annotations']['active'] = []
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_foreign_annotations_cannot_calibrate(self):
        self.state['annotations']['active'][0]['document_id'] = 'another-doc'
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_ambiguous_dom_modes_block_conversion(self):
        self.book.xpath_modes.add('document1')
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_spatial_and_invalid_offsets_block_conversion(self):
        for cfi in ['epubcfi(/4/2/4/2@50:50)', 'epubcfi(/4/2/4/2/1:999)', 'epubcfi(/400/2/4/2/1:0)']:
            with self.subTest(cfi=cfi):
                self.assertEqual(self.plan(cfi)['status'], 'unsupported')

    def test_device_change_after_archive_blocks_ready_plan(self):
        self.row['lastAccess']='9001'
        self.assertEqual(self.plan()['status'], 'conflict')

    def test_unsupported_surrogate_convention_is_not_guessed(self):
        with self.assertRaises(ValueError):
            SourceChapter('<html><body>😀</body></html>'.encode())

    def test_entities_are_decoded_without_changing_source_node_coordinates(self):
        source = SourceChapter(self.one)
        slot = source.slots['/2/4/2/1']
        self.assertEqual(slot['text'], 'Alpha café & more')
        self.assertEqual(slot['source_offset'], self.one.decode().index('Alpha'))


class DeviceRootRuleTests(ReversePositionTests.__bases__[0]):
    """Books with no BOOX highlights use the device-verified root-index rule."""
    setUp = ReversePositionTests.setUp
    position = ReversePositionTests.position
    plan = ReversePositionTests.plan

    def at_cover(self):
        # Opened on BOOX, never past the start: chapter bounds only, no text path.
        cover = {'chapterIndex': 0, 'chapterID': 'one', 'chapterStartPosition': 0,
                 'chapterEndPosition': len(self.one), 'nodePosition': 0, 'textOffset': 0, 'xpath': []}
        self.book.xpath_modes = set()
        self.state['annotations']['active'] = []
        self.state['progress']['current_page_position_v3'] = cover
        attrs = json.loads(self.row['extraAttributes'])
        attrs['backend']['current_page_position_v3'] = json.dumps(cover)
        self.row['extraAttributes'] = json.dumps(attrs)
        self.state['raw_metadata_row'] = copy.deepcopy(self.row)
        return cover

    def test_root_index_follows_the_chapter_prolog(self):
        from reader_reverse import native_root_index
        cases = {b'<html>': 0,
                 b"<?xml version='1.0' encoding='utf-8'?><html>": 1,
                 b"<?xml version='1.0' encoding='utf-8'?>\n<html>": 1,
                 b'<?xml version="1.0"?>\n<!DOCTYPE html>\n<html>': 3,
                 b'<!DOCTYPE html><html>': None,
                 b'<!-- note --><html>': None}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(native_root_index(raw), expected)

    def test_book_without_highlights_pushes_from_its_cover(self):
        self.at_cover()
        plan = self.plan()
        self.assertEqual(plan['status'], 'ready', plan)
        self.assertEqual(plan['position']['chapterID'], 'two')
        self.assertEqual(plan['position']['xpath'][0], 0)
        self.assertEqual(plan['position']['nodePosition'], self.two.decode().index('Gamma'))

    def test_observed_position_contradicting_the_rule_blocks_conversion(self):
        self.at_cover()
        # A real text position whose document root disagrees with this prolog.
        self.state['annotations']['active'] = [{'document_id': 'doc', 'location_begin': self.beta}]
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_unverified_prolog_blocks_conversion(self):
        self.at_cover()
        two = b'<!DOCTYPE html>' + self.two
        with zipfile.ZipFile(self.path, 'w') as archive:
            archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
            archive.writestr('OPS/book.opf', '<package><manifest><item id="one" href="one.xhtml"/><item id="two" href="two.xhtml"/></manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>')
            archive.writestr('OPS/one.xhtml', self.one)
            archive.writestr('OPS/two.xhtml', two)
        self.book.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.state['epub_sha256'] = self.book.digest
        self.assertEqual(plan_position(self.state, self.book, self.row,
                                       {'cfi': 'epubcfi(/4/2/4/2/1:3)', 'epoch_ms': 1}, self.book.digest)['status'],
                         'unsupported')

    def test_no_boox_position_at_all_blocks_conversion(self):
        self.at_cover()
        self.state['progress'] = {'last_access_ms': 9000}
        with self.assertRaisesRegex(ValueError, 'open the book once'):
            NativeSource(self.path, self.book, self.state)


if __name__ == '__main__':
    unittest.main()
