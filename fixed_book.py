"""Exact physical-page identities and Calibre-viewer mappings for PDF/comics."""
from bisect import bisect_right
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import tempfile
import urllib.parse
import zipfile

FORMATS = {'PDF', 'CBZ', 'CBR'}
SCHEMA = 'neomatrix-fixed-pages-v1'


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _pdf_reader(path):
    try:
        from calibre_plugins.frankenboox._vendor.pypdf import PdfReader
    except ImportError:
        from _vendor.pypdf import PdfReader
    reader = PdfReader(path, strict=True)
    if reader.is_encrypted:
        raise ValueError('Encrypted PDFs need separate password support')
    return reader


def _pdf_value(value, stack=None):
    """Canonicalize reading resources; object numbers/stream packing aren't content."""
    stack = set() if stack is None else stack
    if hasattr(value, 'get_object'):
        value = value.get_object()
    if value is None or type(value).__name__ == 'NullObject':
        return None
    if hasattr(value, 'get_data'):
        return ['stream', hashlib.sha256(value.get_data()).hexdigest(),
                _pdf_value({k:v for k,v in value.items() if k not in ('/Length','/Filter','/DecodeParms')}, stack)]
    if isinstance(value, dict):
        marker = id(value)
        if marker in stack:
            raise ValueError('Cyclic PDF resources cannot be fingerprinted safely')
        stack.add(marker)
        try:
            result=[]
            for k,v in sorted(value.items(),key=lambda item:str(item[0])):
                # NeoReader rewrites PDF graphics-state smoothness at six
                # decimal places. Normalize only this rendering tolerance;
                # fonts, images, transforms and all other resources stay exact.
                if str(k)=='/SM' and isinstance(v,(int,float)):
                    number=Decimal(str(v))
                    if not number.is_finite() or not 0<=number<=1:
                        raise ValueError('Invalid PDF smoothness')
                    v=float(number.quantize(Decimal('0.000001')))
                result.append([str(k),_pdf_value(v,stack)])
            return result
        finally:
            stack.remove(marker)
    if isinstance(value, (list,tuple)):
        return [_pdf_value(v, stack) for v in value]
    if isinstance(value, bytes):
        return ['bytes', value.hex()]
    if isinstance(value, str):
        return ['text', str(value)]
    if type(value).__name__ == 'BooleanObject':
        return ['bool', bool(value.value)]
    if isinstance(value, (int,float)):
        number=Decimal(str(value))
        if not number.is_finite():raise ValueError('Non-finite PDF reading value')
        return ['number',str(number.normalize())]
    raise ValueError('Unsupported PDF resource value: '+type(value).__name__)


def pdf_pages(path):
    reader = _pdf_reader(path)
    references, hashes = [], []
    for page in reader.pages:
        reference = page.indirect_reference
        if reference is None:
            raise ValueError('PDF page has no native object reference')
        references.append('pdf-'+str(reference.idnum)+'-'+str(reference.generation))
        # Include inherited resources and geometry; omit only annotations and
        # document metadata, which must not move the underlying reading page.
        # BOOX's annotation writer round-trips geometry through float32. Four
        # decimal points of a PDF point retain far sub-pixel geometry precision.
        geometry=lambda box:[float(Decimal(str(n)).quantize(Decimal('0.0001'))) for n in box]
        content = {'media': geometry(page.mediabox), 'crop': geometry(page.cropbox),
                   'rotation': page.rotation, 'resources': page.get('/Resources'),
                   'contents': page.get_contents().get_data() if page.get_contents() else b'',
                   'reading_keys': {k:v for k,v in page.items() if k not in
                       ('/Parent','/Annots','/Metadata','/StructParents','/Tabs','/PieceInfo',
                        '/LastModified','/MediaBox','/CropBox','/Rotate','/Resources','/Contents','/ONYXAnnotsCloned')}}
        canonical = json.dumps(_pdf_value(content),ensure_ascii=False,separators=(',',':')).encode()
        hashes.append(hashlib.sha256(canonical).hexdigest())
    if not references or len(set(references)) != len(references):
        raise ValueError('PDF page references are missing or ambiguous')
    return references, hashes


IMAGE_EXTENSIONS = {'.jpg','.jpeg','.png','.webp','.bmp','.gif'}


def natural_key(name):
    return [(1,int(part)) if part.isdigit() else (0,part.casefold())
            for part in re.split(r'(\d+)',name)]


