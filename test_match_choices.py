"""Matching BOOX records to Calibre: skipped files stay skipped, without Qt."""
import ast
import json
import os
from pathlib import Path
import re
import unittest

from sync_state import volume_identity

ROOT = Path(__file__).parent
_tree = ast.parse((ROOT / 'main.py').read_text())
_helpers = [n for n in _tree.body if isinstance(n, ast.FunctionDef)
            and n.name in ('norm', 'tolerant_json', 'stable_boox_key', 'boox_md5')]
_dialog = next(n for n in _tree.body if isinstance(n, ast.ClassDef) and n.name == 'FrankenDialog')
_methods = [n for n in _dialog.body if isinstance(n, ast.FunctionDef)
            and n.name in ('_match_books', '_best_fallback', '_same_format_choices')]
for _method in _methods:
    _method.decorator_list = []
environment = {'re': re, 'json': json, 'os': os, 'volume_identity': volume_identity}
exec(compile(ast.Module(body=_helpers + _methods, type_ignores=[]), str(ROOT / 'main.py'), 'exec'), environment)


def record(book_id, title, formats):
    return {'id': book_id, 'title': title, 'ntitle': environment['norm'](title), 'authors': ['A'],
            'nauth': 'a', 'formats': tuple(formats), 'volumes': None}


def boox_row(name, fmt, md5, doc_id):
    return {'name': name, 'title': None, 'type': fmt, 'progress': '1/10', 'lastAccess': '1000',
            'extraAttributes': json.dumps({'backend': {'doc_id': doc_id, 'md5': md5}})}


class Harness:
    _match_books = environment['_match_books']
    _best_fallback = environment['_best_fallback']
    _same_format_choices = environment['_same_format_choices']

    def __init__(self, rows, recs, answers=()):
        self.boox_rows, self.recs, self.answers, self.asked = rows, recs, list(answers), []

    def _calibre_records(self):
        return self.recs

    def _annotations_for(self, row):
        return []

    def _bookmarks_for(self, row):
        return []

    def _md5_for_format(self, book_id, fmt):
        return None

    def _manual_pick(self, br, recs, suggested=None):
        self.asked.append(br['name'])
        return self.answers.pop(0)


class MatchChoiceTests(unittest.TestCase):
    def setUp(self):
        self.prefs = {'manual_matches': {}, 'skipped_boox_records': {}}
        environment['prefs'] = self.prefs
        self.db = type('DB', (), {'format_metadata': lambda *args, **kwargs: {}})()

    def run_match(self, rows, recs, answers=()):
        ui = Harness(rows, recs, answers)
        ui.db = self.db
        ui._match_books()
        return ui

    def test_format_missing_from_calibre_is_not_asked(self):
        rows = [boox_row('Reading Lists.txt', 'txt', 'a' * 32, '1' * 32)]
        ui = self.run_match(rows, [record(1, 'Some Book', ['epub'])])
        self.assertEqual(ui.asked, [])
        self.assertIsNone(ui.matches['1' * 32]['book_id'])
        self.assertEqual(ui.matches['1' * 32]['method'], 'no Calibre book in this format')

    def test_leave_unmatched_is_remembered_including_duplicate_copies(self):
        recs = [record(1, 'Unrelated', ['epub'])]
        first = [boox_row('Notes.epub', 'epub', 'b' * 32, '2' * 32)]
        ui = self.run_match(first, recs, answers=[{'id': None, 'cancel': False}])
        self.assertEqual(ui.asked, ['Notes.epub'])
        # Same record again, plus a BOOX duplicate copy with a new identity.
        again = first + [boox_row('Notes-1.epub', 'epub', 'b' * 32, '3' * 32)]
        ui = self.run_match(again, recs)
        self.assertEqual(ui.asked, [])
        self.assertEqual({m['method'] for m in ui.matches.values()}, {'left unmatched (remembered)'})

    def test_a_different_file_is_still_asked(self):
        recs = [record(1, 'Unrelated', ['epub'])]
        self.run_match([boox_row('Notes.epub', 'epub', 'b' * 32, '2' * 32)], recs,
                       answers=[{'id': None, 'cancel': False}])
        ui = self.run_match([boox_row('Other.epub', 'epub', 'c' * 32, '4' * 32)], recs,
                            answers=[{'id': None, 'cancel': False}])
        self.assertEqual(ui.asked, ['Other.epub'])

    def test_dialog_offers_only_same_format_books(self):
        ui = Harness([], [])
        epub, pdf = record(1, 'E', ['epub']), record(2, 'P', ['pdf'])
        offered, suggested = ui._same_format_choices({'type': 'PDF'}, [epub, pdf], suggested=epub)
        self.assertEqual(offered, [pdf])
        self.assertIsNone(suggested)


if __name__ == '__main__':
    unittest.main()
