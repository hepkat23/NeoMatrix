"""Invert exact EPUB text CFIs only after native source-position calibration.

NeoReader source offsets and chapter bounds are not visible-text percentages.
This implementation requires matching live book structure and independently
observed native text slots. Unsupported source encodings/DOM conventions fail.
"""
import json
from bisect import bisect_right
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
from xml.parsers import expat
import zipfile


def units(value):
    return len(value.encode('utf-16-le')) // 2


# NeoReader's index for <html> among the document's top-level nodes, keyed by
# what precedes <html> in the raw chapter source. The XML declaration (with the
# whitespace right after it) counts as one node. Verified on a Go Color 7 Gen II
# (provider 539b26bd…) against 210 native positions in every synced EPUB; any
# other prolog shape is unverified and refused.
NATIVE_ROOT_INDEX = {
    (): 0,
    ('xml',): 1,
    ('xml', 'doctype', 'space'): 3,
}
_PROLOG = re.compile(rb'(<\?xml\b[^>]*\?>\s*)|(<!DOCTYPE[^>]*>)|(<!--.*?-->)|(<\?[^>]*\?>)|(\s+)', re.S | re.I)


def native_root_index(raw):
    """NeoReader's top-level index of <html> for this chapter, or None if unverified."""
    end = raw.lower().find(b'<html')
    if end < 0:
        return None
    shape, position = [], 0
    while position < end:
        match = _PROLOG.match(raw, position, end)
        if not match:
            return None
        shape.append(('xml', 'doctype', 'comment', 'pi', 'space')[match.lastindex - 1])
        position = match.end()
    return NATIVE_ROOT_INDEX.get(tuple(shape))


def native_route(route, root_index):
    return (root_index,) + tuple(route[1:])


class SourceChapter:
    def __init__(self, raw):
        if raw.startswith(b'\xef\xbb\xbf'):
            raise ValueError('Native source BOM offsets are not verified')
        text = raw.decode('utf-8')
        # Source-coordinate behavior for surrogate pairs needs native evidence.
        if any(ord(ch) > 0xffff for ch in text) or b'<!ENTITY' in raw.upper():
            raise ValueError('Unsupported native source character convention')
        self.slots = {}
        self.elements = {}
        self.raw = raw
        stack = []
        parser = expat.ParserCreate()
        # Avoid decoding the full preceding source for every XML event.
        byte_ends, extras = [], []
        byte_count = extra = 0
        for char in text:
            width = len(char.encode('utf-8'))
            byte_count += width
            if width > 1:
                extra += width - 1
                byte_ends.append(byte_count)
                extras.append(extra)

        def source_offset():
            index = parser.CurrentByteIndex
            mark = bisect_right(byte_ends, index)
            return index - (extras[mark - 1] if mark else 0)

        def flush(frame):
            pending = frame.get('pending')
            if pending is None:
                return
            cfi = frame['cfi'] + '/' + str(frame['elements'] * 2 + 1)
            route = tuple(frame['route'] + [frame['slots']])
            if cfi in self.slots:
                raise ValueError('Fragmented XML text slot cannot be inverted safely')
            self.slots[cfi] = {'text': pending[1], 'source_offset': pending[0], 'route': route}
            frame['slots'] += 1
            frame['pending'] = None

        def start(name, attrs):
            if stack:
                parent = stack[-1]
                flush(parent)
                route = parent['route'] + [parent['slots']]
                parent['slots'] += 1
                parent['elements'] += 1
                cfi = parent['cfi'] + '/' + str(parent['elements'] * 2)
            else:
                route, cfi = [1], '/2'
            self.elements[tuple(route)] = source_offset()
            stack.append({'route': route, 'cfi': cfi, 'slots': 0, 'elements': 0, 'pending': None})

        def characters(value):
            if not stack or not value:
                return
            frame = stack[-1]
            if frame['pending'] is None:
                frame['pending'] = [source_offset(), value]
            else:
                frame['pending'][1] += value

        def end(name):
            flush(stack[-1])
            stack.pop()

        def comment(value):
            if stack:
                flush(stack[-1])
                stack[-1]['slots'] += 1

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = characters
        parser.CommentHandler = comment
        parser.ExternalEntityRefHandler = lambda *args: 0
        parser.Parse(raw, True)
        self.by_route = {v['route']: v for v in self.slots.values()}


