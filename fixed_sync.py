"""Two-way PDF/comic progress using verified physical pages and native records."""
import copy
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

try:
    from calibre_plugins.frankenboox.fixed_book import FixedBook,FORMATS,SCHEMA,digest_file
    from calibre_plugins.frankenboox.reader_bridge import (REPORT_KEY,READER_MAPPING_VERSION,
        _viewer_sidecar_path,_sidecar_entries,_timestamp_seconds,utc_stamp,stable_id,object_value)
    from calibre_plugins.frankenboox.reader_push import _backend
    from calibre_plugins.frankenboox.push_values import md5_value,timestamp_ms
except ImportError:
    from fixed_book import FixedBook,FORMATS,SCHEMA,digest_file
    from reader_bridge import (REPORT_KEY,READER_MAPPING_VERSION,_viewer_sidecar_path,
        _sidecar_entries,_timestamp_seconds,utc_stamp,stable_id,object_value)
    from reader_push import _backend
    from push_values import md5_value,timestamp_ms

FIXED_KEY='neomatrix_fixed_page_mapping_v1'
COMIC_PROVIDER_SHA256='539b26bdb3ffecd91a22a0dbe753af43c0e11f65f61d1de851c4516d65500b13'


def format_of(state):
    return str((state.get('boox_book') or {}).get('format') or '').upper()


def resume_value(backend):
    values=[backend[key] for key in ('current_page_position_v3','current_page_position_v2','current_page')
            if key in backend and backend[key] is not None and backend[key]!='']
    if not values:raise ValueError('BOOX has no saved physical-page position')
    return values[0]


def _stored_context(db,bid,fmt):
    return ((db.get_custom_book_data(FIXED_KEY).get(bid) or {}).get(fmt) or {})


def _book_from_context(path,fmt,context):
    if context.get('schema')!=SCHEMA or context.get('format')!=fmt or digest_file(path)!=context.get('calibre_sha256'):
        raise ValueError('Fixed-page mapping is missing or the Calibre file changed; connect BOOX and Sync')
    book=FixedBook(path,fmt)
    if book.reading_digest!=context.get('reading_sha256') or book.count!=context.get('page_count'):
        raise ValueError('Calibre fixed-page content changed after verification')
    references=context.get('native_references')
    if not isinstance(references,list) or len(references)!=book.count or len(set(references))!=book.count:
        raise ValueError('Verified native page table is missing or ambiguous')
    book.native_references=references
    return book


def verify_book(db,bid,state,device,current_row=None,status=lambda text:None):
    fmt=format_of(state)
    path=db.format_abspath(bid,fmt)
    if fmt not in FORMATS or not path:
        raise ValueError('Calibre needs the matching '+fmt+' file; different formats are not interchangeable')
    identity=state.get('boox_identity') or {}
    row=current_row if current_row is not None else device.query(identity.get('metadata_uuid'))
    _,_,backend=_backend(row)
    if (row.get('uuid')!=identity.get('metadata_uuid') or row.get('location')!=identity.get('path')
            or str(row.get('type','')).upper()!=fmt or backend.get('doc_id')!=identity.get('document_id')
            or md5_value(backend.get('md5') or row.get('hashTag'))!=md5_value(identity.get('file_md5'))):
        raise ValueError('BOOX fixed-page copy identity changed; pull fresh data')
    if fmt in {'CBZ','CBR'} and device.reader_provider_sha256()!=COMIC_PROVIDER_SHA256:
        raise ValueError('Comic page ordering needs verification on this NeoReader firmware')
    device_digest=device.book_sha256(identity['path'])
    context=_stored_context(db,bid,fmt)
    if (context.get('device_sha256')==device_digest and context.get('identity')==identity
            and context.get('calibre_sha256')==digest_file(path)):
        book=_book_from_context(path,fmt,context)
    else:
        status('Verifying '+fmt+' pages and reading content')
        with tempfile.TemporaryDirectory(prefix='neomatrix-fixed-native-') as directory:
            native=Path(directory)/('book.'+fmt.lower())
            device.copy_book_to(identity['path'],native)
            if digest_file(native)!=device_digest or device.book_sha256(identity['path'])!=device_digest:
                raise ValueError('BOOX book changed during fixed-page verification')
            book=FixedBook(path,fmt,native)
        context=book.portable(0)
        context.update(device_sha256=device_digest,identity=copy.deepcopy(identity))
    page=book.native_page(resume_value(backend))
    for key in ('current_page_position_v3','current_page_position_v2','current_page'):
        if key in backend and backend[key] is not None and backend[key]!='' and book.native_page(backend[key])!=page:
            raise ValueError('NeoReader physical-page fields disagree')
    if int(backend.get('total_page') or 0)!=book.count:
        raise ValueError('NeoReader page count differs from the verified document')
    context.update(page_index=page,calibre_cfi=book.cfi(page),last_access_ms=timestamp_ms(row['lastAccess']))
    state['fixed_format_mapping']=copy.deepcopy(context)
    return book,context,row