def comic_pages(path, fmt):
    if fmt == 'CBZ':
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            wanted = [name for name in names if Path(name).suffix.lower() in IMAGE_EXTENSIONS
                      and not any(part.startswith('.') or part=='__MACOSX' for part in Path(name).parts)]
            if len(set(wanted)) != len(wanted):
                raise ValueError('Comic archive has duplicate image names')
            entries = [(name,hashlib.sha256(archive.read(name)).hexdigest()) for name in wanted]
    elif fmt == 'CBR':
        from calibre.utils.unrar import names, extract_member
        wanted = [name for name in names(str(path)) if Path(name).suffix.lower() in IMAGE_EXTENSIONS
                  and not any(part.startswith('.') or part=='__MACOSX' for part in Path(name).parts)]
        if len(set(wanted)) != len(wanted):
            raise ValueError('Comic archive has duplicate image names')
        entries = []
        for name in wanted:
            result = extract_member(str(path), match=re.compile("^"+re.escape(name)+"$"))
            if not isinstance(result,tuple) or len(result)!=2 or not isinstance(result[1],bytes):
                raise ValueError('CBR image extraction could not be verified')
            entries.append((name,hashlib.sha256(result[1]).hexdigest()))
    else:
        raise ValueError('Unsupported comic archive format')
    if not entries:
        raise ValueError('Comic archive contains no supported page images')
    # Inspected NeoReader ZIP/RAR code sorts by parent and then natural numeric
    # basename order, independently of archive entry order. Locale collators and
    # mixed-folder naming need separate verification; uniform page-name patterns
    # make every nonnumeric comparison identical and remove that ambiguity.
    parents={str(Path(name).parent) for name,_ in entries}
    if len(parents)!=1:
        raise ValueError('Comic folder ordering needs native verification')
    def compare_names(a,b):
        i=j=0
        while i<len(a) and j<len(b):
            if a[i].isascii() and a[i].isdigit() and b[j].isascii() and b[j].isdigit():
                ai=i;bj=j
                while i<len(a) and a[i].isascii() and a[i].isdigit():i+=1
                while j<len(b) and b[j].isascii() and b[j].isdigit():j+=1
                x,y=int(a[ai:i]),int(b[bj:j])
                if x!=y:return (x>y)-(x<y)
            elif a[i]==b[j]:i+=1;j+=1
            else:
                raise ValueError('Comic filename collation needs native verification')
        return (len(a)>len(b))-(len(a)<len(b))
    ordered=sorted(entries,key=lambda item:natural_key(Path(item[0]).name))
    for left,right in zip(ordered,ordered[1:]):
        if compare_names(Path(left[0]).name,Path(right[0]).name)>=0:
            raise ValueError('Comic numeric page order is ambiguous')
    return [name for name,_ in ordered], [digest for _,digest in ordered]


