import subprocess
import tempfile
from pathlib import Path
import unittest
from boox_files import retain_existing_books


class ExistingBookTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
