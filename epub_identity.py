"""Conservative EPUB identity independent of ZIP packaging and reader notes.

Reading resources retain their exact bytes. Only the known Calibre bookmark
member and non-rendering OPF metadata are excluded; manifest order is irrelevant
but manifest IDs, reading order, layout metadata and all other files are retained.
"""
import hashlib
import io
import json
import posixpath
import xml.etree.ElementTree as ET
import zipfile

IDENTITY_SCHEMA = 'neomatrix-epub-reading-v1'
OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'
CONTAINER = 'urn:oasis:names:tc:opendocument:xmlns:container'
BOOKMARKS = 'META-INF/calibre_bookmarks.txt'
_BIBLIOGRAPHIC = {'identifier', 'title', 'creator', 'contributor', 'publisher',
                  'description', 'subject', 'date', 'rights', 'source', 'relation', 'coverage'}
_BIBLIOGRAPHIC_REFINEMENTS = {'role', 'file-as', 'display-seq', 'identifier-type',
                            'title-type', 'alternate-script'}


def _xml(data):
    declarations = data.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
        raise ValueError('EPUB package identity does not support XML entity declarations')
    return ET.fromstring(data)


def _text(value):
    return value if value and value.strip() else None


def _node(element):
    return [element.tag, sorted(element.attrib.items()), _text(element.text),
            [[_node(child), _text(child.tail)] for child in element]]


def _non_rendering_metadata(element):
    if element.tag in {'{' + DC + '}' + name for name in _BIBLIOGRAPHIC}:
        return True
    if element.tag == '{' + OPF + '}meta':
        name, prop = element.get('name', ''), element.get('property', '')
        return name.startswith('calibre:') or prop == 'dcterms:modified'
    return False


def _package(data, allow_metadata_changes=True):
    root = _xml(data)
    if root.tag != '{' + OPF + '}package':
        raise ValueError('Unsupported EPUB package structure')
    # Encrypted/obfuscated fonts may derive their key from this identifier.
    if allow_metadata_changes:
        root.attrib.pop('unique-identifier', None)
    metadata = root.find('{' + OPF + '}metadata')
    manifest = root.find('{' + OPF + '}manifest')
    spine = root.find('{' + OPF + '}spine')
    if metadata is None or manifest is None or spine is None:
        raise ValueError('Incomplete EPUB package structure')
    removed_ids = {e.get('id') for e in metadata if _non_rendering_metadata(e) and e.get('id')}
    for element in list(metadata):
        harmless_refinement = (element.tag == '{' + OPF + '}meta' and
                               element.get('property') in _BIBLIOGRAPHIC_REFINEMENTS and
                               element.get('refines', '').lstrip('#') in removed_ids)
        if allow_metadata_changes and (_non_rendering_metadata(element) or harmless_refinement):
            metadata.remove(element)
    ids = [e.get('id') for e in manifest]
    if any(e.tag != '{' + OPF + '}item' or not e.get('href') or not e.get('id') for e in manifest) or len(set(ids)) != len(ids):
        raise ValueError('Ambiguous EPUB manifest')
    if any(e.tag != '{' + OPF + '}itemref' or e.get('idref') not in ids for e in spine):
        raise ValueError('Invalid EPUB reading order')
    manifest[:] = sorted(manifest, key=lambda e: json.dumps(_node(e), ensure_ascii=False, sort_keys=True))
    return json.dumps(_node(root), ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _members(archive):
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if len(set(names)) != len(names):
        raise ValueError('EPUB has duplicate ZIP members')
    if any(name.startswith('/') or '\\' in name or posixpath.normpath(name) != name or name.startswith('../') for name in names):
        raise ValueError('EPUB has ambiguous ZIP paths')
    container = _xml(archive.read('META-INF/container.xml'))
    packages = [e.get('full-path') for e in container.iter('{' + CONTAINER + '}rootfile')]
    if not packages or len(set(packages)) != len(packages) or any(p not in names for p in packages):
        raise ValueError('EPUB package is missing or ambiguous')
    digest = hashlib.sha256(IDENTITY_SCHEMA.encode('ascii'))
    for name in sorted(names):
        if name == BOOKMARKS:
            continue
        data = archive.read(name)
        if name in packages:
            data = _package(data, allow_metadata_changes='META-INF/encryption.xml' not in names)
        encoded = name.encode('utf-8')
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def reading_digest(path):
    with zipfile.ZipFile(path) as archive:
        return _members(archive)


def identity_from_bytes(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        reading = _members(archive)
    return {'schema': IDENTITY_SCHEMA, 'sha256': hashlib.sha256(data).hexdigest(), 'reading_sha256': reading}
