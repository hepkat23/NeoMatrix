"""Review farthest verified positions during normal Sync; push only after approval."""
import hashlib
from pathlib import Path
from uuid import uuid4
try:
    from calibre_plugins.frankenboox.library_push import calibre_position, preview, PUSH_KEY
    from calibre_plugins.frankenboox.boox_push import AdbReadingDevice, apply_position, PushError
    from calibre_plugins.frankenboox.reader_bridge import ANCHOR_KEY
    from calibre_plugins.frankenboox.reader_push import _backend, _resume
except ImportError:
    from library_push import calibre_position, preview, PUSH_KEY
    from boox_push import AdbReadingDevice, apply_position, PushError
    from reader_bridge import ANCHOR_KEY
    from reader_push import _backend, _resume


def prepare_sync(owner, entries, status, device_factory=None):
    """Compare verified locations; never let a timestamp alone rewind a reader."""
    try:
        from calibre_plugins.frankenboox.reader_bridge import load_book, convert
        from calibre_plugins.frankenboox.epub_position import EpubPositionMapper
    except ImportError:
        from reader_bridge import load_book, convert
        from epub_position import EpubPositionMapper
    plans, keep, issues, device, pull = [], {}, [], None, []
    for bid, current, files in entries:
        fmt=str((current.get('boox_book') or {}).get('format') or '').upper()
        if fmt in {'PDF','CBZ','CBR'}:
            title=owner.db.field_for('title',bid) or str(bid)
            try:
                try:
                    from calibre_plugins.frankenboox import fixed_sync
                except ImportError:
                    import fixed_sync
                if device is None:
                    if device_factory:device=device_factory()
                    else:
                        from calibre_plugins.frankenboox.main import find_adb
                        adb=find_adb()
                        if not adb:raise PushError('Connect BOOX and install Android Platform Tools')
                        device=AdbReadingDevice(adb)
                status('Comparing '+fmt+' pages: '+title)
                prepared=fixed_sync.verify_book(owner.db,bid,current,device,current.get('raw_metadata_row'),status)
                book,context,row=prepared
                try:
                    local=fixed_sync.calibre_position(owner.db,bid,book.path,fmt,book)
                except ValueError as error:
                    if str(error).startswith('No saved Calibre '):pull.append(bid);continue
                    raise
                if local['page_index']<context['page_index']:pull.append(bid);continue
                if local['page_index']==context['page_index']:continue
                keep[bid]=True
                plan=fixed_sync.preview(owner.db,bid,current,device,prepared=prepared)
                if plan['status']=='ready':plans.append({'book_id':bid,'title':title,'path':plan['book_path'],'plan':plan})
                elif plan['status']!='unchanged':issues.append(title+': '+plan['reason']+'; Calibre position kept')
            except Exception as error:
                keep[bid]=True
                issues.append(title+': '+str(error)+'; positions kept unchanged')
            continue
        path = owner.db.format_abspath(bid, 'EPUB')
        if not path:
            continue
        title = owner.db.field_for('title', bid) or str(bid)
        try:
            position = calibre_position(owner.db, bid, path)
        except ValueError as error:
            if str(error).startswith('No saved Calibre EPUB reading position'):
                continue
            keep[bid] = True
            issues.append(title + ': ' + str(error) + '; positions kept unchanged')
            continue
        except Exception as error:
            keep[bid] = True
            issues.append(title + ': could not compare Calibre position; positions kept unchanged')
            continue
        mapper = None
        try:
            status('Comparing reading locations: ' + title)
            book = load_book(path)
            _, portable, _ = convert(current, book)
            boox = portable.get('resume')
            # A book opened on BOOX but still on its image-only cover is at the
            # very start, so any Calibre position is farther: offer it as a push.
            at_cover = not boox and portable.get('resume_issue_kind') == 'image-only-start'
            if not boox and not at_cover:
                keep[bid] = True
                issues.append(title + ': ' + (portable.get('resume_issue') or 'BOOX position cannot be compared safely; no position sent'))
                continue
            if not at_cover:
                mapper = EpubPositionMapper(path)
                order = mapper.compare_cfis(position['cfi'],boox['calibre_cfi'])
                if order < 0:
                    pull.append(bid)
                    continue
                if order == 0:
                    continue
            keep[bid] = True
            if not (current.get('boox_identity') or {}).get('metadata_uuid'):
                issues.append(title + ': BOOX copy identity is missing; Calibre position kept')
                continue
            if device is None:
                if device_factory:
                    device = device_factory()
                else:
                    from calibre_plugins.frankenboox.main import find_adb
                    adb = find_adb()
                    if not adb:
                        raise PushError('Connect BOOX and install Android Platform Tools')
                    device = AdbReadingDevice(adb)
            plan = preview(owner.db, bid, current, device, current_row=current.get('raw_metadata_row'))
            if plan['status'] == 'ready':
                plans.append({'book_id':bid, 'title':title, 'path':plan['book_path'], 'plan':plan})
            elif plan['status'] != 'unchanged':
                issues.append(title + ': ' + plan['reason'] + '; Calibre position kept')
        except Exception as error:
            keep[bid] = True
            issues.append(title + ': ' + str(error) + '; positions kept unchanged')
        finally:
            if mapper:
                mapper.close()
    return {'plans':plans, 'keep':keep, 'issues':issues, 'device':device, 'pull':pull}


