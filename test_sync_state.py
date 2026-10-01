import copy
import unittest
from sync_state import state_signature


class StateSignatureTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            'progress': {'raw': '10/100', 'position': [1, 2, 3]},
            'annotations': {'active': [{'uuid': 'a', 'note': 'one'}, {'uuid': 'b', 'note': 'two'}], 'raw_rows': [{'uuid': 'a'}, {'uuid': 'b'}]},
            'bookmarks': {'active': [{'uuid': 'a'}, {'uuid': 'b'}], 'raw_rows': [{'uuid': 'a'}, {'uuid': 'b'}]},
            'reader_note': {'document_rows': [{'id': 1}, {'id': 2}], 'shape_rows': [{'id': 1}, {'id': 2}], 'resource_rows': [{'id': 1}, {'id': 2}]},
            'handwriting_point_files': [{'name': 'a', 'summary': {'sha256': 'a'}}, {'name': 'b', 'summary': {'sha256': 'b'}}],
            'reading_statistics': [{'time': 1}, {'time': 2}],
        }

    def test_volatile_collection_details_do_not_change_signature(self):
        other = copy.deepcopy(self.state)
        for key in ('imported_at_utc', 'sources', 'safety', 'match', 'calibre_integration'):
            other[key] = {'changed': True}
        self.assertEqual(state_signature(self.state), state_signature(other))

    def test_unordered_record_lists_can_be_reordered(self):
        other = copy.deepcopy(self.state)
        for block in ('annotations', 'bookmarks'):
            for key in ('active', 'raw_rows'):
                other[block][key].reverse()
        for key in ('document_rows', 'shape_rows', 'resource_rows'):
            other['reader_note'][key].reverse()
        other['handwriting_point_files'].reverse()
        other['reading_statistics'].reverse()
        self.assertEqual(state_signature(self.state), state_signature(other))

    def test_source_changes_are_detected(self):
        changes = (
            lambda s: s['progress'].update(raw='11/100'),
            lambda s: s['annotations']['active'][0].update(note='edited'),
            lambda s: s['bookmarks']['active'][0].update(uuid='new'),
            lambda s: s['reading_statistics'][0].update(time=3),
            lambda s: s['handwriting_point_files'][0]['summary'].update(sha256='changed'),
            lambda s: s['reader_note']['shape_rows'].append({'id': 3}),
        )
        for change in changes:
            with self.subTest(change=change):
                other = copy.deepcopy(self.state)
                change(other)
                self.assertNotEqual(state_signature(self.state), state_signature(other))

    def test_position_list_order_is_significant(self):
        other = copy.deepcopy(self.state)
        other['progress']['position'].reverse()
        self.assertNotEqual(state_signature(self.state), state_signature(other))

    def test_input_is_not_mutated(self):
        before = copy.deepcopy(self.state)
        state_signature(self.state)
        self.assertEqual(before, self.state)

    def test_non_objects_are_rejected(self):
        for value in (None, [], 'state', 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                state_signature(value)


if __name__ == '__main__':
    unittest.main()
