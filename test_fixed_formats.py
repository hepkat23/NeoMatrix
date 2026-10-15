import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

from _vendor.pypdf import PdfWriter
from _vendor.pypdf.generic import NameObject,BooleanObject
import fixed_book
import fixed_sync
from boox_push import apply_position,PushError


class FakeDB:
    def __init__(self,path,fmt):
        self.path,self.fmt=str(path),fmt
        self.positions=[];self.data={};self.annotations=[]
    def format_abspath(self,bid,fmt):return self.path if fmt==self.fmt else None
    def get_custom_book_data(self,key):return copy.deepcopy(self.data.get(key,{}))
    def add_custom_book_data(self,key,values,delete_first=False):self.data.setdefault(key,{}).update(copy.deepcopy(values))
    def get_last_read_positions(self,bid,fmt,user):return copy.deepcopy(self.positions)
    def annotations_map_for_book(self,*args,**kwargs):return {}
    def merge_annotations_for_book(self,bid,fmt,annotations,**kwargs):self.annotations+=copy.deepcopy(annotations)
    def set_last_read_position(self,bid,fmt,**values):self.positions=[dict(values,format=fmt)]


class FixtureDevice:
    serial='FIXED-FORMAT-TEST'
    def __init__(self,path,row):self.path=Path(path);self.row=copy.deepcopy(row);self.notes={'note':'keep'};self.writes=0
    def query(self,uuid):return copy.deepcopy(self.row)
    def book_sha256(self,path):return fixed_book.digest_file(self.path)
    def copy_book_to(self,path,destination):shutil.copyfile(self.path,destination)
    def reader_provider_sha256(self):return fixed_sync.COMIC_PROVIDER_SHA256
    def stop_reader(self):pass
    def update(self,uuid,expected,values):
        if expected!=self.row:return 0
        self.row.update(values);self.writes+=1;return 1


def render_fixture(path,directory,digest):
    path=Path(path);root=Path(directory);nodes=[]
    if path.suffix.lower()=='.pdf':
        count=len(fixed_book._pdf_reader(path).pages)
        for page in range(count):nodes.append({'n':'p','a':[['id','page_'+str(page+1)]],'x':'Physical page '+str(page+1)})
    else:
        names,hashes=fixed_book.comic_pages(path,'CBZ')
        with zipfile.ZipFile(path) as archive:
            for page,name in enumerate(names):
                asset=str(page)+'.jpg';(root/asset).write_bytes(archive.read(name))
                nodes.append({'n':'img','a':[['id','page_'+str(page+1)],['src',asset]]})
    tree={'n':'html','c':[{'n':'head','c':[]},{'n':'body','c':nodes}]}
    (root/'book.json').write_text(json.dumps({'tree':tree}))
    (root/'calibre-book-manifest.json').write_text(json.dumps({'spine':['book.json']}))


class FixedFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.pdf=self.root/'book.pdf';self.cbz=self.root/'book.cbz'
        writer=PdfWriter();writer.add_blank_page(width=400,height=600);writer.add_blank_page(width=401,height=601)
        writer.write(self.pdf)
        with zipfile.ZipFile(self.cbz,'w') as archive:
            archive.writestr('scan002.jpg',b'second-image');archive.writestr('scan001.jpg',b'first-image')
        constants=types.ModuleType('calibre.db.constants');constants.EBOOK_VIEWER_USER='local';constants.EBOOK_VIEWER_DEVICE='calibre-desktop-viewer'
        render=types.ModuleType('calibre.srv.render_book');render.render_for_viewer=render_fixture
        modules={'calibre':types.ModuleType('calibre'),'calibre.srv':types.ModuleType('calibre.srv'),
                 'calibre.srv.render_book':render,'calibre.db':types.ModuleType('calibre.db'),'calibre.db.constants':constants}
        self.module_patch=patch.dict(sys.modules,modules);self.module_patch.start();self.addCleanup(self.module_patch.stop)
        self.sidecar=self.root/'viewer.json'
        self.sidecar_patch=patch.object(fixed_sync,'_viewer_sidecar_path',return_value=str(self.sidecar))
        self.sidecar_patch.start();self.addCleanup(self.sidecar_patch.stop)

    def fixture(self,fmt='PDF',page=0):
        path=self.pdf if fmt=='PDF' else self.cbz
        book=fixed_book.FixedBook(path,fmt)
        position=book.native_position(page)
        raw=json.dumps(position) if isinstance(position,dict) else str(position)
        identity={'metadata_uuid':'uuid','document_id':'doc','path':'/storage/card/book.'+fmt.lower(),'file_md5':'a'*32}
        backend={'doc_id':'doc','md5':'a'*32,'total_page':2,'current_page_position_v3':raw,'current_page_position_v2':raw,'layout_type':'singlePage'}
        row={'uuid':'uuid','type':fmt.lower(),'location':identity['path'],'lastAccess':'1000','hashTag':'a'*32,
             'extraAttributes':json.dumps({'backend':backend,'theme':'keep'})}
        state={'boox_identity':identity,'boox_book':{'format':fmt.lower()},'raw_metadata_row':copy.deepcopy(row),
               'progress':{'last_access_ms':1000,'last_access_utc':'1970-01-01T00:00:01+00:00','current_page_position_v3_raw':raw},
               'annotations':{'active':[{'note':'preserved source note'}]},'bookmarks':{'active':[]}}
        return book,FakeDB(path,fmt),state,FixtureDevice(path,row)

    def test_cbr_extracts_each_named_member_with_compiled_matcher(self):
        module=types.ModuleType('calibre.utils.unrar')
        module.names=lambda path: ['Scan0002.jpg','Scan0001.jpg']
        def extract(path,match):
            self.assertIsInstance(path,str)
            name=next(name for name in module.names(path) if match.search(name))
            return name,name.encode()
        module.extract_member=extract
        with patch.dict(sys.modules,{'calibre.utils.unrar':module}):
            names,hashes=fixed_book.comic_pages(self.root/'book.cbr','CBR')
        self.assertEqual(names,['Scan0001.jpg','Scan0002.jpg'])
        self.assertEqual(hashes,[hashlib.sha256(name.encode()).hexdigest() for name in names])

    def test_only_pdf_smoothness_rounding_is_normalized(self):
        self.assertEqual(fixed_book._pdf_value({'/SM':0.00100708}),fixed_book._pdf_value({'/SM':0.001007}))
        self.assertNotEqual(fixed_book._pdf_value({'/SM':0.001007}),fixed_book._pdf_value({'/SM':0.002007}))
        self.assertNotEqual(fixed_book._pdf_value({'/Width':0.00100708}),fixed_book._pdf_value({'/Width':0.001007}))

    def test_omitted_pdf_page_does_not_shift_later_page_numbers(self):
        writer=PdfWriter()
        for i in range(3):writer.add_blank_page(width=400+i,height=600)
        writer.write(self.pdf)
        def omitted(path,directory,digest):
            render_fixture(path,directory,digest)
            target=Path(directory)/'book.json';data=json.loads(target.read_text())
            del data['tree']['c'][1]['c'][1]
            target.write_text(json.dumps(data))
        with patch.object(sys.modules['calibre.srv.render_book'],'render_for_viewer',side_effect=omitted):
            book=fixed_book.FixedBook(self.pdf,'PDF')
        self.assertEqual(book.page_from_cfi(book.cfi(2)),2)
        with self.assertRaisesRegex(ValueError,'omitted'):book.cfi(1)

    def test_pdf_native_reference_is_not_a_display_page_number(self):
        book,db,state,device=self.fixture()
        self.assertEqual(book.native_page(book.native_position(1)),1)
        self.assertEqual(book.page_from_cfi(book.cfi(1)),1)

    def test_newer_but_earlier_calibre_pdf_does_not_rewind(self):
        book,db,state,device=self.fixture(page=1)
        db.positions=[{'cfi':book.cfi(0),'epoch':999999.0}]
        self.assertEqual(fixed_sync.preview(db,1,state,device)['status'],'unchanged')
        self.assertEqual(device.writes,0)

    def test_farther_older_pdf_position_is_ready(self):
        book,db,state,device=self.fixture()
        db.positions=[{'cfi':book.cfi(1),'epoch':0.001}]
        plan=fixed_sync.preview(db,1,state,device)
        self.assertEqual(plan['status'],'ready',plan)
        self.assertEqual(plan['position'],book.native_position(1))
        self.assertGreater(plan['calibre_epoch_ms'],1000)

    def test_farther_comic_push_backs_up_and_preserves_notes(self):
        book,db,state,device=self.fixture('CBZ')
        db.positions=[{'cfi':book.cfi(1),'epoch':0.001}]
        plan=fixed_sync.preview(db,1,state,device)
        self.assertEqual(plan['status'],'ready',plan)
        apply_position(plan,device,self.root/'backup.json')
        self.assertEqual(device.notes,{'note':'keep'})
        self.assertEqual(json.loads(device.row['extraAttributes'])['theme'],'keep')
        self.assertEqual(json.loads(device.row['extraAttributes'])['backend']['current_page_position_v3'],'1')
        self.assertTrue((self.root/'backup.json').exists())

    def test_device_race_stops_backed_up_push(self):
        book,db,state,device=self.fixture()
        db.positions=[{'cfi':book.cfi(1),'epoch':1.0}]
        plan=fixed_sync.preview(db,1,state,device)
        device.row['lastAccess']='2000'
        with self.assertRaises(PushError):apply_position(plan,device,self.root/'backup.json')
        self.assertEqual(device.writes,0)

    def test_comic_page_zero_is_valid_and_imported(self):
        book,db,state,device=self.fixture('CBZ')
        fixed_sync.verify_book(db,1,state,device)
        report=fixed_sync.integrate_reader(db,1,state)
        self.assertTrue(report['resume_applied'])
        self.assertEqual(db.positions[0]['format'],'CBZ')
        self.assertEqual(book.page_from_cfi(db.positions[0]['cfi']),0)

    def test_pdf_import_preserves_sidecar_notes_and_uses_pdf_format(self):
        book,db,state,device=self.fixture(page=1)
        self.sidecar.write_text(json.dumps([{'type':'bookmark','title':'keep','pos':book.cfi(0)}]))
        fixed_sync.verify_book(db,1,state,device)
        fixed_sync.integrate_reader(db,1,state)
        self.assertEqual(db.positions[0]['format'],'PDF')
        self.assertEqual(db.positions[0]['pos_frac'],1.0)
        self.assertTrue(any(x.get('title')=='keep' for x in json.loads(self.sidecar.read_text())))
        self.assertEqual(state['annotations']['active'][0]['note'],'preserved source note')

    def test_same_comic_page_preserves_spatial_viewer_position(self):
        book,db,state,device=self.fixture('CBZ',1)
        cfi=book.cfi(1)[:-1]+'@25:50)'
        db.positions=[{'cfi':cfi,'epoch':1.0}]
        fixed_sync.verify_book(db,1,state,device)
        report=fixed_sync.integrate_reader(db,1,state)
        self.assertFalse(report['resume_applied']);self.assertEqual(db.positions[0]['cfi'],cfi)

    def test_invalid_cfi_cannot_become_the_last_page(self):
        book,db,state,device=self.fixture()
        db.positions=[{'cfi':'epubcfi(/2/2/4/9999)','epoch':1.0}]
        self.assertEqual(fixed_sync.preview(db,1,state,device)['status'],'unsupported')
        with self.assertRaises(ValueError):book.page_from_cfi('epubcfi(/2/2/4/2/1:9999)')

    def test_out_of_range_and_boolean_comic_positions_rejected(self):
        book,db,state,device=self.fixture('CBZ')
        for value in [-1,2,True,'-1','NaN']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):book.native_page(value)

    def test_pdf_reference_not_in_verified_file_rejected(self):
        book,db,state,device=self.fixture()
        with self.assertRaises(ValueError):book.native_page({'pageReferenceId':'pdf-9999-0'})

    def test_pdf_content_identity_ignores_metadata_and_annotation_flag(self):
        before=fixed_book.pdf_pages(self.pdf)[1]
        writer=PdfWriter(clone_from=str(self.pdf));writer.add_metadata({'/Title':'Changed metadata'})
        writer.pages[0][NameObject('/ONYXAnnotsCloned')]=BooleanObject(True)
        target=self.root/'annotation-copy.pdf';writer.write(target)
        self.assertEqual(before,fixed_book.pdf_pages(target)[1])

    def test_pdf_geometry_change_is_a_different_reading_document(self):
        before=fixed_book.pdf_pages(self.pdf)[1]
        writer=PdfWriter(clone_from=str(self.pdf));writer.pages[0].mediabox.upper_right=(500,600)
        target=self.root/'changed.pdf';writer.write(target)
        self.assertNotEqual(before,fixed_book.pdf_pages(target)[1])

    def test_comic_order_uses_verified_numeric_names_not_archive_order(self):
        names,digests=fixed_book.comic_pages(self.cbz,'CBZ')
        self.assertEqual(names,['scan001.jpg','scan002.jpg'])
        self.assertEqual(digests[0],hashlib.sha256(b'first-image').hexdigest())

    def test_unknown_comic_name_collation_is_blocked(self):
        path=self.root/'mixed.cbz'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('cover.jpg',b'cover');archive.writestr('001.jpg',b'page')
        with self.assertRaises(ValueError):fixed_book.comic_pages(path,'CBZ')

    def test_missing_matching_format_never_uses_epub_instead(self):
        book,db,state,device=self.fixture()
        db.fmt='EPUB'
        report=fixed_sync.integrate_reader(db,1,state)
        self.assertEqual(report['resume_issue_kind'],'missing-matching-format')
        self.assertEqual(db.positions,[])

    def test_unknown_comic_firmware_does_not_get_a_guessed_order(self):
        book,db,state,device=self.fixture('CBZ')
        device.reader_provider_sha256=lambda:'b'*64
        db.positions=[{'cfi':book.cfi(1),'epoch':1.0}]
        self.assertEqual(fixed_sync.preview(db,1,state,device)['status'],'unsupported')


if __name__=='__main__':unittest.main()
