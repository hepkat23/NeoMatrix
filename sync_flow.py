"""Review newer Calibre positions during normal Sync; push only after approval."""
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
    """Read both sides. Retain newer local positions even when push is unsupported."""
    plans, keep, issues, device = [], {}, [], None
    anchors = owner.db.get_custom_book_data(ANCHOR_KEY)
    for bid, current, files in entries:
        path = owner.db.format_abspath(bid, 'EPUB')
        if not path:continue
        title = owner.db.field_for('title', bid) or str(bid)
        try:
            position = calibre_position(owner.db, bid, path)
        except ValueError:
            # No saved position on this side: there is nothing to push.
            continue
        except Exception:
            keep[bid] = True
            issues.append(title + ': could not check the saved Calibre position; kept it unchanged')
            continue
        try:boox_time = int((current.get('progress') or {}).get('last_access_ms') or 0)
        except (ValueError, TypeError):boox_time = 0
        archived, _ = owner._archived_state_for_book(bid)
        portable = anchors.get(bid) or {}
        if archived:
            try:
                baseline = int((archived.get('progress') or {}).get('last_access_ms') or 0)
                local_changed = (position['epoch_ms'] > baseline and
                                 position['cfi'] != (portable.get('resume') or {}).get('calibre_cfi'))
                device_changed = (boox_time != baseline or
                                  _resume(_backend(archived['raw_metadata_row'])[2]) != _resume(_backend(current['raw_metadata_row'])[2]))
                if local_changed and device_changed:
                    keep[bid] = True
                    issues.append(title + ': both reading positions changed since the last pull; Calibre position kept and no position sent')
                    continue
            except (ValueError, TypeError, KeyError):
                pass
        if position['epoch_ms'] <= boox_time:continue
        keep[bid] = True
        if not archived:
            issues.append(title + ': first save BOOX data with Sync; newer Calibre position kept')
            continue
        if not (archived.get('boox_identity') or {}).get('metadata_uuid'):
            issues.append(title + ': saved BOOX copy identity is missing; Calibre position kept')
            continue
        try:
            # A newer timestamp alone does not require a device write.
            if ((portable.get('resume') or {}).get('calibre_cfi') == position['cfi'] and
                    archived.get('boox_identity') == current.get('boox_identity') and
                    _resume(_backend(archived['raw_metadata_row'])[2]) == _resume(_backend(current['raw_metadata_row'])[2])):
                continue
            status('Checking newer Calibre position: ' + title)
            if device is None:
                if device_factory:device = device_factory()
                else:
                    from calibre_plugins.frankenboox.main import find_adb
                    adb = find_adb()
                    if not adb:raise PushError('Connect BOOX and install Android Platform Tools')
                    device = AdbReadingDevice(adb)
            plan = preview(owner.db, bid, archived, device, current_row=current.get('raw_metadata_row'))
            if plan['status'] == 'ready':
                plans.append({'book_id': bid, 'title': title, 'path': plan['book_path'], 'plan': plan})
            elif plan['status'] != 'unchanged':issues.append(title + ': ' + plan['reason'] + '; Calibre position kept')
        except Exception as error:issues.append(title + ': ' + str(error) + '; Calibre position kept')
    return {'plans': plans, 'keep': keep, 'issues': issues, 'device': device}


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
            path = db.format_abspath(bid, 'EPUB')
            if not path or calibre_position(db, bid, path) != plan['calibre_position']:
                raise PushError('Calibre position changed during Sync; try Sync again')
            digest = hashlib.sha256()
            with open(path, 'rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):digest.update(chunk)
            if digest.hexdigest() != plan['calibre_sha256']:
                raise PushError('Calibre EPUB changed during Sync; try Sync again')
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
