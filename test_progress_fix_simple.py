"""Regression checks for BOOX progress written to Calibre's standard index."""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

import reader_bridge as bridge
import library_push


class ProgressFractionTests(unittest.TestCase):
    def test_page_boundaries(self):
        for current, expected in ((0, 0.0), (50, 0.5), (100, 1.0)):
            with self.subTest(current=current):
                self.assertEqual(bridge._extract_progress_fraction(
                    {'current_page': current, 'total_pages': 100}), expected)

    def test_raw_boundaries(self):
        for raw, expected in (('0/100', 0.0), (' 50 / 100 ', 0.5), ('100/100', 1.0)):
            with self.subTest(raw=raw):
                self.assertEqual(bridge._extract_progress_fraction({'raw': raw}), expected)

    def test_valid_raw_zero_takes_precedence(self):
        self.assertEqual(bridge._extract_progress_fraction(
            {'raw': '0/100', 'current_page': 50, 'total_pages': 100}), 0.0)

    def test_missing_and_invalid_progress(self):
        for progress in (None, [], {}, {'raw': 'unknown'}, {'raw': '5/0'},
                         {'raw': '101/100'}, {'current_page': -1, 'total_pages': 100},
                         {'current_page': 101, 'total_pages': 100},
                         {'current_page': 1, 'total_pages': 0},
                         {'current_page': True, 'total_pages': 100},
                         {'current_page': 0, 'total_pages': True},
                         {'current_page': '50', 'total_pages': 100}):
            with self.subTest(progress=progress):
                self.assertIsNone(bridge._extract_progress_fraction(progress))

    def test_nonfinite_and_overflow(self):
        for current, total in ((1, float('inf')), (0, float('inf')),
                               (float('inf'), float('inf')), (float('nan'), 100),
                               (1, float('nan')), (10 ** 400, 10 ** 401)):
            with self.subTest(current=current, total=total):
                self.assertIsNone(bridge._extract_progress_fraction(
                    {'current_page': current, 'total_pages': total}))
        self.assertIsNone(bridge._extract_progress_fraction({'raw': '1' + '0' * 400 + '/2' + '0' * 400}))


class StandardProgressWriteTests(unittest.TestCase):
    def setUp(self):
        constants = types.ModuleType('calibre.db.constants')
        constants.EBOOK_VIEWER_USER = 'local'
        constants.EBOOK_VIEWER_DEVICE = 'calibre-desktop-viewer'
        self.modules = patch.dict(sys.modules, {
            'calibre': types.ModuleType('calibre'),
            'calibre.db': types.ModuleType('calibre.db'),
            'calibre.db.constants': constants,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.db = MagicMock()
        self.db.format_abspath.return_value = '/unused/book.epub'
        self.db.annotations_map_for_book.return_value = {}
        self.cfi = 'epubcfi(/6/2!/4/2/1:50)'
        self.timestamp = '2026-09-30T00:00:00+00:00'

    def integrate(self, progress, applied=True, resume=True, same=False, **choices):
        portable = {'resume': {'calibre_cfi': self.cfi, 'timestamp': self.timestamp}
                    if resume else None, 'annotations': []}
        with patch.object(library_push, 'calibre_position', side_effect=ValueError('No saved Calibre EPUB reading position')), \
                patch.object(bridge, 'load_book'), \
                patch.object(bridge, 'convert', return_value=([], portable, [])), \
                patch.object(bridge, '_write_sidecar_resume',
                             return_value=(applied, None if applied else {'pos': 'previous', 'same': same})) as sidecar:
            report = bridge._integrate_reader(self.db, 7, {'progress': progress}, **choices)
        return report, sidecar

    def test_writes_real_fraction_and_preserves_position_metadata(self):
        for raw, expected in (('0/100', 0.0), ('50/100', 0.5), ('100/100', 1.0)):
            with self.subTest(raw=raw):
                self.db.reset_mock()
                report, _ = self.integrate({'raw': raw})
                self.db.set_last_read_position.assert_called_once_with(
                    7, 'EPUB', user='local', device='calibre-desktop-viewer',
                    cfi=self.cfi, epoch=1790726400.0, pos_frac=expected)
                self.assertTrue(report['resume_applied'])
                self.assertEqual(report['skipped'], [])

    def test_structured_pages_reach_database(self):
        self.integrate({'current_page': 50, 'total_pages': 100})
        self.assertEqual(self.db.set_last_read_position.call_args.kwargs['pos_frac'], 0.5)

    def test_unknown_or_invalid_progress_retains_database_record(self):
        for progress in ({}, {'raw': 'broken'}, {'current_page': True, 'total_pages': 100},
                         {'current_page': 1, 'total_pages': float('inf')}):
            with self.subTest(progress=progress):
                self.db.reset_mock()
                report, _ = self.integrate(progress)
                self.db.set_last_read_position.assert_not_called()
                self.assertTrue(any('progress' in item.lower() for item in report['skipped']))

    def test_newer_calibre_position_is_retained(self):
        report, _ = self.integrate({'raw': '50/100'}, applied=False)
        self.db.set_last_read_position.assert_not_called()
        self.assertFalse(report['resume_applied'])

    def test_equal_position_is_reported_as_matching_not_retained(self):
        report, _ = self.integrate({'raw': '50/100'}, applied=False, same=True)
        self.db.set_last_read_position.assert_not_called()
        self.assertTrue(report['resume_matches'])
        self.assertFalse(any('farthest' in item for item in report['skipped']))

    def test_farther_calibre_position_is_still_reported_as_retained(self):
        report, _ = self.integrate({'raw': '50/100'}, applied=False, same=False)
        self.assertFalse(report['resume_matches'])
        self.assertTrue(any('farthest Calibre position was retained' in item for item in report['skipped']))

    def test_keep_choice_retains_calibre_position(self):
        report, sidecar = self.integrate({'raw': '50/100'}, keep_boox_position=True)
        sidecar.assert_not_called()
        self.db.set_last_read_position.assert_not_called()
        self.assertFalse(report['resume_applied'])

    def test_unmapped_location_does_not_write(self):
        _, sidecar = self.integrate({'raw': '50/100'}, resume=False)
        sidecar.assert_not_called()
        self.db.set_last_read_position.assert_not_called()

    def test_explicit_boox_choice_preserves_timestamp_policy(self):
        _, sidecar = self.integrate({'raw': '50/100'}, use_boox_position=True)
        self.assertTrue(sidecar.call_args.kwargs['force'])
        self.assertEqual(self.db.set_last_read_position.call_args.kwargs['cfi'], self.cfi)
        self.assertEqual(self.db.set_last_read_position.call_args.kwargs['pos_frac'], 0.5)
        self.assertGreater(self.db.set_last_read_position.call_args.kwargs['epoch'], 1790726400.0)


if __name__ == '__main__':
    unittest.main()
