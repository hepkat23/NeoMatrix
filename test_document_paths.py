import copy
import unittest
import xml.etree.ElementTree as ET
from reader_bridge import Book, Chapter, convert

class DocumentPaths(unittest.TestCase):
    def fixture(self):
        original = ET.fromstring('<html>\n<head><title>Title</title></head>\n<body>\n<p><span>First sentence.</span></p>\n<p><span>Second sentence.</span></p>\n<p><span>Current sentence.</span></p>\n</body></html>')
        normalized = copy.deepcopy(original)
        normalized.text = normalized.tail = None
        chapter = Chapter('chapter.xhtml', 0, normalized, original)
        book = Book([chapter], {'chapter': 0}, 'a'*64)
        def position(paragraph, offset=0):
            return {'chapterID': 'chapter', 'nodePosition': 100+paragraph, 'textOffset': offset, 'xpath': [1, 3, paragraph, 0, 0]}
        state = {'boox_identity': {'document_id': 'test'},
                 'annotations': {'active': [
                     {'annotation_uuid': 'first', 'quote': 'First sentence.', 'location_begin': position(1)},
                     {'annotation_uuid': 'second', 'quote': 'Second sentence.', 'location_begin': position(3)}]},
                 'progress': {'current_page_position_v3': position(5, 8), 'last_access_ms': 1000}}
        return book, state

    def test_verified_document_and_whitespace_path_maps_resume(self):
        book, state = self.fixture()
        _, anchors, _ = convert(state, book)
        self.assertEqual(book.xpath_modes, {'document0'})
        self.assertEqual(anchors['resume']['calibre_cfi'], 'epubcfi(/2/2/4/6/2/1:8)')
        self.assertEqual(anchors['resume']['method'], 'quote-calibrated-boox-xpath')

    def test_without_observed_quote_evidence_resume_is_not_guessed(self):
        book, state = self.fixture()
        state['annotations']['active'] = []
        _, anchors, _ = convert(state, book)
        self.assertIsNone(anchors['resume'])

    def test_conflicting_quote_evidence_blocks_resume(self):
        book, state = self.fixture()
        state['annotations']['active'][1]['location_begin']['xpath'] = [2, 3, 0, 0]
        _, anchors, _ = convert(state, book)
        self.assertIsNone(anchors['resume'])

    def test_wrong_document_prefix_or_out_of_range_offset_is_not_accepted(self):
        for prefix, offset in ((0, 8), (1, 999)):
            book, state = self.fixture()
            state['progress']['current_page_position_v3']['xpath'][0] = prefix
            state['progress']['current_page_position_v3']['textOffset'] = offset
            _, anchors, _ = convert(state, book)
            self.assertIsNone(anchors['resume'])

if __name__ == '__main__':
    unittest.main()