class NativeSource:
    def __init__(self, path, book, state):
        self.book = book
        self.chapters = []
        self.verified = False
        # Highlights can calibrate the raw DOM convention for this book. Without
        # them, the device-verified root-index rule is used, and every observed
        # BOOX position must still agree with it and with the EPUB structure.
        self.calibrated = book.xpath_modes == {'document0'}
        with zipfile.ZipFile(path) as archive:
            rootfile = ET.fromstring(archive.read('META-INF/container.xml')).find('.//{*}rootfile').get('full-path')
            root = ET.fromstring(archive.read(rootfile))
            manifest = {item.get('id'): item.get('href') for item in root.find('{*}manifest')}
            total = 0
            for index, item in enumerate(root.find('{*}spine')):
                cid = item.get('idref')
                href = urllib.parse.unquote(urllib.parse.urlsplit(manifest[cid]).path)
                name = posixpath.normpath(posixpath.join(posixpath.dirname(rootfile), href))
                raw = archive.read(name)
                if index >= len(book.chapters) or book.chapters[index].name != name or book.ids.get(cid) != index:
                    raise ValueError('Native and Calibre chapter order differ')
                self.chapters.append({'id': cid, 'index': index, 'start': total,
                                      'end': total + len(raw), 'raw': raw, 'source': None})
                total += len(raw)
        if len(self.chapters) != len(book.chapters):
            raise ValueError('Native and Calibre chapter count differ')
        observed = []
        progress = state.get('progress') or {}
        for key in ('current_page_position_v2', 'current_page_position_v3'):
            if isinstance(progress.get(key), dict):
                observed.append(progress[key])
        for item in (state.get('annotations') or {}).get('active') or []:
            if item.get('document_id') != (state.get('boox_identity') or {}).get('document_id'):
                continue
            for key in ('location_begin', 'location_end', 'position_v2', 'position_v3'):
                if isinstance(item.get(key), dict):
                    observed.append(item[key])
        for item in (state.get('bookmarks') or {}).get('active') or []:
            if item.get('document_id') == (state.get('boox_identity') or {}).get('document_id'):
                for key in ('position_v2', 'position_v3'):
                    value = item.get(key)
                    if isinstance(value, str):
                        try:
                            from calibre_plugins.frankenboox.reader_bridge import object_value
                        except ImportError:
                            from reader_bridge import object_value
                        value = object_value(value)
                    if isinstance(value, dict) and value:
                        observed.append(value)
        evidence = set()
        for position in observed:
            chapter = self.chapter(position)
            route = tuple(position.get('xpath') or [])
            if not route:
                # Chapter bounds alone (e.g. an image-only cover) are still checked above.
                continue
            source = self.source(chapter)
            if not self.calibrated:
                if route[0] != self.root_index(chapter):
                    raise ValueError('Observed native document root differs')
                route = native_route(route, 1)
            slot = source.by_route.get(route)
            if slot:
                if position.get('nodePosition') != slot['source_offset']:
                    raise ValueError('Native source-coordinate convention differs')
                offset = position.get('textOffset')
                if type(offset) is not int or not 0 <= offset <= units(slot['text']):
                    raise ValueError('Observed native text offset is invalid')
                evidence.add((chapter['index'], route))
            elif route in source.elements:
                if position.get('nodePosition') != source.elements[route]:
                    raise ValueError('Observed native element coordinate differs')
            else:
                raise ValueError('Observed native source path differs')
        if self.calibrated and len(evidence) < 2:
            raise ValueError('Two independent native text nodes are required for source calibration')
        if not self.calibrated and not observed:
            raise ValueError('No BOOX position confirms this EPUB structure; open the book once on BOOX')
        self.verified = True

    def root_index(self, chapter):
        if self.calibrated:
            return 1
        index = native_root_index(chapter['raw'])
        if index is None:
            raise ValueError('Native document root for this chapter is not verified')
        return index

    def source(self, chapter):
        if chapter['source'] is None:
            chapter['source'] = SourceChapter(chapter['raw'])
        return chapter['source']

    def chapter(self, position):
        index = self.book.ids.get(position.get('chapterID'))
        if index is None or not 0 <= index < len(self.chapters):
            raise ValueError('Unknown native chapter identity')
        chapter = self.chapters[index]
        expected = (index, chapter['start'], chapter['end'])
        actual = tuple(position.get(key) for key in ('chapterIndex', 'chapterStartPosition', 'chapterEndPosition'))
        if any(type(v) is not int for v in actual) or actual != expected:
            raise ValueError('Native chapter bounds differ from the EPUB source')
        return chapter

    def target(self, cfi):
        match = re.fullmatch(r'epubcfi\(/([0-9]+)(/2(?:/[0-9]+)+):([0-9]+)\)', cfi or '')
        if not match or int(match[1]) % 2:
            raise ValueError('CFI is not a supported exact text position')
        index = int(match[1]) // 2 - 1
        if not 0 <= index < len(self.chapters):
            raise ValueError('CFI chapter is outside the EPUB')
        chapter = self.chapters[index]
        slot = self.source(chapter).slots.get(match[2])
        offset = int(match[3])
        if not slot or not 0 <= offset < units(slot['text']):
            raise ValueError('CFI text offset exceeds the source text slot')
        root = self.root_index(chapter)
        position = {'chapterIndex': index, 'chapterID': chapter['id'],
                    'chapterStartPosition': chapter['start'], 'chapterEndPosition': chapter['end'],
                    'nodePosition': slot['source_offset'], 'textOffset': offset,
                    'xpath': list(native_route(slot['route'], root))}
        # Round-trip through the forward mapper in the same root convention.
        modes = self.book.xpath_modes
        if not self.calibrated:
            self.book.xpath_modes = {'document0' if root == 1 else 'document0prefix%d' % root}
        try:
            anchor = self.book.position(position)
        finally:
            self.book.xpath_modes = modes
        if not anchor or anchor.get('calibre_cfi') != cfi:
            raise ValueError('Reverse position failed its exact CFI round trip')
        return {'position': position, 'raw_position': json.dumps(position, separators=(',', ':')),
                'anchor': anchor, 'cfi': cfi, 'method': 'verified-source-dom',
                'calibration': 'book highlights' if self.calibrated else 'device root rule'}

    def compare(self, target, current):
        target_chapter, current_chapter = self.chapter(target), self.chapter(current)
        if target_chapter['index'] != current_chapter['index']:
            return target_chapter['index'] - current_chapter['index']
        ranks = []
        for position, chapter in ((target, target_chapter), (current, current_chapter)):
            route = tuple(position.get('xpath') or [])
            if route and not self.calibrated:
                if route[0] != self.root_index(chapter):
                    raise ValueError('Native document root differs inside a chapter')
                route = native_route(route, 1)
            slot = self.source(chapter).by_route.get(route)
            if not slot or slot['source_offset'] != position.get('nodePosition'):
                raise ValueError('Native element positions cannot be compared inside a chapter')
            offset = position.get('textOffset')
            if type(offset) is not int or not 0 <= offset <= units(slot['text']):
                raise ValueError('Invalid native text offset')
            ranks.append((slot['source_offset'], offset))
        return (ranks[0] > ranks[1]) - (ranks[0] < ranks[1])
