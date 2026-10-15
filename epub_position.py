"""Map Calibre EPUB CFI positions to CrossPoint visible-text offsets and back."""

import hashlib
import html.entities
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile


NON_VISIBLE_TAGS = {'head', 'style', 'script', 'title', 'rp'}


def _tag(element):
    return element.tag.rsplit('}', 1)[-1].lower() if isinstance(element.tag, str) else ''


def _local_path(rootfile, href):
    href = urllib.parse.unquote(urllib.parse.urlsplit(href).path)
    return posixpath.normpath(posixpath.join(posixpath.dirname(rootfile), href))


def _xml_root(data):
    # Some EPUBs contain HTML named entities in XHTML even though EPUB XHTML
    # is XML. XML only predefines five entities; translate the other standard
    # HTML5 entities to numeric references so the text remains parseable.
    data = re.sub(
        rb'&([A-Za-z][A-Za-z0-9]+);',
        _xml_safe_entity,
        data,
    )
    parser = ET.XMLParser()
    return ET.fromstring(data, parser=parser)


def _xml_safe_entity(match):
    name = match.group(1).decode('ascii')
    if name in {'amp', 'lt', 'gt', 'apos', 'quot'}:
        return match.group(0)
    replacement = html.entities.html5.get(name + ';')
    if replacement is None:
        return match.group(0)
    return ''.join('&#{};'.format(ord(char)) for char in replacement).encode('ascii')


def _text_units_utf16(text):
    return len(text.encode('utf-16-le')) // 2


