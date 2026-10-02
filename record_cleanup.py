"""Explicit, backed-up removal of selected stale BOOX library records.

Only Metadata rows are removed. Ebook files, annotations, bookmarks, handwriting
and shelf tables are never changed. Backups restore exact IDs on the same BOOX.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import uuid
try:
    from calibre_plugins.frankenboox.storage_cleanup import CleanupError, parse_metadata, source_paths, URI
    from calibre_plugins.frankenboox.boox_files import record_token
except ImportError:
    from storage_cleanup import CleanupError, parse_metadata, source_paths, URI
    from boox_files import record_token

FORMAT = 'neomatrix-book-record-backup-v1'
IDENTITY = re.compile(r'^[0-9a-fA-F]{32}$')
COLUMN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
MAX_SCRIPT = 4 * 1024 * 1024


def validate_row(row):
    if (not isinstance(row, dict) or not str(row.get('id') or '').isascii()
            or not str(row.get('id') or '').isdigit() or int(row['id']) <= 0
            or not IDENTITY.fullmatch(str(row.get('uuid') or ''))):
        raise CleanupError('This record has no reliable BOOX identity.')
    if any(not COLUMN.fullmatch(key) or (value is not None and not isinstance(value, str))
           or (isinstance(value, str) and '\x00' in value) for key, value in row.items()):
        raise CleanupError('This record cannot be backed up and restored safely.')
    return row


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def conditional_where(row):
    validate_row(row)
    return ' AND '.join('"%s" IS NULL' % key if value is None else
                        'CAST("%s" AS TEXT) COLLATE BINARY = %s' % (key, literal(value))
                        for key, value in sorted(row.items()))


def eligible_records(scan, tokens):
    requested = set(tokens)
    rows = [row for record, row in zip(scan.book_records, scan.metadata)
            if record.state != 'P' and record_token(scan.serial, row) in requested]
    if not rows or len(rows) != len(requested):
        raise CleanupError('Some selected records are current or changed. Scan again before removing them.')
    for row in rows:validate_row(row)
    if len({row['id'] for row in rows}) != len(rows) or len({row['uuid'] for row in rows}) != len(rows):
        raise CleanupError('Duplicate record identities are protected.')
    return rows


def write_backup(path, serial, rows):
    payload={'serial':serial,'records':rows}
    raw=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    backup={'format':FORMAT,'payload':payload,'sha256':hashlib.sha256(raw).hexdigest()}
    encoded=json.dumps(backup,ensure_ascii=False,indent=2).encode()
    fd=os.open(str(path),os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(encoded);stream.flush();os.fsync(stream.fileno())
    if Path(path).read_bytes()!=encoded:raise CleanupError('Backup could not be verified. Nothing removed.')
    return backup


def read_backup(path):
    backup=json.loads(Path(path).read_text(encoding='utf-8'))
    if backup.get('format')!=FORMAT:raise CleanupError('Choose a BOOX book-record backup.')
    payload=backup['payload']
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',', ':')).encode()
    if hashlib.sha256(raw).hexdigest()!=backup.get('sha256'):raise CleanupError('The backup has changed or is incomplete.')
    if not payload.get('serial') or not isinstance(payload.get('records'),list) or not payload['records']:
        raise CleanupError('The backup has no device or records.')
    for row in payload['records']:validate_row(row)
    if len({row['id'] for row in payload['records']})!=len(payload['records']) or len({row['uuid'] for row in payload['records']})!=len(payload['records']):
        raise CleanupError('Duplicate identities in backup.')
    return payload


def metadata(device):
    return device.record_metadata() if hasattr(device, 'record_metadata') else device.metadata()


def protect_current_book(device, row):
    paths = source_paths(row)
    if paths and device.book_status(paths) == 'P':
        raise CleanupError('A selected book is now at its saved location. Scan again; its record is protected.')


def matching_rows(rows, original):
    return [row for row in rows if row.get('id')==original['id'] or row.get('uuid')==original['uuid']]


def remove_records(device, scan, tokens, backup_path, status=lambda text:None):
    if device.serial!=scan.serial:raise CleanupError('The connected BOOX changed. Nothing removed.')
    rows=eligible_records(scan,tokens)
    device.ensure_connected();device.check_record_provider()
    current=metadata(device)
    for row in rows:
        if matching_rows(current,row)!=[row]:raise CleanupError('BOOX changed after the scan. Scan again; nothing removed.')
        protect_current_book(device,row)
    # A verified, exclusive backup precedes stopping the reader or any write.
    write_backup(backup_path,device.serial,rows)
    device.pause_reader()
    removed=0
    try:
        for number,row in enumerate(rows,1):
            status('Removing selected book records: %d of %d…' % (number,len(rows)))
            device.ensure_connected()
            if matching_rows(metadata(device),row)!=[row]:raise CleanupError('A selected record changed. Removal stopped.')
            protect_current_book(device,row)
            if device.delete_record(row)!=1:raise CleanupError('BOOX rejected a changed record. Removal stopped.')
            if matching_rows(metadata(device),row):raise CleanupError('Record removal could not be verified.')
            removed+=1
    except Exception as error:
        raise CleanupError('%s\n%d removal(s) confirmed. Check the last operation before retrying. Keep this recovery backup: %s' %
                           (error,removed,backup_path)) from error
    return {'removed_records':removed,'backup':str(backup_path)}


def restore_records(device, backup_path, status=lambda text:None):
    backup=read_backup(backup_path)
    if device.serial!=backup['serial']:raise CleanupError('This record backup belongs to another BOOX.')
    device.ensure_connected();device.check_record_provider()
    rows=backup['records'];current=metadata(device);pending=[]
    for row in rows:
        matches=matching_rows(current,row)
        if matches==[row]:continue
        if matches:raise CleanupError('BOOX has newer or conflicting records. Nothing restored.')
        pending.append(row)
    if pending:device.pause_reader()
    restored=0
    try:
        for number,row in enumerate(pending,1):
            status('Restoring book records: %d of %d…' % (number,len(pending)))
            device.ensure_connected()
            if matching_rows(metadata(device),row):raise CleanupError('A record appeared during restore. Nothing overwritten.')
            device.insert_record(row)
            if matching_rows(metadata(device),row)!=[row]:raise CleanupError('Restored record differs from the backup.')
            restored+=1
    except Exception as error:
        raise CleanupError('%s\n%d record(s) restored. Keep the backup and retry.' % (error,restored)) from error
    return {'restored_records':restored,'already_present':len(rows)-len(pending)}


def record_command(device, command):
    """Use a temporary quoted script so long JSON is never truncated by ADB argv."""
    data=(command+'\n').encode('utf-8')
    if len(data)>MAX_SCRIPT:raise CleanupError('This book record is too large for safe cleanup.')
    remote='/data/local/tmp/neomatrix-record-'+uuid.uuid4().hex+'.sh'
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local=Path(tmp)/'record.sh';local.write_bytes(data)
            device.run(['push',str(local),remote])
        output=device.run(['shell','sh '+shlex.quote(remote)]).decode('utf-8','strict').replace('\r','')
        if '[ERROR]' in output or 'Exception' in output or 'usage:' in output:
            raise CleanupError('BOOX rejected the record operation: '+output[:800])
        return output.strip()
    finally:
        device.run(['shell','rm -f '+shlex.quote(remote)])

# Verified local ContentProvider: direct Metadata DELETE; inserts abort on conflicts;
# annotation/bookmark schemas have no cascading foreign keys. Other firmware fails closed.
SUPPORTED_PROVIDER_SHA256 = {'539b26bdb3ffecd91a22a0dbe753af43c0e11f65f61d1de851c4516d65500b13'}