class FixedBook:
    def __init__(self, path, fmt, native_path=None):
        self.path, self.format = str(path), fmt.upper()
        if self.format not in FORMATS:
            raise ValueError('Unsupported fixed-page format')
        self.digest = digest_file(path)
        if self.format=='PDF':
            self.references, self.page_hashes = pdf_pages(path)
        else:
            self.references, self.page_hashes = comic_pages(path,self.format)
        self.count = len(self.page_hashes)
        self.reading_digest = hashlib.sha256(json.dumps(self.page_hashes,separators=(',',':')).encode()).hexdigest()
        if native_path:
            if self.format=='PDF':
                native_references,native_hashes=pdf_pages(native_path)
            else:
                native_references,native_hashes=comic_pages(native_path,self.format)
            if native_hashes != self.page_hashes:
                raise ValueError('BOOX and Calibre fixed-page reading content differs')
            self.native_references = native_references
        else:
            self.native_references = self.references
        self.markers = self._viewer_markers()

    def _viewer_markers(self):
        from calibre.srv.render_book import render_for_viewer
        markers = {}
        self.viewer_roots = {}
        with tempfile.TemporaryDirectory(prefix='neomatrix-fixed-viewer-') as directory:
            render_for_viewer(self.path,directory,self.digest)
            root=Path(directory)
            manifest=json.loads((root/'calibre-book-manifest.json').read_text())
            for spine,name in enumerate(manifest['spine']):
                document=json.loads((root/name).read_text())
                self.viewer_roots[spine]=document['tree']
                def walk(node,route):
                    attrs=dict(node.get('a') or [])
                    match=re.fullmatch(r'page_(\d+)',attrs.get('id',''))
                    if match:
                        page=int(match[1])-1
                        if not 0<=page<self.count:raise ValueError('Calibre page marker is outside the document')
                        if page in markers:raise ValueError('Duplicate Calibre page marker')
                        markers[page]=(spine,tuple(route))
                        if self.format in {'CBZ','CBR'}:
                            if node.get('n')!='img' or not 0<=page<self.count:
                                raise ValueError('Comic page marker is not a verified image')
                            image=Path(urllib.parse.unquote(attrs.get('data-calibre-src') or attrs.get('src') or ''))
                            if image.is_absolute() or '..' in image.parts or not image.parts:
                                raise ValueError('Invalid Calibre comic image path')
                            if digest_file(root/image)!=self.page_hashes[page]:
                                raise ValueError('Calibre and BOOX comic image order/content differs')
                    index=0
                    for child in node.get('c') or []:
                        if child.get('n'):
                            index+=1;walk(child,route+[index*2])
                walk(document['tree'],[2])
            if not markers or (self.format!='PDF' and set(markers)!=set(range(self.count))):
                raise ValueError('Calibre conversion did not retain every physical page marker')
        self.marker_pages=sorted(markers)
        ordered=[markers[i] for i in self.marker_pages]
        self.marker_ranks=ordered
        if ordered!=sorted(ordered) or len(set(ordered))!=len(ordered):
            raise ValueError('Calibre physical page order is ambiguous')
        return [markers.get(i) for i in range(self.count)]

    def cfi(self,page):
        if type(page) is not int or not 0<=page<self.count:
            raise ValueError('Page is outside the document')
        if self.markers[page] is None:
            raise ValueError('Calibre viewer omitted this PDF page; position cannot be mapped')
        spine,route=self.markers[page]
        return 'epubcfi(/'+str((spine+1)*2)+''.join('/'+str(n) for n in route)+')'

    def page_from_cfi(self,cfi):
        try:
            from calibre_plugins.frankenboox.epub_position import _parse_cfi
        except ImportError:
            from epub_position import _parse_cfi
        spine,steps,offset,spatial=_parse_cfi(cfi)
        self._validate_cfi(spine,steps,offset,spatial)
        rank=(spine,tuple(steps))
        if spine not in {value[0] for value in self.marker_ranks}:
            raise ValueError('Calibre position is outside the mapped document')
        # Exact physical page granularity: spatial offsets inside an image do
        # not become a different physical page or a guessed text offset.
        index=bisect_right(self.marker_ranks,rank)-1
        if index<0:raise ValueError('Calibre position is before the first verified physical page')
        return self.marker_pages[index]

    def _validate_cfi(self,spine,steps,offset,spatial):
        if not steps or steps[0]!=2 or spine not in self.viewer_roots:
            raise ValueError('Calibre position has no verified document root')
        node=self.viewer_roots[spine]
        text=None
        for index,step in enumerate(steps[1:]):
            children=[child for child in node.get('c') or [] if child.get('n')]
            if step%2:
                if index!=len(steps)-2:
                    raise ValueError('CFI descends through a text node')
                number=(step-1)//2
                if number==0:text=node.get('x') or ''
                elif 0<number<=len(children):text=children[number-1].get('l') or ''
                else:raise ValueError('CFI text slot is outside the document')
            else:
                number=step//2-1
                if not 0<=number<len(children):raise ValueError('CFI element is outside the document')
                node=children[number]
        if text is not None:
            length=len(text.encode('utf-16-le'))//2
            if offset is not None and not 0<=offset<=length:
                raise ValueError('CFI text offset exceeds its text slot')
        elif offset:
            raise ValueError('Element-relative text offsets need separate verification')
        if spatial and text is not None:
            raise ValueError('Spatial CFI points to a text slot')

    def native_page(self,value):
        if self.format=='PDF':
            if isinstance(value,str):value=json.loads(value)
            if not isinstance(value,dict) or set(value)!={'pageReferenceId'}:
                raise ValueError('Unsupported PDF position shape')
            reference=value['pageReferenceId']
            if reference not in self.native_references:
                raise ValueError('PDF page reference is outside the verified BOOX file')
            return self.native_references.index(reference)
        if isinstance(value,str) and re.fullmatch(r'\d+',value):value=int(value)
        if type(value) is not int or not 0<=value<self.count:
            raise ValueError('Comic page index is outside the verified archive')
        return value

    def native_position(self,page):
        self.cfi(page)
        return {'pageReferenceId':self.native_references[page]} if self.format=='PDF' else page

    def portable(self,page):
        return {'schema':SCHEMA,'format':self.format,'page_index':page,'page_count':self.count,
                'reading_sha256':self.reading_digest,'calibre_sha256':self.digest,
                'calibre_cfi':self.cfi(page),'native_references':self.native_references}
