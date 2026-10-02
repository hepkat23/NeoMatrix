"""BOOX to Calibre viewer conversion. No ebook or device writes."""
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone, timedelta

ANCHOR_KEY = 'neomatrix_portable_anchors_v1'
REPORT_KEY = 'neomatrix_reader_report_v1'
NS = uuid.UUID('61885b90-e112-4d2f-aa1e-3476976da5e4')
try:
    from calibre_plugins.frankenboox.epub_identity import reading_digest, IDENTITY_SCHEMA
except ImportError:
    from epub_identity import reading_digest, IDENTITY_SCHEMA


def _extract_progress_fraction(progress):
    """Return BOOX page progress as Calibre's 0–1 fraction, or None if unknown."""
    if not isinstance(progress, dict):
        return None

    def fraction(current, total):
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               for value in (current, total)):
            return None
        try:
            current, total = float(current), float(total)
        except (ValueError, TypeError, OverflowError):
            return None
        if not (math.isfinite(current) and math.isfinite(total)):
            return None
        if total <= 0 or not 0 <= current <= total:
            return None
        return current / total

    # Archived BOOX progress uses "current/total"; structured pages are the fallback.
    raw = progress.get('raw')
    if isinstance(raw, str):
        match = re.fullmatch(r'\s*(\d+)\s*/\s*(\d+)\s*', raw)
        if match:
            try:
                result = fraction(*map(int, match.groups()))
            except (ValueError, OverflowError):
                result = None
            if result is not None:
                return result
    return fraction(progress.get('current_page'), progress.get('total_pages'))