def calibre_position(db,bid,path,fmt,book):
    from calibre.db.constants import EBOOK_VIEWER_USER
    records=list(db.get_last_read_positions(bid,fmt=fmt,user=EBOOK_VIEWER_USER))
    records += [x for x in db.annotations_map_for_book(bid,fmt,user_type='local',user='viewer').get('last-read',[])
                if x.get('pos_type')=='epubcfi']
    records += [x for x in _sidecar_entries(_viewer_sidecar_path(path))
                if x.get('type')=='last-read' and x.get('pos_type')=='epubcfi']
    candidates=[]
    for record in records:
        cfi=record.get('cfi') or record.get('pos')
        if not cfi:continue
        page=book.page_from_cfi(cfi)
        epoch=record.get('epoch') if 'epoch' in record else _timestamp_seconds(record.get('timestamp'))
        if type(epoch) not in (int,float):epoch=0
        candidates.append((page,epoch,cfi))
    if not candidates:raise ValueError('No saved Calibre '+fmt+' reading position')
    page,epoch,cfi=max(candidates)
    return {'cfi':cfi,'epoch_ms':round(epoch*1000),'page_index':page,'format':fmt}


def preview(db,bid,state,device,current_row=None,status=lambda text:None,prepared=None):
    try:
        book,context,row=prepared if prepared is not None else verify_book(db,bid,state,device,current_row,status)
        fmt=book.format;path=book.path
        local=calibre_position(db,bid,path,fmt,book)
        page=local['page_index'];current=context['page_index']
        if page<=current:return {'status':'unchanged','reason':'BOOX already has the farthest physical page'}
        if timestamp_ms(row['lastAccess'])!=timestamp_ms((state.get('progress') or {}).get('last_access_ms')):
            return {'status':'conflict','reason':'BOOX changed since collection; no position sent'}
        saved_backend=_backend(state.get('raw_metadata_row') or {})[2]
        if book.native_page(resume_value(saved_backend))!=current:
            return {'status':'conflict','reason':'BOOX page changed since collection'}
        identity=state['boox_identity']
        position=book.native_position(page)
        raw=json.dumps(position,separators=(',',':')) if isinstance(position,dict) else str(position)
        return {'status':'ready','reason':'Verified '+fmt+' page '+str(page+1)+' is ready for push',
                'position_kind':'pdf-page' if fmt=='PDF' else 'comic-page','format':fmt,
                'serial':device.serial,'metadata_uuid':identity['metadata_uuid'],
                'document_id':identity['document_id'],'book_path':identity['path'],
                'file_md5':md5_value(identity['file_md5']),'file_sha256':context['device_sha256'],
                'expected_extra_attributes':row['extraAttributes'],'expected_last_access':row['lastAccess'],
                'position':position,'position_raw':raw,'page_index':page,'page_count':book.count,
                'calibre_epoch_ms':max(local['epoch_ms'],timestamp_ms(row['lastAccess'])+1),
                'calibre_position':local,'calibre_sha256':book.digest,
                'fixed_format_mapping':context}
    except Exception as error:
        return {'status':'unsupported','reason':str(error)}


def _write_sidecar(path,book,page,timestamp,force=False,current=None):
    sidecar=_viewer_sidecar_path(path)
    entries=_sidecar_entries(sidecar)
    old=[entry for entry in entries if entry.get('type')=='last-read']
    candidates=old+([current] if current else [])
    ranks=[]
    for entry in candidates:
        cfi=entry.get('pos') or entry.get('cfi')
        if cfi:ranks.append((book.page_from_cfi(cfi),cfi,entry))
    previous=max(ranks,key=lambda x:x[0])[2] if ranks else None
    if not force and ranks and page<=max(x[0] for x in ranks):return False,previous
    incoming={'type':'last-read','pos_type':'epubcfi','pos':book.cfi(page),'timestamp':utc_stamp(timestamp)}
    kept=[entry for entry in entries if entry.get('type')!='last-read']+[incoming]
    payload=json.dumps(kept,ensure_ascii=False,separators=(',',':')).encode()
    fd,temporary=tempfile.mkstemp(prefix='.neomatrix-fixed-',suffix='.json',dir=os.path.dirname(sidecar))
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,sidecar)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    return True,previous


