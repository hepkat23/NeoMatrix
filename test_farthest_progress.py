import json, tempfile, unittest, zipfile
from pathlib import Path
from unittest.mock import patch
from epub_position import EpubPositionMapper
from library_push import farthest_position
import reader_bridge as bridge
import sync_flow
from types import SimpleNamespace
from progress_lock import progress_lock

def make_epub(path):
    container = b'''<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>'''
    opf = b'''<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>A Test Book</dc:title><dc:creator>Writer</dc:creator></metadata>
      <manifest><item id="c1" href="Text/one.xhtml" media-type="application/xhtml+xml"/>
        <item id="c2" href="Text/two.xhtml" media-type="application/xhtml+xml"/></manifest>
      <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
    </package>'''
    one = '''<?xml version="1.0"?>
    <html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ignore this</title></head>
      <body><p>Hello 😀 world</p><p>Second paragraph</p></body></html>'''.encode()
    two = '''<?xml version="1.0"?>
    <html xmlns="http://www.w3.org/1999/xhtml"><head><title>Title</title></head>
      <body><p>Chapter two text.</p></body></html>'''.encode()
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('META-INF/container.xml', container)
        zf.writestr('OPS/package.opf', opf)
        zf.writestr('OPS/Text/one.xhtml', one)
        zf.writestr('OPS/Text/two.xhtml', two)