def object_value(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except ValueError:
        # Normalize only numeric tokens outside quoted strings.
        pieces = re.split(r'("(?:\\.|[^"\\])*")', value)
        for i in range(0, len(pieces), 2):
            pieces[i] = re.sub(r'(?<=[\[:,])\s*(-?)0+(\d+)', r'\1\2', pieces[i])
        try:
            result = json.loads(''.join(pieces))
            return result if isinstance(result, dict) else {}
        except ValueError:
            return {}


def utc_stamp(value, fallback=None):
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            d = datetime.fromtimestamp(int(value) / 1000, timezone.utc)
        else:
            d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        return d.isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return fallback or '1970-01-01T00:00:00+00:00'


def tag(el):
    return el.tag.rsplit('}', 1)[-1] if isinstance(el.tag, str) else ''


def utf16(text):
    return len(text.encode('utf-16-le')) // 2


class Chapter:
    def __init__(self, name, index, root, position_root=None):
        self.name, self.index, self.root = name, index, root
        self.parts = []
        # Each text slot records a DOM-compatible CFI and flattened text interval.
        self.text = ''
        def add(text, path):
            if text:
                start = len(self.text)
                self.text += text
                self.parts.append((start, len(self.text), path, text))
        def visit(el, path):
            if tag(el) in ('script', 'style', 'head'):
                return
            add(el.text, path + '/1')
            elements = 0
            for child in el:
                if isinstance(child.tag, str):
                    elements += 1
                    visit(child, path + '/' + str(elements * 2))
                # Comments do not increment element index; their tail shares a text slot.
                add(child.tail, path + '/' + str(elements * 2 + 1))
        # Calibre CFIs include /2 for the document's html element.
        visit(root, '/2')
        # Whitespace-normalized text with back-pointers to the original characters.
        out, positions = [], []
        for i, ch in enumerate(self.text):
            if ch.isspace():
                if out and out[-1] == ' ':
                    continue
                ch = ' '
            out.append(ch); positions.append(i)
        self.normalized, self.positions = ''.join(out), positions
        self.paths = {}
        body = next((el for el in root if tag(el) == 'body'), root)
        # NeoReader counts whitespace DOM nodes and includes the document's
        # html step. Keep that source tree separate from Calibre's normalized tree;
        # an observed highlight must verify a mode before any resume uses it.
        for origin, start_el in (('html', root), ('body', body),
                                ('document', position_root if position_root is not None else root)):
            for base in (0,1):
                mode = origin + str(base)
                mapping = {}; self.paths[mode] = mapping
                def walk(el, route, cfi_path):
                    slots = []
                    if el.text:
                        slots.append(('text',el.text,cfi_path+'/1'))
                    count = 0
                    for child in el:
                        if isinstance(child.tag,str):
                            count += 1
                            slots.append(('element',child,cfi_path+'/'+str(count*2)))
                        else:
                            slots.append(('comment',child,''))
                        if child.tail:
                            slots.append(('text',child.tail,cfi_path+'/'+str(count*2+1)))
                    first = next((a for a,b,p,t in self.parts if p.startswith(cfi_path+'/')),None)
                    if first is not None:
                        mapping[tuple(route)] = first
                    for j,(kind,value,path) in enumerate(slots):
                        child_route = route+[j+base]
                        if kind == 'element':
                            walk(value,child_route,path)
                        elif kind == 'text':
                            hits = [a for a,b,p,t in self.parts if p == path]
                            if len(hits) == 1:
                                mapping[tuple(child_route)] = hits[0]
                body_index = [el for el in root if isinstance(el.tag,str)].index(body)+1 if body is not root else 1
                walk(start_el, [1] if origin == 'document' else [],
                     '/2/'+str(body_index*2) if origin == 'body' else '/2')

    def resolve(self, position, mode):
        if not isinstance(position, dict):
            return None
        path = position.get('xpath')
        if not isinstance(path,list) or not path:
            return None
        start = self.paths[mode].get(tuple(path))
        if start is None:
            return None
        try:
            count = int(position.get('textOffset',0))
        except (ValueError,TypeError):
            return None
        if count < 0:
            return None
        # BOOX/Java offsets count UTF-16 code units.
        stop = next((b for a,b,p,t in self.parts if a <= start < b), start)
        used = 0
        for offset in range(start,stop+1):
            if used == count:
                return offset if offset < len(self.text) else None
            if offset == len(self.text):
                break
            used += utf16(self.text[offset])
            if used > count:
                break
        return None

    def local_cfi(self, offset, end=False):
        for start, stop, path, text in self.parts:
            if (start < offset <= stop) if end else (start <= offset < stop):
                # Adjacent text fragments (e.g. around comments) share CFI text index.
                earlier = sum(utf16(t) for a,b,p,t in self.parts if p == path and a < start)
                return path + ':' + str(earlier + utf16(text[:offset-start]))
        raise ValueError('No text at the requested location')

    def full_cfi(self, offset):
        return 'epubcfi(/' + str(2 * (self.index + 1)) + self.local_cfi(offset) + ')'

    def find_quote(self, quote):
        needle = re.sub(r'\s+', ' ', quote).strip()
        if not needle:
            return []
        results, start = [], 0
        while True:
            pos = self.normalized.find(needle, start)
            if pos < 0:
                break
            results.append((self.positions[pos], self.positions[pos+len(needle)-1]+1))
            start = pos + 1
        return results

    def anchor(self, offset, method):
        return {'spine_index':self.index, 'href':self.name, 'text_offset':offset,
                'offset_unit':'unicode-codepoints', 'text_model':'body-dom-text-v1',
                'chapter_text_sha256':hashlib.sha256(self.text.encode()).hexdigest(),
                'context':self.text[max(0,offset-40):offset+80], 'method':method,
                'calibre_cfi':self.full_cfi(offset)}


class Book:
    def __init__(self, chapters, ids, digest, reading_sha256=None):
        self.chapters, self.ids, self.digest = chapters, ids, digest
        self.reading_digest = reading_sha256
        self.xpath_modes = set()

    def calibrate(self, state):
        modes = None
        for item in recovered_annotations(state):
            pos = object_value(item.get('location_begin'))
            if not pos.get('xpath'):
                continue
            match = self.quote(item.get('quote') or '', pos)
            if not match:
                continue
            chapter, start, end = match
            valid = {mode for mode in chapter.paths if chapter.resolve(pos,mode) == start}
            if valid:
                modes = valid if modes is None else modes & valid
        self.xpath_modes = modes or set()

    def chapter_for(self, position):
        if not isinstance(position, dict):
            return None
        cid = position.get('chapterID')
        if isinstance(cid, str) and cid in self.ids:
            return self.chapters[self.ids[cid]]
        return None

    def quote(self, quote, position):
        chapter = self.chapter_for(position)
        candidates = [chapter] if chapter else self.chapters
        matches = [(c,a,b) for c in candidates for a,b in c.find_quote(quote)]
        return matches[0] if len(matches) == 1 else None

    def position(self, position):
        chapter = self.chapter_for(position)
        if not chapter or not chapter.text.strip():
            return None
        candidates = {chapter.resolve(position, mode) for mode in self.xpath_modes}
        candidates.discard(None)
        if len(candidates) == 1:
            return chapter.anchor(candidates.pop(), 'quote-calibrated-boox-xpath')
        # A zero node/text offset is the only BOOX numerical convention validated
        # independently of rendering. Never interpret arbitrary BOOX byte offsets as text.
        if position.get('nodePosition') == 0 and position.get('textOffset') == 0:
            offset = len(chapter.text) - len(chapter.text.lstrip())
            return chapter.anchor(offset, 'chapter-start')
        return None


def load_book(path):
    from calibre.ebooks.oeb.iterator.book import extract_book
    from calibre.srv.render_book import SimpleContainer, create_cover_page, ensure_body
    from calibre.utils.logging import default_log
    import tempfile
    with open(path, 'rb') as stream:
        digest = hashlib.sha256(stream.read()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='neomatrix-epub-') as directory:
        book_fmt, opfpath, input_fmt = extract_book(path, directory, log=default_log)
        container = SimpleContainer(directory, opfpath, default_log)
        create_cover_page(container, 'epub', False)
        names = [name for name, linear in container.spine_names]
        chapters = []
        for i,name in enumerate(names):
            root = container.parsed(name)
            ensure_body(root)
            position_root = copy.deepcopy(root)
            root.text = root.tail = None
            for child in tuple(root):
                if isinstance(child.tag,str) and tag(child) not in ('head','body'):
                    root.remove(child)
            chapters.append(Chapter(name,i,root,position_root))
        manifest = {el.get('id'):container.href_to_name(el.get('href'), container.opf_name)
                    for el in container.opf.xpath('//*[local-name()="manifest"]/*[local-name()="item"]')}
        ids = {ident:names.index(name) for ident,name in manifest.items() if name in names}
    try:
        reading_sha256 = reading_digest(path)
    except Exception:
        # Calibre can read some nonstandard packages. Keep their import behavior;
        # equivalence-based pushes require a valid conservative fingerprint.
        reading_sha256 = None
    return Book(chapters, ids, digest, reading_sha256)


def stable_id(identity, item, kind):
    key = item.get('annotation_uuid') or item.get('uuid') or item.get('guid')
    if not key:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
    return str(uuid.uuid5(NS, str(identity) + ':' + kind + ':' + str(key)))


def recovered_annotations(state):
    block = state.get('annotations') or {}
    raw = {str(r.get('uuid')):r for r in block.get('raw_rows',[]) if r.get('uuid')}
    for original in block.get('active',[]):
        item = dict(original)
        row = raw.get(str(item.get('annotation_uuid')), {})
        if not item.get('location_begin'):
            item['location_begin'] = object_value(row.get('locationBegin'))
        if not item.get('location_end'):
            item['location_end'] = object_value(row.get('locationEnd'))
        yield item


def _viewer_sidecar_path(epub_path):
    """Return Calibre viewer's per-book annotation sidecar path.

    Calibre's internal viewer does not read ``last-read`` from the library
    annotations table: it loads that position from this sidecar (or from the
    EPUB itself).  NeoMatrix never changes the EPUB, so this is the safe,
    user-local location for an imported resume position.
    """
    from calibre.gui2.viewer.annotations import annotations_dir
    from calibre.gui2.viewer.ui import path_key

    directory = annotations_dir()
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, path_key(epub_path) + '.json')


