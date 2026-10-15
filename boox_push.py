"""Backed-up, conditional updates of one observed NeoReader resume position."""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
try:
    from calibre_plugins.frankenboox.epub_identity import identity_from_bytes
except ImportError:
    from epub_identity import identity_from_bytes

try:
    from calibre_plugins.frankenboox.reader_push import _backend, _resume, position_object
    from calibre_plugins.frankenboox.push_values import timestamp_ms, md5_value
except ImportError:
    from reader_push import _backend, _resume, position_object
    from push_values import timestamp_ms, md5_value

URI = 'content://com.onyx.content.database.ContentProvider/Metadata'
FIELDS = ('uuid', 'location', 'extraAttributes', 'lastAccess', 'type', 'hashTag')


class PushError(RuntimeError):
    pass


def _values(plan):
    if plan.get('status') != 'ready':
        raise PushError('This position was not verified for push')
    raw, attrs, backend = _backend({'extraAttributes': plan['expected_extra_attributes']})
    kind=plan.get('position_kind')
    fixed=kind in ('pdf-page','comic-page')
    def decode(value):
        if not fixed:return position_object(value)
        if kind=='pdf-page':
            value=json.loads(value) if isinstance(value,str) else value
            if not isinstance(value,dict) or set(value)!={'pageReferenceId'} or not re.fullmatch(r'pdf-[1-9]\d*-\d+',str(value['pageReferenceId'])):
                raise PushError('Invalid verified PDF page reference')
            return value
        if isinstance(value,str) and re.fullmatch(r'\d+',value):value=int(value)
        if type(value) is not int or not 0<=value<plan.get('page_count',0):
            raise PushError('Invalid verified comic page index')
        return value
    observed = decode(plan['position_raw'])
    if observed != plan['position']:
        raise PushError('Observed position changed after preview')
    if fixed:
        try:
            from calibre_plugins.frankenboox.fixed_sync import resume_value
        except ImportError:
            from fixed_sync import resume_value
        old=decode(resume_value(backend))
        if not 0<=plan.get('page_index',-1)<plan.get('page_count',0):raise PushError('Verified page is out of bounds')
    else:
        old = _resume(backend)
    changed = []
    for key in ('current_page_position_v3', 'current_page_position_v2', 'current_page'):
        if key in backend and backend[key] is not None and backend[key]!='':
            if decode(backend[key]) != old:
                raise PushError('Reader engine positions disagree')
            backend[key] = plan['position_raw'] if isinstance(backend[key], str) else observed
            changed.append(key)
    if not changed:
        raise PushError('No observed engine resume field to update')
    when = timestamp_ms(plan['calibre_epoch_ms'])
    if when <= timestamp_ms(plan['expected_last_access']):
        raise PushError('Calibre position is not newer')
    if 'user_doc_data_update_time' in backend:
        backend['user_doc_data_update_time'] = when
    attrs['backend'] = json.dumps(backend, ensure_ascii=False, separators=(',', ':')) if isinstance(attrs['backend'], str) else backend
    return {'extraAttributes': json.dumps(attrs, ensure_ascii=False, separators=(',', ':')), 'lastAccess': str(when)}


def _check(plan, row, device):
    if plan.get('serial') != device.serial:
        raise PushError('Connected BOOX differs from the preview')
    if row.get('uuid') != plan.get('metadata_uuid') or row.get('location') != plan.get('book_path'):
        raise PushError('Book copy changed after preview')
    for key, expected in (('extraAttributes', plan.get('expected_extra_attributes')), ('lastAccess', plan.get('expected_last_access'))):
        if row.get(key) != expected:
            raise PushError('BOOX changed after preview; nothing overwritten')
    _, _, backend = _backend(row)
    if backend.get('doc_id') != plan.get('document_id') or md5_value(backend.get('md5') or row.get('hashTag')) != plan.get('file_md5'):
        raise PushError('Book identity changed after preview')
    if device.book_sha256(plan['book_path']) != plan['file_sha256']:
        raise PushError('Book content changed after preview')


