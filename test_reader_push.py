import copy
import json
import unittest
from reader_push import plan_position, position_object


MD5 = 'a' * 32
ONE = {'chapterID': 'one', 'nodePosition': 0, 'textOffset': 0, 'xpath': []}
TWO = {'chapterID': 'two', 'nodePosition': 0, 'textOffset': 0, 'xpath': []}

class Book:
    digest = 'b' * 64
    def position(self, position):
        if position['chapterID'] in ('one', 'two'):
            return {'calibre_cfi': 'epubcfi(' + position['chapterID'] + ')', 'spine_index': 1 if position['chapterID']=='one' else 0, 'text_offset': position.get('textOffset',0)}
    def quote(self, *args):
        return None


class PushPlans(unittest.TestCase):
    def setUp(self):
        self.book = Book()
        raw = json.dumps({'backend': {'doc_id': 'doc-one', 'md5': MD5,
                         'current_page_position_v3': json.dumps(TWO)}, 'retained': {'theme': 'unchanged'}}, indent=2)
        self.row = {'uuid': 'uuid-one', 'location': '/storage/card/one.epub', 'type': 'epub',
                    'lastAccess': '1000', 'extraAttributes': raw}
        self.state = {'boox_identity': {'metadata_uuid': 'uuid-one', 'document_id': 'doc-one',
                      'path': self.row['location'], 'file_md5': MD5}, 'boox_book': {'format': 'epub'},
                      'progress': {'last_access_ms': 1000, 'current_page_position_v3': TWO},
                      'bookmarks': {'active': [{'document_id': 'doc-one', 'position_v3': json.dumps(ONE)}]},
                      'raw_metadata_row': copy.deepcopy(self.row), 'epub_sha256': self.book.digest}
        self.calibre = {'cfi': 'epubcfi(one)', 'epoch_ms': 2000}

    def plan(self, **overrides):
        return plan_position(overrides.get('state', self.state), overrides.get('book', self.book),
                             overrides.get('row', self.row), overrides.get('calibre', self.calibre),
                             overrides.get('sha256', self.book.digest))

    def test_verified_observed_bookmark_is_ready(self):
        before = copy.deepcopy((self.state, self.row, self.calibre))
        plan = self.plan()
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['position'], ONE)
        self.assertEqual(plan['expected_extra_attributes'], self.row['extraAttributes'])
        self.assertEqual(before, (self.state, self.row, self.calibre))

    def test_newer_but_earlier_calibre_position_is_not_pushed(self):
        original = self.book.position
        def rank(position):
            anchor = original(position)
            if anchor:
                anchor['spine_index'] = 0 if position['chapterID']=='one' else 1
            return anchor
        self.book.position = rank
        self.assertEqual(self.plan()['status'], 'unchanged')

    def test_current_location_is_noop_even_when_timestamp_is_old(self):
        self.calibre.update(cfi='epubcfi(two)', epoch_ms=1)
        self.assertEqual(self.plan()['status'], 'unchanged')

    def test_replaced_book_content_is_not_a_duplicate(self):
        self.assertEqual(self.plan(sha256='c' * 64)['status'], 'conflict')

    def test_other_volume_uuid_is_never_selected(self):
        self.row['uuid'] = 'uuid-volume-two'
        self.assertEqual(self.plan()['status'], 'conflict')

    def test_changed_document_identity_is_blocked(self):
        self.state['boox_identity']['document_id'] = 'doc-other'
        self.assertEqual(self.plan()['status'], 'conflict')

    def test_changed_path_requires_fresh_pull(self):
        self.row['location'] = '/sdcard/Books/moved.epub'
        self.assertEqual(self.plan()['status'], 'conflict')

    def test_changed_epub_digest_is_blocked(self):
        self.state['epub_sha256'] = 'changed'
        self.assertEqual(self.plan()['status'], 'conflict')

    def test_missing_epub_verification_is_blocked(self):
        del self.state['epub_sha256']
        self.assertEqual(self.plan()['status'], 'conflict')

    def test_newer_device_conflicts_even_with_newer_calibre(self):
        self.row['lastAccess'] = '1500'
        self.assertEqual(self.plan()['status'], 'conflict')

    def test_older_but_farther_calibre_position_is_written(self):
        self.calibre['epoch_ms'] = 999
        plan = self.plan()
        self.assertEqual(plan['status'], 'ready')
        self.assertGreater(plan['calibre_epoch_ms'], 1000)

    def test_invalid_timestamps_fail_closed(self):
        for value in (None, True, False, 0, '0', 1.1, -1, 'nan', ' 2000'):
            with self.subTest(value=value):
                self.calibre['epoch_ms'] = value
                self.assertEqual(self.plan()['status'], 'unsupported')

    def test_invalid_current_timestamp_is_blocked(self):
        self.row['lastAccess'] = None
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_unseen_cfi_never_gets_a_guessed_position(self):
        self.calibre['cfi'] = 'epubcfi(unseen)'
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_foreign_bookmarks_cannot_supply_a_position(self):
        self.state['bookmarks']['active'][0]['document_id'] = 'doc-volume-two'
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_ambiguous_observed_positions_are_rejected(self):
        alternative = dict(ONE, textOffset=10)
        self.state['bookmarks']['active'].append({'document_id': 'doc-one', 'position_v3': alternative})
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_invalid_json_and_shapes_fail_closed(self):
        for value in ('not-json', '[]', 'null', '{"backend":NaN}', '{"backend":[]}'):
            with self.subTest(value=value):
                self.row['extraAttributes'] = value
                self.assertEqual(self.plan()['status'], 'unsupported')

    def test_different_engine_locations_are_not_overwritten(self):
        attrs = json.loads(self.row['extraAttributes'])
        attrs['backend']['current_page_position_v2'] = json.dumps(ONE)
        self.row['extraAttributes'] = json.dumps(attrs)
        self.assertEqual(self.plan()['status'], 'unsupported')

    def test_padded_numbers_preserve_strings(self):
        value = '{"chapterID":"0001","nodePosition":00000001,"textOffset":000002,"note":"0003"}'
        self.assertEqual(position_object(value), {'chapterID': '0001', 'nodePosition': 1, 'textOffset': 2, 'note': '0003'})

    def test_boolean_node_offsets_rejected(self):
        with self.assertRaises(ValueError):
            position_object(dict(ONE, nodePosition=True))

    def test_pdf_and_missing_inputs_unsupported(self):
        self.row['type'] = 'pdf'
        self.assertEqual(self.plan()['status'], 'unsupported')
        for key in ('state', 'row', 'calibre'):
            self.assertEqual(self.plan(**{key: None})['status'], 'unsupported')


if __name__ == '__main__':
    unittest.main()