def _sidecar_entries(path):
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            raw = json.load(stream)
        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, dict) and item.get('type')]
    except (OSError, ValueError, TypeError):
        return []


def _timestamp_seconds(value):
    try:
        return datetime.fromisoformat(utc_stamp(value)).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return 0.0


def _write_sidecar_resume(epub_path, resume, timestamp, force=False):
    """Persist one viewer resume position and report whether it won.

    Returns ``(applied, displaced_last_read)``.  The write is atomic and keeps
    every existing viewer highlight/bookmark entry intact.
    """
    path = _viewer_sidecar_path(epub_path)
    entries = _sidecar_entries(path)
    incoming = {
        'type': 'last-read',
        'pos_type': 'epubcfi',
        'pos': resume['calibre_cfi'],
        'timestamp': utc_stamp(timestamp),
    }
    old = [entry for entry in entries if entry.get('type') == 'last-read']
    previous = max(old, key=lambda entry: _timestamp_seconds(entry.get('timestamp')), default=None)
    if previous and not force and _timestamp_seconds(previous.get('timestamp')) > _timestamp_seconds(incoming['timestamp']):
        return False, previous

    kept = [entry for entry in entries if entry.get('type') != 'last-read']
    kept.append(incoming)
    payload = json.dumps(kept, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix='.neomatrix-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return True, previous


def convert(state, book):
    annotations, anchors, skipped = [], [], []
    book.calibrate(state)
    identity = (state.get('boox_identity') or {}).get('document_id') or book.digest
    for item in recovered_annotations(state):
        uid = stable_id(identity, item, 'highlight')
        when = utc_stamp(item.get('updated_at_ms') or item.get('created_at_ms'))
        if item.get('status') not in (None, 0, '0'):
            continue
        match = book.quote(item.get('quote') or '', object_value(item.get('location_begin')))
        if not match:
            skipped.append('Highlight ' + uid + ': text absent or ambiguous in the EPUB'); continue
        chapter, start, end = match
        annotations.append({'type':'highlight', 'uuid':uid, 'timestamp':when,
                            'highlighted_text':item.get('quote') or '', 'notes':item.get('note') or '',
                            'start_cfi':chapter.local_cfi(start), 'end_cfi':chapter.local_cfi(end, True),
                            'spine_name':chapter.name, 'spine_index':chapter.index,
                            'style':{'type':'builtin', 'kind':'color', 'which':'yellow'}})
        anchors.append({'id':uid, 'kind':'highlight', 'start':chapter.anchor(start,'unique-quote'),
                        'end_cfi':chapter.local_cfi(end,True)})
    for item in (state.get('bookmarks') or {}).get('active', []):
        uid = stable_id(identity,item,'bookmark')
        position = object_value(item.get('position_v3') or item.get('position_v2') or item.get('chapter_or_position'))
        anchor = book.position(position)
        if not anchor and item.get('quote'):
            match = book.quote(item['quote'],position)
            if match:
                anchor = match[0].anchor(match[1],'unique-quote')
        if not anchor:
            skipped.append('Bookmark '+uid+': unsupported location'); continue
        title = 'BOOX: ' + (item.get('title') or item.get('quote') or 'Bookmark') + ' [' + uid[:8] + ']'
        annotations.append({'type':'bookmark','title':title,'pos_type':'epubcfi',
                            'pos':anchor['calibre_cfi'],'timestamp':utc_stamp(item.get('updated_at_ms') or item.get('created_at_ms'))})
        anchors.append({'id':uid,'kind':'bookmark','start':anchor})
    progress = state.get('progress') or {}
    backend = object_value(object_value((state.get('raw_metadata_row') or {}).get('extraAttributes')).get('backend'))
    resume = None
    resume_positions = []
    for key in ('current_page_position_v3','current_page_position_v2','backend_current_page'):
        position = object_value(progress.get(key) or progress.get(key+'_raw') or backend.get(key.replace('backend_','')))
        if position:
            resume_positions.append(position)
        resume = book.position(position)
        if resume:
            break
    # NeoReader often creates a bookmark at the exact current reading
    # position. If the proprietary DOM path cannot be resolved directly,
    # use that bookmark's unique quote as a verified text anchor. This is
    # still deterministic: the structured position must match exactly and
    # the quote must occur once in the EPUB.
    if not resume and resume_positions:
        for item in (state.get('bookmarks') or {}).get('active', []):
            quote = item.get('quote') or ''
            bookmark_position = object_value(
                item.get('position_v3') or item.get('position_v2') or item.get('chapter_or_position')
            )
            if not quote or not bookmark_position:
                continue
            if not any(bookmark_position == candidate for candidate in resume_positions):
                continue
            match = book.quote(quote, bookmark_position)
            if match:
                resume = match[0].anchor(match[1], 'resume-from-bookmark-quote')
                break
    if resume:
        when = utc_stamp(progress.get('last_access_ms') or progress.get('last_access_utc'))
        resume = dict(resume, timestamp=when)
        annotations.extend([
            {'type':'last-read','pos_type':'epubcfi','pos':resume['calibre_cfi'],'timestamp':when},
            {'type':'bookmark','title':'BOOX resume position','pos_type':'epubcfi','pos':resume['calibre_cfi'],'timestamp':when}])
    else:
        skipped.append('Resume position: BOOX location could not be verified; existing Calibre position retained')
    return annotations, {'schema':1,'epub_sha256':book.digest,
                         'epub_identity_schema':IDENTITY_SCHEMA,
                         'epub_reading_sha256':book.reading_digest,
                         'resume':resume,'annotations':anchors}, skipped


def integrate_reader(db, book_id, state, use_boox_position=False, keep_boox_position=False):
    path = db.format_abspath(book_id, 'EPUB')
    if not path:
        report = {'mapped':0,'resume':False,'skipped':['Native reader integration currently supports EPUB only']}
        db.add_custom_book_data(REPORT_KEY,{book_id:report},delete_first=False)
        return report
    book = load_book(path)
    annotations, portable, skipped = convert(state,book)
    resume_applied = False
    resume_retained = False
    resume_kept_by_choice = False
    if use_boox_position and portable['resume']:
        current_map = db.annotations_map_for_book(book_id, 'EPUB', user_type='local', user='viewer')
        previous = current_map.get('last-read') or []
        # This explicit user action is a new reading-position choice. Preserve the
        # displaced viewer position as a bookmark, leaving all other notes intact.
        now = datetime.now(timezone.utc)
        for old in previous:
            stamp = datetime.fromisoformat(utc_stamp(old.get('timestamp')))
            now = max(now, stamp + timedelta(microseconds=1))
        for annotation in annotations:
            if annotation.get('type') == 'last-read' or annotation.get('title') == 'BOOX resume position':
                annotation['timestamp'] = now.isoformat()
    if portable['resume'] and keep_boox_position and not use_boox_position:
        # The user explicitly chose to keep the current Calibre position. The
        # BOOX resume remains available as the imported bookmark, but the
        # viewer's last-read sidecar is not changed even if BOOX is newer.
        resume_kept_by_choice = True
    elif portable['resume']:
        try:
            apply_timestamp = now.isoformat() if use_boox_position else portable['resume'].get('timestamp')
            resume_applied, previous = _write_sidecar_resume(
                path,
                portable['resume'],
                apply_timestamp or (state.get('progress') or {}).get('last_access_ms'),
                force=use_boox_position,
            )
            resume_retained = not resume_applied and previous is not None
            if use_boox_position and previous and previous.get('pos') != portable['resume']['calibre_cfi']:
                old_pos = previous.get('pos')
                if old_pos:
                    old_id = hashlib.sha256(old_pos.encode('utf-8')).hexdigest()[:8]
                    annotations.append({
                        'type': 'bookmark',
                        'title': 'Calibre position before BOOX import [' + old_id + ']',
                        'pos_type': previous.get('pos_type', 'epubcfi'),
                        'pos': old_pos,
                        'timestamp': now.isoformat() if use_boox_position else previous.get('timestamp'),
                    })
            if resume_applied and hasattr(db, 'set_last_read_position'):
                try:
                    from calibre.db.constants import EBOOK_VIEWER_DEVICE, EBOOK_VIEWER_USER
                    pos_frac = _extract_progress_fraction(state.get('progress'))
                    if pos_frac is None:
                        skipped.append('BOOX progress was missing or invalid; existing Calibre position index retained.')
                    else:
                        db.set_last_read_position(
                            book_id, 'EPUB', user=EBOOK_VIEWER_USER,
                            device=EBOOK_VIEWER_DEVICE,
                            cfi=portable['resume']['calibre_cfi'],
                            epoch=_timestamp_seconds(apply_timestamp), pos_frac=pos_frac,
                        )
                except Exception as error:
                    skipped.append('Viewer resume sidecar was updated, but Calibre position index was not: ' + str(error))
        except Exception as error:
            skipped.append('Calibre viewer resume sidecar could not be updated: ' + str(error))
    # Preserve all existing viewer annotations and let source timestamps resolve edits.
    # Never replace an entire annotation set or stamp an old BOOX read as "now".
    library_annotations = [annotation for annotation in annotations if annotation.get('type') != 'last-read']
    if library_annotations:
        db.merge_annotations_for_book(book_id,'EPUB',library_annotations,user_type='local',user='viewer')
    if portable['resume'] and resume_kept_by_choice:
        skipped.append('Existing Calibre position was kept by the sync choice. Use Tools → Use BOOX reading position to apply the BOOX position explicitly.')
    elif portable['resume'] and resume_retained:
        skipped.append('A newer Calibre position was retained. Use Tools → Use BOOX reading position to apply the BOOX position explicitly.')
    report = {'mapped':len(portable['annotations']), 'resume':bool(portable['resume']),
              'resume_applied':resume_applied, 'skipped':skipped}
    db.add_custom_book_data(ANCHOR_KEY,{book_id:portable},delete_first=False)
    db.add_custom_book_data(REPORT_KEY,{book_id:report},delete_first=False)
    return report