def apply_position(plan, device, backup_path, before_write=None):
    """Caller must obtain the user's explicit choice before invoking this method."""
    values = _values(plan)
    row = device.query(plan['metadata_uuid'])
    _check(plan, row, device)
    backup = {'format': 'neomatrix-position-backup-v1', 'serial': device.serial,
              'metadata_uuid': plan['metadata_uuid'], 'book_path': plan['book_path'],
              'file_md5': plan['file_md5'], 'file_sha256': plan['file_sha256'],
              'original_row': row, 'written_values': values}
    payload = json.dumps(backup, ensure_ascii=False, indent=2).encode('utf-8')
    # Exclusive creation: existing backups are never overwritten.
    fd = os.open(str(backup_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    if Path(backup_path).read_bytes() != payload:
        raise PushError('Backup verification failed; no device update attempted')
    device.stop_reader()
    row = device.query(plan['metadata_uuid'])
    _check(plan, row, device)
    if before_write is not None:
        before_write()
    count = device.update(plan['metadata_uuid'], row, values)
    if count != 1:
        raise PushError('BOOX rejected the conditional update; backup was retained')
    actual = device.query(plan['metadata_uuid'])
    if any(actual.get(key) != value for key, value in values.items()):
        raise PushError('BOOX update could not be verified; backup was retained')
    if any(actual.get(key) != row.get(key) for key in FIELDS if key not in values):
        raise PushError('Unexpected device metadata change; backup was retained')
    return {'status': 'written', 'backup': str(backup_path), 'row': actual,
            'note': 'Reopen the book in NeoReader to load the position and recalculate its page progress'}


def restore_position(device, backup_path):
    backup = json.loads(Path(backup_path).read_text(encoding='utf-8'))
    if backup.get('format') != 'neomatrix-position-backup-v1' or backup.get('serial') != device.serial:
        raise PushError('This backup belongs to a different BOOX or format')
    uuid = backup['metadata_uuid']
    original = backup['original_row']
    current = device.query(uuid)
    try:
        original_backend = _backend(original)[2]
        current_backend = _backend(current)[2]
        same = (original.get('uuid') == uuid == current.get('uuid') and
                original.get('location') == backup['book_path'] == current.get('location') and
                original_backend.get('doc_id') and
                original_backend['doc_id'] == current_backend.get('doc_id') and
                md5_value(original_backend.get('md5') or original.get('hashTag')) == backup['file_md5'] and
                md5_value(current_backend.get('md5') or current.get('hashTag')) == backup['file_md5'])
    except (ValueError, TypeError, KeyError):
        same = False
    if not same or device.book_sha256(backup['book_path']) != backup['file_sha256']:
        raise PushError('Book changed; restore was stopped')
    if set(backup['written_values']) != {'extraAttributes', 'lastAccess'}:
        raise PushError('Unexpected restore fields')
    values = {key: original[key] for key in ('extraAttributes', 'lastAccess')}
    if all(current.get(key) == value for key, value in values.items()):
        return {'status': 'unchanged'}
    if any(current.get(key) != value for key, value in backup['written_values'].items()):
        raise PushError('BOOX was changed since the push; newer device data retained')
    device.stop_reader()
    if device.query(uuid) != current:
        raise PushError('BOOX changed during restore; nothing overwritten')
    if device.book_sha256(backup['book_path']) != backup['file_sha256']:
        raise PushError('Book changed during restore; nothing overwritten')
    if device.update(uuid, current, values) != 1 or any(device.query(uuid).get(k) != v for k, v in values.items()):
        raise PushError('Restore could not be verified; backup was retained')
    return {'status': 'restored'}


class AdbReadingDevice:
    """Use the exported provider; no rooting, database replacement or firmware code."""
    def __init__(self, adb):
        self.adb = adb
        output = self._run('devices')
        devices = [line.split()[0] for line in output.splitlines()[1:] if line.strip() and line.split()[1] == 'device']
        if len(devices) != 1:
            raise PushError('Connect and authorize exactly one BOOX')
        self.serial = devices[0]
        sdk = self._shell('getprop', 'ro.build.version.sdk').strip()
        # Older Android content tools parse string binds differently. Never send
        # escaped JSON through an unverified legacy parser.
        if not sdk.isascii() or not sdk.isdigit() or int(sdk) < 31:
            raise PushError('Position push requires Android 12 or newer; BOOX imports still work')

    def _run(self, *args):
        cp = subprocess.run([self.adb, *args], capture_output=True, timeout=60)
        text = cp.stdout.decode('utf-8', 'strict').replace('\r', '')
        error = cp.stderr.decode('utf-8', 'replace')
        if cp.returncode or error.strip() or '[ERROR]' in text or text.startswith('usage:'):
            raise PushError(error.strip() or text.strip() or 'ADB request failed')
        return text

    def _shell(self, *args):
        return self._run('-s', self.serial, 'shell', ' '.join(shlex.quote(str(x)) for x in args))

    def query(self, uuid):
        if not isinstance(uuid, str) or not uuid or '\x00' in uuid:
            raise PushError('Invalid book identity')
        where = 'uuid=' + self._literal(uuid)
        row = {}
        for key in FIELDS:
            lines = self._shell('content', 'query', '--uri', URI, '--projection', key, '--where', where).splitlines()
            prefix = 'Row: 0 ' + key + '='
            if len(lines) != 1 or not lines[0].startswith(prefix):
                raise PushError('Book identity is missing, duplicated or unreadable')
            value = lines[0][len(prefix):]
            row[key] = None if value == 'NULL' else value
        return row

    @staticmethod
    def _literal(value):
        if value is None:
            raise PushError('Cannot use an unknown value in the update guard')
        return "'" + str(value).replace("'", "''") + "'"

    def book_sha256(self, path):
        if not isinstance(path, str) or not path.startswith(('/storage/', '/sdcard/')) or '\x00' in path:
            raise PushError('Unknown device book path')
        text = self._shell('sha256sum', path).strip()
        digest = text.split()[0] if text else ''
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise PushError('Could not verify the device ebook checksum')
        return digest

    def stop_reader(self):
        self._shell('am', 'force-stop', 'com.onyx.kreader')

    def book_identity(self, path):
        if not isinstance(path, str) or not path.startswith(('/storage/', '/sdcard/')) or '\x00' in path:
            raise PushError('Unknown device book path')
        command = 'cat ' + shlex.quote(path)
        cp = subprocess.run([self.adb, '-s', self.serial, 'exec-out', command], capture_output=True, timeout=120)
        if cp.returncode or cp.stderr.strip():
            raise PushError(cp.stderr.decode('utf-8', 'replace').strip() or 'Could not read the BOOX EPUB')
        try:
            return identity_from_bytes(cp.stdout)
        except Exception as error:
            raise PushError('Could not verify BOOX EPUB reading content: ' + str(error)) from error

    def copy_book_to(self,path,destination):
        if not isinstance(path,str) or not path.startswith(('/storage/','/sdcard/')) or '\x00' in path:
            raise PushError('Unknown BOOX book path')
        with open(destination,'xb') as stream:
            cp=subprocess.run([self.adb,'-s',self.serial,'exec-out','cat '+shlex.quote(path)],
                              stdout=stream,stderr=subprocess.PIPE,timeout=180)
        if cp.returncode or cp.stderr.strip():
            raise PushError(cp.stderr.decode('utf-8','replace').strip() or 'Could not read BOOX book')

    def reader_provider_sha256(self):
        paths=self._shell('pm','path','com.onyx.kreader').splitlines()
        if len(paths)!=1 or not paths[0].startswith('package:'):
            raise PushError('NeoReader package identity is ambiguous')
        path=paths[0][len('package:'):]
        digest=self._shell('sha256sum',path).split()[0]
        if not re.fullmatch(r'[0-9a-f]{64}',digest):raise PushError('NeoReader package could not be verified')
        return digest

    def update(self, uuid, expected, values):
        if set(values) != {'extraAttributes', 'lastAccess'} or expected.get('uuid') != uuid:
            raise PushError('Unexpected push fields or identity')
        where = ' AND '.join(key + '=' + self._literal(expected[key]) for key in ('uuid', 'location', 'extraAttributes', 'lastAccess'))
        args = ['content', 'update', '--uri', URI, '--where', where]
        for key, value in values.items():
            # Android 13's content CLI splits binds with backslash escaping.
            # Shell quoting alone cannot protect JSON colons/backslashes here.
            escaped = value.replace('\\', '\\\\').replace(':', '\\:')
            args += ['--bind', key + ':s:' + escaped]
        output = self._shell(*args)
        # Android's content CLI can return success without printing the count.
        # A silent response alone is never evidence of a successful write.
        match = re.fullmatch(r'Updated (\d+) rows\.\s*', output)
        if match:
            return int(match.group(1))
        if output.strip():
            raise PushError('Unexpected BOOX update response')
        actual = self.query(uuid)
        return 1 if all(actual.get(key) == value for key, value in values.items()) else 0