class EpubPositionMapper:
    """Read an EPUB package and build CFI paths for each visible text node.

    CrossPoint's saved 10-byte progress format counts Unicode code points in
    the visible text inside ``body`` and omits text in head/style/script/title/rp.
    This follows that model so a Calibre CFI can map to a useful X3 text anchor.
    """

    def __init__(self, epub_path):
        self.epub_path = epub_path
        self.zf = zipfile.ZipFile(epub_path, 'r')
        self.opf_path = self._read_package_path()
        self.manifest, self.spine_hrefs = self._read_spine()
        self._chapter_cache = {}

    def close(self):
        try:
            self.zf.close()
        except Exception:
            pass

    def _read_package_path(self):
        root = _xml_root(self.zf.read('META-INF/container.xml'))
        for element in root.iter():
            if _tag(element) == 'rootfile':
                full_path = element.attrib.get('full-path')
                if full_path:
                    return posixpath.normpath(urllib.parse.unquote(full_path))
        raise ValueError('EPUB container.xml has no rootfile')

    def _read_spine(self):
        root = _xml_root(self.zf.read(self.opf_path))
        manifest = {}
        spine_refs = []
        for element in root.iter():
            name = _tag(element)
            if name == 'item':
                item_id = element.attrib.get('id')
                href = element.attrib.get('href')
                if item_id and href:
                    manifest[item_id] = _local_path(self.opf_path, href)
            elif name == 'itemref':
                idref = element.attrib.get('idref')
                if idref:
                    spine_refs.append(idref)
        spine = [manifest[item_id] for item_id in spine_refs if item_id in manifest]
        if not spine:
            raise ValueError('EPUB package has an empty or invalid spine')
        return manifest, spine

    def spine_index_for_href(self, href):
        normalized = posixpath.normpath(urllib.parse.unquote(href))
        try:
            return self.spine_hrefs.index(normalized)
        except ValueError:
            return None

    def href_for_spine_index(self, spine_index):
        try:
            return self.spine_hrefs[int(spine_index)]
        except (IndexError, TypeError, ValueError):
            return None

    def offset_for_fraction(self, fraction):
        """Approximate a Calibre whole-book fraction as a spine/text offset.

        Calibre's pos_frac is evenly distributed across spine items. This is
        a coarse fallback for a CFI that cannot be resolved against the EPUB;
        exact CFI-to-text mapping is always attempted first.
        """
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            raise ValueError('Calibre whole-book progress fraction is invalid')
        if not self.spine_hrefs:
            raise ValueError('EPUB has no spine items')
        fraction = min(1.0, max(0.0, fraction))
        scaled = fraction * len(self.spine_hrefs)
        spine_index = min(int(scaled), len(self.spine_hrefs) - 1)
        within_spine = min(1.0, max(0.0, scaled - spine_index))
        chapter = self._chapter(spine_index)
        text_offset = int(chapter['text_length'] * within_spine)
        return spine_index, text_offset

    def chapter_digest(self, spine_index):
        href = self.href_for_spine_index(spine_index)
        if href is None:
            return None
        try:
            return hashlib.sha256(self.zf.read(href)).hexdigest()
        except KeyError:
            return None

    def _chapter(self, spine_index):
        spine_index = int(spine_index)
        if spine_index in self._chapter_cache:
            return self._chapter_cache[spine_index]
        href = self.href_for_spine_index(spine_index)
        if href is None:
            raise ValueError('CFI refers to a spine item outside this EPUB')
        root = _xml_root(self.zf.read(href))
        html = root if _tag(root) == 'html' else next(
            (el for el in root.iter() if _tag(el) == 'html'), root
        )
        parts = []
        flat_offset = 0

        def add(text, cfi_path, visible):
            nonlocal flat_offset
            if visible and text:
                start = flat_offset
                flat_offset += len(text)
                parts.append((start, flat_offset, cfi_path, text))

        def visit(element, cfi_path, in_body=False, hidden=False):
            name = _tag(element)
            inside = in_body or name == 'body'
            hidden_here = hidden or name in NON_VISIBLE_TAGS
            add(element.text, cfi_path + '/1', inside and not hidden_here)
            child_element_index = 0
            for child in element:
                if isinstance(child.tag, str):
                    child_element_index += 1
                    visit(child, cfi_path + '/' + str(child_element_index * 2), inside, hidden_here)
                # A tail is a text node in this element's child list, after the
                # preceding element. Comments do not consume element indexes.
                add(child.tail, cfi_path + '/' + str(child_element_index * 2 + 1), inside and not hidden_here)

        visit(html, '/2')
        chapter = {'href': href, 'root': html, 'parts': parts, 'text_length': flat_offset}
        self._chapter_cache[spine_index] = chapter
        return chapter

    def offset_to_cfi(self, spine_index, text_offset):
        """Return a Calibre-style full CFI for a CrossPoint visible-text offset."""
        spine_index = int(spine_index)
        text_offset = int(text_offset)
        if text_offset < 0:
            raise ValueError('text offset cannot be negative')
        chapter = self._chapter(spine_index)
        parts = chapter['parts']
        for start, stop, path, text in parts:
            if start <= text_offset < stop:
                local_units = _text_units_utf16(text[:text_offset - start])
                local = '{}:{}'.format(path, local_units)
                return 'epubcfi(/{}{})'.format(2 * (spine_index + 1), local)
        if parts and text_offset == chapter['text_length']:
            start, stop, path, text = parts[-1]
            return 'epubcfi(/{}{}:{})'.format(
                2 * (spine_index + 1), path, _text_units_utf16(text)
            )
        if text_offset == 0 and not parts:
            return 'epubcfi(/{}/2/4)'.format(2 * (spine_index + 1))
        raise ValueError('visible-text offset is outside this spine item')

    def verified_spine(self,cfi):
        """Validate the CFI before relying on its package reading order."""
        spine,steps,offset,spatial=_parse_cfi(cfi)
        if spatial is None:
            self.cfi_to_offset(cfi)
            return spine
        if any(not 0<=value<=100 for value in spatial):
            raise ValueError('Spatial reading positions have invalid coordinates')
        node=self._chapter(spine)['root']
        if not steps or steps[0]!=2 or offset is not None:
            raise ValueError('Spatial reading positions have no verified element root')
        for step in steps[1:]:
            children=[child for child in node if isinstance(child.tag,str)]
            if step<2 or step%2 or step//2-1>=len(children):
                raise ValueError('Spatial reading positions have an invalid element path')
            node=children[step//2-1]
        return spine

    def compare_cfis(self,left,right):
        """Compare verified sections first; never estimate spatial text offsets."""
        a,b=self.verified_spine(left),self.verified_spine(right)
        if a!=b:return (a>b)-(a<b)
        if left==right:return 0
        if _parse_cfi(left)[3] is not None or _parse_cfi(right)[3] is not None:
            raise ValueError('Spatial reading positions cannot be compared exactly within the same section')
        x,y=self.cfi_to_offset(left),self.cfi_to_offset(right)
        return (x>y)-(x<y)

    def cfi_to_offset(self, cfi):
        """Return ``(spine_index, visible_text_offset)`` or raise ValueError.

        Supports Calibre's established flat CFI form (the first step selects
        the spine item) and standard indirection form (``...!/...``).
        Range CFIs and media offsets are deliberately rejected.
        """
        spine_index, local_steps, text_offset, spatial_offset = _parse_cfi(cfi)
        chapter = self._chapter(spine_index)
        parts = chapter['parts']
        if not local_steps:
            return spine_index, 0

        numeric_path = '/' + '/'.join(str(n) for n in local_steps)
        last_step = local_steps[-1]
        if last_step % 2 == 1:
            candidates = [numeric_path]
            if not numeric_path.startswith('/2/') and not numeric_path.startswith('/2:'):
                candidates.append('/2' + numeric_path)
            for text_path in candidates:
                for start, stop, part_path, text in parts:
                    if part_path == text_path:
                        units = text_offset or 0
                        local_chars = _utf16_offset_to_chars(text, units)
                        if local_chars is None:
                            continue
                        return spine_index, start + local_chars

        # CFI can point at an element boundary instead of a text node. Gather
        # its visible descendant text in document order. Calibre commonly adds
        # a spatial offset to these element CFIs; the vertical percentage gives
        # a useful approximate position within the element's text.
        element_path = numeric_path
        if local_steps[-1] % 2 == 1:
            element_path = '/' + '/'.join(str(n) for n in local_steps[:-1])
        prefixes = [element_path]
        if not element_path.startswith('/2'):
            prefixes.append('/2' + element_path)
        descendants = [
            part for part in parts
            if any(part[2].startswith(prefix.rstrip('/') + '/') for prefix in prefixes)
        ]
        if descendants:
            total_chars = sum(stop - start for start, stop, _path, _text in descendants)
            if spatial_offset is not None:
                # CFI spatial percentages are measured from the top/left.
                # Reading flow follows the vertical axis; x does not represent
                # text progress. This is an estimate, not a layout-exact CFI.
                fraction = min(100.0, max(0.0, spatial_offset[1])) / 100.0
                target_chars = int(total_chars * fraction)
            elif text_offset is not None and last_step % 2 == 0:
                target_chars = _utf16_descendant_offset(descendants, text_offset)
            else:
                target_chars = 0
            remaining = min(total_chars, target_chars)
            for start, stop, _path, _text in descendants:
                length = stop - start
                if remaining <= length:
                    return spine_index, start + remaining
                remaining -= length
            start, stop, _path, _text = descendants[-1]
            return spine_index, stop
        raise ValueError('CFI text node could not be verified in the EPUB')


def _find_unescaped(text, char):
    in_assertion = False
    escaped = False
    for index, current in enumerate(text):
        if escaped:
            escaped = False
            continue
        if current == '^':
            escaped = True
        elif current == '[':
            in_assertion = True
        elif current == ']':
            in_assertion = False
        elif current == char and not in_assertion:
            return index
    return -1


def _parse_steps(path):
    """Parse CFI steps, assertions, and text or spatial offsets."""
    steps = []
    offset = None
    spatial = None
    cursor = 0
    while cursor < len(path):
        if path[cursor] != '/':
            if path[cursor] == ':' and steps:
                match = re.match(r':(\d+)', path[cursor:])
                if not match:
                    raise ValueError('unsupported CFI text offset')
                offset = int(match.group(1))
                cursor += len(match.group(0))
                # Text assertions may follow the numeric offset.
                if cursor < len(path) and path[cursor] == '[':
                    end = _assertion_end(path, cursor)
                    cursor = end + 1
                if cursor != len(path):
                    raise ValueError('unsupported CFI offset suffix {!r}'.format(path[cursor:]))
                break
            if path[cursor] == '@' and steps:
                match = re.match(r'@([0-9]+(?:\.[0-9]+)?):([0-9]+(?:\.[0-9]+)?)', path[cursor:])
                if not match:
                    raise ValueError('invalid CFI spatial offset')
                spatial = (float(match.group(1)), float(match.group(2)))
                cursor += len(match.group(0))
                if cursor != len(path):
                    raise ValueError('unsupported CFI spatial suffix {!r}'.format(path[cursor:]))
                break
            raise ValueError('unsupported CFI step syntax')
        cursor += 1
        match = re.match(r'\d+', path[cursor:])
        if not match:
            raise ValueError('invalid CFI step')
        steps.append(int(match.group(0)))
        cursor += len(match.group(0))
        if cursor < len(path) and path[cursor] == '[':
            end = _assertion_end(path, cursor)
            cursor = end + 1
        if cursor < len(path) and path[cursor] not in '/:@':
                raise ValueError('unsupported CFI step suffix {!r}'.format(path[cursor:]))
    return steps, offset, spatial


def _assertion_end(text, start):
    escaped = False
    for index in range(start + 1, len(text)):
        if escaped:
            escaped = False
        elif text[index] == '^':
            escaped = True
        elif text[index] == ']':
            return index
    raise ValueError('unterminated CFI assertion')


def _parse_cfi(cfi):
    if not isinstance(cfi, str) or not cfi.startswith('epubcfi(') or not cfi.endswith(')'):
        raise ValueError('not a full EPUB CFI')
    body = cfi[len('epubcfi('):-1]
    if _find_unescaped(body, ',') >= 0:
        raise ValueError('range CFIs are not resume positions')
    bang = _find_unescaped(body, '!')
    if bang >= 0:
        package, content = body[:bang], body[bang + 1:]
        package_steps, _package_text_offset, _package_spatial = _parse_steps(package)
        if not package_steps or package_steps[-1] < 2 or package_steps[-1] % 2:
            raise ValueError('CFI has no valid spine selector')
        spine_index = package_steps[-1] // 2 - 1
        local_steps, text_offset, spatial_offset = _parse_steps(content)
    else:
        steps, text_offset, spatial_offset = _parse_steps(body)
        if not steps or steps[0] < 2 or steps[0] % 2:
            raise ValueError('CFI has no valid spine selector')
        spine_index = steps[0] // 2 - 1
        local_steps = steps[1:]
    if spine_index < 0:
        raise ValueError('invalid CFI spine selector')
    return spine_index, local_steps, text_offset, spatial_offset


def _utf16_descendant_offset(parts, units):
    """Convert an element-relative CFI UTF-16 offset into visible code points."""
    if units < 0:
        raise ValueError('CFI text offset cannot be negative')
    consumed_units = 0
    consumed_chars = 0
    for _start, _stop, _path, text in parts:
        local = _utf16_offset_to_chars(text, units - consumed_units)
        if local is not None:
            return consumed_chars + local
        part_units = _text_units_utf16(text)
        if units < consumed_units + part_units:
            raise ValueError('CFI text offset splits a UTF-16 surrogate pair')
        consumed_units += part_units
        consumed_chars += len(text)
    if units == consumed_units:
        return consumed_chars
    raise ValueError('CFI text offset exceeds the element text')


def _utf16_offset_to_chars(text, units):
    if units < 0:
        return None
    consumed = 0
    for index, char in enumerate(text):
        if consumed == units:
            return index
        consumed += _text_units_utf16(char)
        if consumed > units:
            return None
    return len(text) if consumed == units else None