def carry_verified_mappings(pending, reviewed):
    """Carry fresh page verification into independently prepared archive entries."""
    import copy
    try:
        from calibre_plugins.frankenboox.sync_state import state_signature
    except ImportError:
        from sync_state import state_signature
    by_id={bid:state for bid,state,files in reviewed}
    for bid,state,files in pending:
        verified=by_id.get(bid) or {}
        mapping=verified.get('fixed_format_mapping')
        if (mapping and state.get('boox_identity')==verified.get('boox_identity')
                and state_signature(state)==state_signature(verified)):
            state['fixed_format_mapping']=copy.deepcopy(mapping)
    return pending


def default_backup_directory():
    from calibre.utils.config import config_dir
    return Path(config_dir) / 'plugins' / 'neomatrix-position-backups'


def execute_pushes(db, device, rows, folder, status):
    """Only invoke after the sync review's explicit send-to-BOOX choice."""
    folder = Path(folder);folder.mkdir(parents=True, exist_ok=True)
    outcomes = []
    for row in rows:
        bid, plan, backup = row['book_id'], row['plan'], None
        try:
            status('Backing up and sending position: ' + row['title'])
            fmt=plan.get('format','EPUB')
            path = db.format_abspath(bid, fmt)
            if fmt in {'PDF','CBZ','CBR'} and path:
                try:
                    from calibre_plugins.frankenboox.fixed_sync import _book_from_context,calibre_position as fixed_position
                except ImportError:
                    from fixed_sync import _book_from_context,calibre_position as fixed_position
                book=_book_from_context(path,fmt,plan['fixed_format_mapping'])
                position=fixed_position(db,bid,path,fmt,book)
            else:
                position=calibre_position(db,bid,path) if path else None
            if not path or position != plan['calibre_position']:
                raise PushError('Calibre position changed during Sync; try Sync again')
            digest = hashlib.sha256()
            with open(path, 'rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):digest.update(chunk)
            if digest.hexdigest() != plan['calibre_sha256']:
                raise PushError('Calibre book changed during Sync; try Sync again')
            backup = folder / ('neomatrix-position-' + uuid4().hex + '.json')
            marker = {'status': 'pending', 'serial': device.serial, 'backup': str(backup),
                      'identity': {'metadata_uuid': plan['metadata_uuid'], 'document_id': plan['document_id'],
                                   'path': plan['book_path'], 'file_md5': plan['file_md5']},
                      'written_last_access_ms': plan['calibre_epoch_ms']}
            def remember():db.add_custom_book_data(PUSH_KEY, {bid: marker}, delete_first=False)
            apply_position(plan, device, backup, before_write=remember)
            marker['status'] = 'written'
            db.add_custom_book_data(PUSH_KEY, {bid: marker}, delete_first=False)
            plan.update(status='written', reason='Reopen this book on BOOX to refresh its percentage')
            outcomes.append({'book_id': bid, 'title': row['title'], 'status': 'written', 'backup': str(backup)})
        except Exception as error:
            plan.update(status='failed', reason=str(error))
            outcomes.append({'book_id': bid, 'title': row['title'], 'status': 'failed', 'reason': str(error),
                             'backup': str(backup) if backup and backup.exists() else None})
    return outcomes