def integrate_reader(db,bid,state,use_boox_position=False,keep_boox_position=False):
    fmt=format_of(state);path=db.format_abspath(bid,fmt)
    if not path:
        reason='Calibre has no matching '+fmt+' file; original BOOX reading data is archived'
        report={'mapping_version':READER_MAPPING_VERSION,'format':fmt,'mapped':0,'resume':False,
                'resume_applied':False,'resume_issue':reason,'resume_issue_kind':'missing-matching-format','skipped':[reason]}
        db.add_custom_book_data(REPORT_KEY,{bid:report},delete_first=False)
        return report
    context=state.get('fixed_format_mapping') or _stored_context(db,bid,fmt)
    if context.get('identity')!=(state.get('boox_identity') or {}):
        raise ValueError('Connect BOOX and Sync to verify this fixed-page copy')
    book=_book_from_context(path,fmt,context)
    progress=state.get('progress') or {}
    raw=next((progress[key] for key in ('current_page_position_v3_raw','current_page_position_v2_raw')
              if progress.get(key) is not None),None)
    if raw is None:
        raw=next((progress[key] for key in ('current_page_position_v3','current_page_position_v2')
                  if progress.get(key) is not None),None)
    # Zero is a valid comic index and must never disappear through truthiness.
    if raw is None:raise ValueError('Archived fixed-page position is missing')
    page=book.native_page(raw)
    annotations=[]
    skipped=[]
    for item in (state.get('bookmarks') or {}).get('active') or []:
        if item.get('document_id')!=(state.get('boox_identity') or {}).get('document_id'):continue
        values=[item.get(key) for key in ('position_v3','position_v2','chapter_or_position') if item.get(key) is not None]
        try:
            target=next(book.native_page(value) for value in values)
        except Exception:
            skipped.append('A BOOX bookmark has no verified physical page; original data kept')
            continue
        annotations.append({'type':'bookmark','uuid':stable_id(state['boox_identity'],item,'fixed-bookmark'),
                            'title':item.get('title') or item.get('quote') or 'BOOX bookmark',
                            'pos_type':'epubcfi','pos':book.cfi(target),
                            'timestamp':utc_stamp(item.get('updated_at_ms') or item.get('created_at_ms'))})
    local=None
    try:local=calibre_position(db,bid,path,fmt,book)
    except ValueError as error:
        if not str(error).startswith('No saved Calibre '):raise
    timestamp=progress.get('last_access_utc') or progress.get('last_access_ms')
    if use_boox_position:
        epoch=max(datetime.now(timezone.utc).timestamp(),(local or {}).get('epoch_ms',0)/1000+0.001)
        timestamp=datetime.fromtimestamp(epoch,timezone.utc).isoformat()
    applied=False
    if not keep_boox_position or use_boox_position:
        try:
            from calibre_plugins.frankenboox.progress_lock import progress_lock
        except ImportError:
            from progress_lock import progress_lock
        with progress_lock(_viewer_sidecar_path(path)):
            applied,previous=_write_sidecar(path,book,page,timestamp,force=use_boox_position,current=local)
        if use_boox_position and previous:
            displaced=previous.get('pos') or previous.get('cfi')
            if displaced and displaced!=book.cfi(page):
                annotations.append({'type':'bookmark','uuid':stable_id(state['boox_identity'],{'uuid':displaced},'fixed-previous'),
                                    'title':'Calibre position before BOOX import','pos_type':'epubcfi','pos':displaced,'timestamp':utc_stamp(timestamp)})
        if applied:
            from calibre.db.constants import EBOOK_VIEWER_USER,EBOOK_VIEWER_DEVICE
            db.set_last_read_position(bid,fmt,user=EBOOK_VIEWER_USER,device=EBOOK_VIEWER_DEVICE,
                cfi=book.cfi(page),epoch=_timestamp_seconds(timestamp),pos_frac=(page+1)/book.count)
    if annotations:db.merge_annotations_for_book(bid,fmt,annotations,user_type='local',user='viewer')
    stored=copy.deepcopy(db.get_custom_book_data(FIXED_KEY).get(bid) or {})
    stored[fmt]=context;db.add_custom_book_data(FIXED_KEY,{bid:stored},delete_first=False)
    report={'mapping_version':READER_MAPPING_VERSION,'format':fmt,'mapped':len(annotations),'resume':True,
            'resume_applied':applied,'resume_issue':None,'resume_issue_kind':None,'skipped':skipped}
    db.add_custom_book_data(REPORT_KEY,{bid:report},delete_first=False)
    return report