class FarthestProgress(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'book.epub';make_epub(self.path)
        self.sidecar=Path(self.tmp.name)/'position.json'
        self.mapper=EpubPositionMapper(self.path);self.addCleanup(self.mapper.close)
        self.patch=patch.object(bridge,'_viewer_sidecar_path',return_value=str(self.sidecar));self.patch.start();self.addCleanup(self.patch.stop)
    def cfi(self,chapter,offset):return self.mapper.offset_to_cfi(chapter,offset)
    def write_old(self,cfi):
        self.sidecar.write_text(json.dumps([{'type':'last-read','pos':cfi,'pos_type':'epubcfi','timestamp':'2026-10-01T00:00:00+00:00'}, {'type':'bookmark','pos':cfi,'title':'Keep me'}]))
    def test_two_progress_writers_cannot_hold_the_same_book_lock(self):
        with progress_lock(self.sidecar):
            with self.assertRaises(RuntimeError):
                with progress_lock(self.sidecar):pass
        with progress_lock(self.sidecar):pass
    def test_sidecar_change_during_write_is_not_overwritten(self):
        self.write_old(self.cfi(0,2));original=bridge._sidecar_entries
        changed=False
        def race(path):
            nonlocal changed
            if changed:return original(path)
            changed=True
            rows=original(path)
            self.sidecar.write_text(json.dumps([{'type':'last-read','pos':self.cfi(1,8),'timestamp':'2026-10-02T00:00:00+00:00'}]))
            return rows
        with patch.object(bridge,'_sidecar_entries',side_effect=race):
            with self.assertRaises(ValueError):bridge._write_sidecar_resume(self.path,{'calibre_cfi':self.cfi(1,1)},'2026-10-02T00:00:00+00:00')
        self.assertEqual(json.loads(self.sidecar.read_text())[0]['pos'],self.cfi(1,8))

    def test_farthest_calibre_source_wins_despite_older_time(self):
        earlier=self.cfi(0,2);farther=self.cfi(1,1)
        got=farthest_position([{'cfi':earlier,'epoch':2000},{'cfi':farther,'epoch':1000}],self.path)
        self.assertEqual(got['cfi'],farther)
    def test_newer_boox_position_cannot_rewind_calibre(self):
        farther=self.cfi(1,1);self.write_old(farther);before=self.sidecar.read_bytes()
        applied,_=bridge._write_sidecar_resume(self.path,{'calibre_cfi':self.cfi(0,10)},'2026-10-02T00:00:00+00:00')
        self.assertFalse(applied);self.assertEqual(self.sidecar.read_bytes(),before)
    def test_older_boox_position_advances_calibre_and_preserves_notes(self):
        self.write_old(self.cfi(0,2))
        applied,_=bridge._write_sidecar_resume(self.path,{'calibre_cfi':self.cfi(1,1)},'2026-09-01T00:00:00+00:00')
        self.assertTrue(applied);self.assertTrue(any(x.get('title')=='Keep me' for x in json.loads(self.sidecar.read_text())))
    def test_farther_parallel_index_prevents_sidecar_rewind(self):
        self.write_old(self.cfi(0,2));before=self.sidecar.read_bytes()
        applied,_=bridge._write_sidecar_resume(self.path,{'calibre_cfi':self.cfi(0,8)},'2026-10-02T00:00:00+00:00',current_position={'cfi':self.cfi(1,1),'epoch':1000})
        self.assertFalse(applied);self.assertEqual(self.sidecar.read_bytes(),before)
    def test_equal_location_does_not_rewrite(self):
        cfi=self.cfi(0,8);self.write_old(cfi);before=self.sidecar.read_bytes()
        self.assertFalse(bridge._write_sidecar_resume(self.path,{'calibre_cfi':cfi},'2026-10-02T00:00:00+00:00')[0]);self.assertEqual(self.sidecar.read_bytes(),before)
    def test_unresolvable_existing_location_stops_comparison(self):
        self.write_old('epubcfi(/2/2/4/999/1:0)');before=self.sidecar.read_bytes()
        with self.assertRaises(ValueError):bridge._write_sidecar_resume(self.path,{'calibre_cfi':self.cfi(1,1)},'2026-10-02T00:00:00+00:00')
        self.assertEqual(self.sidecar.read_bytes(),before)
    def test_same_chapter_unicode_offset_ordering(self):
        self.write_old(self.cfi(0,8));before=self.sidecar.read_bytes()
        self.assertFalse(bridge._write_sidecar_resume(self.path,{'calibre_cfi':self.cfi(0,2)},'2026-10-02T00:00:00+00:00')[0]);self.assertEqual(self.sidecar.read_bytes(),before)
    def review(self,local,device):
        owner=SimpleNamespace(db=SimpleNamespace(format_abspath=lambda *a:str(self.path),field_for=lambda *a:'Book'))
        state={'boox_identity':{'metadata_uuid':'test'},'raw_metadata_row':{}}
        entries=[(7,state,{})]
        portable={'resume':{'calibre_cfi':device} if device else None}
        with patch.object(sync_flow,'calibre_position',return_value={'cfi':local,'epoch_ms':1000}), patch.object(bridge,'load_book'), patch.object(bridge,'convert',return_value=([],portable,[])), patch.object(sync_flow,'preview',return_value={'status':'ready','book_path':'/book.epub'}):
            return sync_flow.prepare_sync(owner,entries,lambda *a:None,device_factory=lambda:object())
    def test_review_imports_farther_boox_regardless_of_time(self):
        result=self.review(self.cfi(0,2),self.cfi(1,1));self.assertNotIn(7,result['keep']);self.assertEqual(result['plans'],[]);self.assertEqual(result['pull'],[7])
    def test_review_pushes_farther_calibre_regardless_of_time(self):
        result=self.review(self.cfi(1,1),self.cfi(0,2));self.assertTrue(result['keep'][7]);self.assertEqual(len(result['plans']),1)
    def test_review_never_pushes_over_unmapped_boox_position(self):
        result=self.review(self.cfi(1,1),None);self.assertTrue(result['keep'][7]);self.assertEqual(result['plans'],[])
    def test_review_equal_locations_do_not_push(self):
        result=self.review(self.cfi(0,8),self.cfi(0,8));self.assertEqual(result['plans'],[])

    def test_explicit_override_can_restore_an_earlier_location(self):
        self.write_old(self.cfi(1,1))
        self.assertTrue(bridge._write_sidecar_resume(self.path,{'calibre_cfi':self.cfi(0,2)},'2026-10-02T00:00:00+00:00',force=True)[0])

    def test_spatial_cfi_is_not_compared_using_approximate_text_order(self):
        cfi=self.cfi(0,2).rsplit('/',1)[0]+'@25:50)'
        with self.assertRaisesRegex(ValueError,'Spatial reading positions'):
            farthest_position([{'cfi':cfi,'epoch':2000}],self.path)

    def test_incoming_spatial_cfi_does_not_overwrite_exact_position(self):
        self.write_old(self.cfi(0,2));before=self.sidecar.read_bytes()
        cfi=self.cfi(0,2).rsplit('/',1)[0]+'@25:50)'
        with self.assertRaisesRegex(ValueError,'Spatial reading positions'):
            bridge._write_sidecar_resume(self.path,{'calibre_cfi':cfi},'2026-10-02T00:00:00+00:00')
        self.assertEqual(self.sidecar.read_bytes(),before)

if __name__=='__main__':unittest.main()
