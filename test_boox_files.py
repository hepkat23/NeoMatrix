import subprocess
import tempfile
from pathlib import Path
import unittest
import copy
import re
from boox_files import retain_existing_books, check_book_files, record_token, exclude_saved_records


class ExistingBookTests(unittest.TestCase):
    def test_partial_batch_failure_is_reported_and_only_verified_missing_books_are_skipped(self):
        rows = [{'location': '/Books/a.epub'}, {'location': '/Books/b.epub'}]
        calls = []
        def query(command):
            calls.append(command)
            if len(calls) == 1:
                return '0:M\n'
            raise OSError('device disconnected')
        result = check_book_files(rows, query, max_command_bytes=250)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.kept, [rows[1]])
        self.assertEqual(result.missing, [rows[0]])
        self.assertEqual(result.uncertain, [rows[1]])
        self.assertTrue(result.warnings)

    def test_unicode_paths_are_batched_within_byte_limit(self):
        rows = [{'location': '/Books/' + ('書' * 12) + str(i) + '.epub'} for i in range(100)]
        calls = []
        def query(command):
            self.assertLessEqual(len(command.encode('utf-8')), 2000)
            calls.append(command)
            return ''.join(i + ':P\n' for i in re.findall(r'printf "(\d+):P', command))
        result = check_book_files(rows, query, max_command_bytes=2000)
        self.assertGreater(len(calls), 1)
        self.assertEqual(result.kept, rows)
        self.assertEqual(result.uncertain, [])

    def test_oversize_path_is_preserved_with_a_warning(self):
        rows = [{'location': '/Books/' + 'x' * 1000 + '.epub'}]
        calls = []
        result = check_book_files(rows, lambda command: calls.append(command), max_command_bytes=100)
        self.assertEqual(result.kept, rows)
        self.assertEqual(result.uncertain, rows)
        self.assertEqual(calls, [])
        self.assertTrue(result.warnings)

    def test_deleted_book_is_excluded_existing_and_unknown_paths_remain(self):
        rows = [{'location': '/Books/current.epub'}, {'location': '/Books/deleted.epub'},
                {'location': 'content://a/book'}, {'location': '/restricted/uncertain.epub'}]
        kept, count = retain_existing_books(rows, lambda command: '0:P\n1:M\n3:U\n')
        self.assertEqual(kept, [rows[0], rows[2], rows[3]])
        self.assertEqual(count, 1)

    def test_permission_or_connection_errors_do_not_hide_books(self):
        rows = [{'location': '/Books/book.epub'}]
        def failed(command):
            raise OSError('permission denied')
        self.assertEqual(retain_existing_books(rows, failed), (rows, 0))
        for output in ('', '0:M\n1:M\n', 'permission denied\n', '0:M\n0:M\n'):
            self.assertEqual(retain_existing_books(rows, lambda command: output), (rows, 0))

    def test_shell_quotes_paths_and_accepts_alternate_existing_path(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            current = folder / "A title's $(touch injected) `text`.epub"
            current.write_bytes(b'book')
            rows = [{'nativeAbsolutePath': str(folder / 'old.epub'), 'location': str(current)},
                    {'location': str(folder / 'gone.epub')},
                    {'location': str(folder / 'missing-directory' / 'unknown.epub')}]
            def query(command):
                return subprocess.check_output(['sh', '-c', command], text=True, cwd=folder)
            kept, count = retain_existing_books(rows, query)
            self.assertEqual(kept, [rows[0], rows[2]])
            self.assertEqual(count, 1)
            self.assertFalse((folder / 'injected').exists())


class RecordExclusions(unittest.TestCase):
    def setUp(self):
        self.row = {'uuid': 'a'*32, 'id': '3', 'title': 'Same title',
                    'location': '/Books/book.epub', 'lastAccess': '1000',
                    'extraAttributes': '{"backend":{"md5":"old","current_page":10}}'}

    def test_exclusion_is_local_to_one_unchanged_record_and_device(self):
        token = record_token('DEVICE-A', self.row)
        self.assertEqual(exclude_saved_records([self.row], 'DEVICE-A', [token]), ([], [self.row]))
        self.assertEqual(exclude_saved_records([self.row], 'DEVICE-B', [token]), ([self.row], []))
        other = dict(self.row, uuid='b'*32)
        self.assertEqual(exclude_saved_records([other], 'DEVICE-A', [token]), ([other], []))

    def test_reading_again_moving_or_replacing_makes_record_eligible_again(self):
        token = record_token('DEVICE-A', self.row)
        for update in ({'lastAccess': '2000'}, {'location': '/Read/book.epub'},
                       {'extraAttributes': '{"backend":{"md5":"new","current_page":10}}'}):
            changed = dict(self.row, **update)
            self.assertEqual(exclude_saved_records([changed], 'DEVICE-A', [token]), ([changed], []))
        self.assertEqual(self.row['lastAccess'], '1000')

    def test_unknown_record_identity_cannot_be_excluded(self):
        row = {'title': 'Book', 'location': '/Books/book.epub', 'extraAttributes': '{}'}
        self.assertIsNone(record_token('DEVICE-A', row))
        self.assertEqual(exclude_saved_records([row], 'DEVICE-A', [None]), ([row], []))


if __name__ == '__main__':
    unittest.main()
