"""Explicit, backed-up cleanup of positively identified orphan NeoReader files.

Normal Sync never invokes this module. Shared databases, ebooks, Kindle data,
unknown identities and records whose storage cannot be checked are protected.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
import unicodedata
from typing import Optional
import zipfile

try:
    from calibre_plugins.frankenboox.sync_state import volume_identity
except ImportError:
    from sync_state import volume_identity

URI = 'content://com.onyx.content.database.ContentProvider/Metadata'
ROOTS = {
    '/sdcard/.ksync/point': ('Handwriting strokes', 'doc'),
    '/sdcard/.ksync/document': ('Handwriting sync files', 'doc'),
    '/sdcard/Android/data/com.onyx.kreader/files/page_cache': ('Reader page cache', 'md5'),
}
KINDLE_ROOT = '/sdcard/Android/data/com.amazon.kindle/files'
KEY_RE = re.compile(r', (?=[A-Za-z_][A-Za-z0-9_]*=)')
HEX = re.compile(r'^[0-9a-fA-F]{32}$')


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceFile:
    path: str
    size: int
    modified: int


@dataclass(frozen=True)
class Candidate:
    path: str
    title: str
    kind: str
    size: Optional[int]
    status: str
    eligible: bool = False


@dataclass
class Scan:
    serial: str
    candidates: list
    warnings: list
    metadata: list


def digest_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as src:
        for block in iter(lambda: src.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def valid_group(path):
    p = PurePosixPath(path)
    return str(p) == path and str(p.parent) in ROOTS and bool(HEX.fullmatch(p.name))


def valid_file(path, group):
    p = PurePosixPath(path)
    return (valid_group(group) and str(p) == path and '..' not in p.parts
            and path.startswith(group + '/') and '\x00' not in path)


def parse_metadata(text):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.strip() == 'No result found.':
            continue
        if not line.startswith('Row: '):
            raise CleanupError('BOOX book information could not be read reliably.')
        payload = line.split(' ', 2)[2]
        row = {}
        for part in KEY_RE.split(payload):
            key, sep, value = part.partition('=')
            if not sep:
                raise CleanupError('BOOX book information is incomplete.')
            key = key.strip()
            if key in row:
                raise CleanupError('Ambiguous BOOX book information. Cleanup is disabled.')
            row[key] = None if value.strip() == 'NULL' else value.strip()
        rows.append(row)
    if not rows:
        raise CleanupError('No BOOX book records were available. Cleanup is disabled.')
    return rows


def identities(row, kind):
    try:
        extra = json.loads(row.get('extraAttributes') or '{}')
        backend = extra.get('backend') or {}
        if isinstance(backend, str):
            backend = json.loads(backend)
        if not isinstance(backend, dict):
            backend = {}
    except (ValueError, AttributeError, TypeError):
        backend = {}
    values = [backend.get('doc_id')] if kind == 'doc' else [backend.get('md5'), row.get('hashTag')]
    return {str(v).lower() for v in values if v and HEX.fullmatch(str(v))}


@dataclass(frozen=True)
class BookCatalogue:
    files: list
    roots: tuple
    complete: bool = True
    primary: str = '/storage/emulated/0'


def label_key(text):
    text = unicodedata.normalize('NFKC', str(text or '')).casefold()
    return ' '.join(''.join(char if char.isalnum() else ' ' for char in text).split())


def source_paths(row):
    return sorted({str(row.get(key)) for key in ('nativeAbsolutePath', 'location', 'nocasePath')
                   if row.get(key) and str(row[key]).startswith('/')})


def volume_clash(row, filename):
    source = volume_identity(row.get('title'), row.get('name'), row.get('location'))
    other = volume_identity(filename)
    return bool(source and other and source != other)


def possible_copy(row, filename):
    """Conservative protection only; never identifies, merges or deletes books."""
    if volume_clash(row, filename):
        return False
    target = label_key(PurePosixPath(filename).stem)
    originals = source_paths(row)
    if row.get('name'):
        originals.append(str(row['name']))
    names = {label_key(PurePosixPath(path).stem) for path in originals}
    if target and target in names:
        return True
    title = label_key(row.get('title'))
    return bool(title and (' ' + title + ' ') in (' ' + target + ' '))


def same_book_record(row, other):
    title = label_key(row.get('title') or row.get('name'))
    if not title or title != label_key(other.get('title') or other.get('name')):
        return False
    authors, others = label_key(row.get('authors')), label_key(other.get('authors'))
    if authors and others and authors != others:
        return False
    a = volume_identity(row.get('title'), row.get('name'), row.get('location'))
    b = volume_identity(other.get('title'), other.get('name'), other.get('location'))
    return not (a and b and a != b)


def covered_path(path, catalogue):
    if path.startswith('/sdcard/'):
        path = catalogue.primary + path[len('/sdcard'):]
    return any(path.startswith(root + '/') for root in catalogue.roots)


def book_status(row, device, catalogue):
    paths = source_paths(row)
    if not paths:
        return 'U'
    direct = device.book_status(paths)
    if direct != 'M':
        return direct
    # Absence at a recorded path is not absence from the device. Renamed files
    # with identical bytes and potential replacement editions protect old notes.
    if any(possible_copy(row, f.path) for f in catalogue.files):
        return 'C'
    if not catalogue.complete or not all(covered_path(p, catalogue) for p in paths):
        return 'U'
    hashes = identities(row, 'md5')
    try:
        size = int(row.get('size') or 0)
    except (ValueError, TypeError):
        size = 0
    if not hashes or size <= 0:
        return 'U'
    for file in catalogue.files:
        if file.size == size:
            # Permission, disconnection or checksum failures must block cleanup.
            if device.ebook_md5(file.path) in hashes:
                return 'C'
    return 'M'


def scan_device(device, status=lambda text: None, include_kindle=True):
    device.ensure_connected()
    status('Checking book files on the BOOX…')
    rows = device.metadata()
    states = []
    warnings = []
    try:
        catalogue = device.book_catalogue(status)
    except Exception as error:
        catalogue = BookCatalogue([], (), False)
        warnings.append('Book locations could not all be checked; uncertain data is protected. ' + str(error))
    for row in rows:
        try:
            states.append(book_status(row, device, catalogue))
        except Exception:
            states.append('U')
    # A current replacement/copy in another NeoReader record also protects old
    # document data. Explicit different volumes are not treated as copies.
    for index, row in enumerate(rows):
        if states[index] == 'M' and any(states[j] in ('P', 'C') and same_book_record(row, other)
                                        for j, other in enumerate(rows) if j != index):
            states[index] = 'C'
    result = []
    for root, (label, identity_kind) in ROOTS.items():
        status('Checking ' + label.lower() + '…')
        try:
            groups = device.groups(root)
        except Exception as error:
            warnings.append(label + ': ' + str(error))
            continue
        for group in groups:
            if not valid_group(group):
                warnings.append('Unrecognized folder protected: ' + group)
                continue
            token = PurePosixPath(group).name.lower()
            matches = [i for i, row in enumerate(rows) if token in identities(row, identity_kind)]
            titles = sorted({rows[i].get('title') or rows[i].get('name') or 'Untitled book' for i in matches})
            title = ' / '.join(titles) if titles else 'Unidentified book data'
            try:
                files = device.files(group)
                size = sum(f.size for f in files)
                if not files:
                    continue
            except Exception as error:
                result.append(Candidate(group, title, label, None, 'Protected: unreadable or unsafe folder'))
                warnings.append(label + ': ' + str(error))
                continue
            eligible = False
            if size == 0:
                reason = 'Protected: empty data; nothing to reclaim'
            elif label == 'Reader page cache' and size < 1024:
                reason = 'Protected: tiny cache; leave in place'
            elif not matches:
                reason = 'Protected: book identity unknown'
            elif any(states[i] == 'P' for i in matches):
                reason = 'Protected: book still on device'
            elif any(states[i] == 'C' for i in matches):
                reason = 'Protected: book or possible copy found elsewhere'
            elif any(states[i] != 'M' for i in matches):
                reason = 'Protected: book storage could not be checked'
            else:
                reason = 'Old book files not found: backed-up removal available'
                eligible = True
            result.append(Candidate(group, title, label, size, reason, eligible))
    if include_kindle:
        status('Identifying Kindle downloads…')
        try:
            for group in device.groups(KINDLE_ROOT):
                if not group.startswith(KINDLE_ROOT + '/'):
                    raise CleanupError('Unexpected Kindle path.')
                files = device.files(group, protected=True)
                containers = [f for f in files if PurePosixPath(f.path).name.startswith('CR!')]
                if containers:
                    result.append(Candidate(group, 'Kindle download (' + PurePosixPath(group).name + ')',
                                            'Kindle book content', sum(f.size for f in containers),
                                            'Protected: manage downloads in the Kindle app'))
        except Exception:
            warnings.append('Kindle downloads could not all be inspected; they are always protected.')
    result.sort(key=lambda c: (not c.eligible, c.title.casefold(), c.kind, c.path))
    return Scan(device.serial, result, warnings, rows)


def verify_backup(path):
    """Fully validate scope and contents before any deletion or restoration."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if len(names) != len(set(names)) or 'manifest.json' not in names:
            raise CleanupError('Invalid cleanup backup.')
        info = zf.getinfo('manifest.json')
        if info.file_size > 8 * 1024 * 1024:
            raise CleanupError('Backup manifest is too large.')
        manifest = json.loads(zf.read('manifest.json'))
        if manifest.get('format') != 'neomatrix-storage-backup-v1' or not manifest.get('serial'):
            raise CleanupError('This is not a NeoMatrix storage backup.')
        groups = manifest.get('groups', [])
        if not groups or len(set(groups)) != len(groups) or not all(valid_group(p) for p in groups):
            raise CleanupError('Backup contains an unapproved device folder.')
        entries = manifest.get('files', [])
        paths = set()
        expected = {'manifest.json'}
        for index, entry in enumerate(entries):
            path = entry['path']
            group = entry['group']
            member = 'files/%06d' % index
            if (group not in groups or not valid_file(path, group) or path in paths
                    or entry.get('member') != member or not re.fullmatch(r'[a-f0-9]{64}', entry.get('sha256', ''))):
                raise CleanupError('Unsafe or duplicated backup file.')
            size = entry.get('size')
            if not isinstance(size, int) or size < 0 or zf.getinfo(member).file_size != size:
                raise CleanupError('Backup file size differs from its manifest.')
            h = hashlib.sha256()
            with zf.open(member) as src:
                for block in iter(lambda: src.read(1024 * 1024), b''):
                    h.update(block)
            if h.hexdigest() != entry['sha256']:
                raise CleanupError('Backup checksum verification failed.')
            paths.add(path)
            expected.add(member)
        if not entries or set(names) != expected or set(e['group'] for e in entries) != set(groups):
            raise CleanupError('Backup has incomplete or unexpected contents.')
        return manifest


def clean(device, selected, backup_path, status=lambda text: None):
    """Caller confirms closing/saving books; desktop backup precedes every unlink."""
    selected = list(selected)
    backup_path = Path(backup_path)
    if not selected or len(selected) != len(set(selected)) or not all(valid_group(p) for p in selected):
        raise CleanupError('Select only recognized deleted-book data.')
    if backup_path.exists():
        raise CleanupError('Choose a new backup filename; existing backups are never overwritten.')
    device.ensure_connected()
    device.pause_reader()  # Explicit cleanup only; never during scans or normal Sync.
    fresh = scan_device(device, status, include_kindle=False)
    eligible = {c.path for c in fresh.candidates if c.eligible}
    if not set(selected) <= eligible:
        raise CleanupError('A selected book is now present, unreadable or unidentified. Nothing was removed.')
    inventories = {p: device.files(p) for p in selected}
    total = sum(f.size for files in inventories.values() for f in files)
    if shutil.disk_usage(backup_path.parent).free < total + 16 * 1024 * 1024:
        raise CleanupError('The computer needs more free space for the backup.')
    manifest = {'format': 'neomatrix-storage-backup-v1', 'serial': device.serial,
                'created_at': datetime.now(timezone.utc).isoformat(), 'groups': selected,
                'metadata': fresh.metadata, 'files': [],
                'scope': 'External NeoReader files only. Device databases and ebook files are retained.'}
    # A unique partial backup cannot replace an existing verified archive.
    fd, partial = tempfile.mkstemp(prefix='.neomatrix-backup-', dir=backup_path.parent)
    os.close(fd)
    try:
        with tempfile.TemporaryDirectory(prefix='neomatrix-cleanup-') as tmp:
            with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for group in selected:
                    for f in inventories[group]:
                        status('Backing up deleted-book data…')
                        local = Path(tmp) / 'payload'
                        device.read_into(f.path, local)
                        if local.stat().st_size != f.size:
                            raise CleanupError('A device file changed during backup. Nothing was removed.')
                        checksum = digest_file(local)
                        member = 'files/%06d' % len(manifest['files'])
                        zf.write(local, member)
                        manifest['files'].append({'path': f.path, 'group': group, 'member': member,
                                                  'size': f.size, 'sha256': checksum})
                zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        verify_backup(partial)
        # Exclusive creation closes the filename race without overwriting a backup.
        with open(partial, 'rb') as src, open(backup_path, 'xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        verify_backup(backup_path)
    finally:
        if os.path.exists(partial):
            os.unlink(partial)
    removed = 0
    removed_bytes = 0
    try:
        device.ensure_connected()
        device.require_reader_stopped()
        fresh = scan_device(device, status, include_kindle=False)
        if not set(selected) <= {c.path for c in fresh.candidates if c.eligible}:
            raise CleanupError('A selected book changed status after backup.')
        entries = manifest['files']
        # Validate ALL groups and bytes before deleting ANY file.
        for group in selected:
            if device.files(group) != inventories[group]:
                raise CleanupError('A folder changed after backup.')
        for entry in entries:
            if device.checksum(entry['path']) != entry['sha256']:
                raise CleanupError('A file changed after backup.')
        for entry in entries:
            status('Removing verified, backed-up files…')
            device.ensure_connected()
            device.require_reader_stopped()
            device.remove_verified(entry['path'], entry['sha256'])
            removed += 1
            removed_bytes += entry['size']
    except Exception as error:
        raise CleanupError('%s\nRemoved %d file(s). The verified backup is saved at %s; use Restore backup if needed.'
                           % (error, removed, backup_path)) from error
    return {'removed_files': removed, 'removed_bytes': removed_bytes, 'backup': str(backup_path)}


def restore(device, backup_path, status=lambda text: None):
    manifest = verify_backup(backup_path)
    device.ensure_connected()
    if manifest['serial'] != device.serial:
        raise CleanupError('This backup belongs to a different BOOX. Restore is disabled.')
    device.pause_reader()
    # Preflight all conflicts before writing. Identical existing files are safe.
    pending = []
    for entry in manifest['files']:
        current = device.optional_checksum(entry['path'])
        if current is not None and current != entry['sha256']:
            raise CleanupError('A different file already exists. Nothing was restored: ' + entry['path'])
        if current is None:
            pending.append(entry)
    restored = 0
    try:
        with zipfile.ZipFile(backup_path) as zf, tempfile.TemporaryDirectory(prefix='neomatrix-restore-') as tmp:
            for entry in pending:
                status('Restoring backed-up files…')
                device.ensure_connected()
                device.require_reader_stopped()
                local = Path(tmp) / 'payload'
                with zf.open(entry['member']) as src, open(local, 'wb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                # Recheck after extraction: the backup could have changed since verification.
                if local.stat().st_size != entry['size'] or digest_file(local) != entry['sha256']:
                    raise CleanupError('Backup changed during restore.')
                device.restore_file(entry['path'], local, entry['sha256'])
                restored += 1
    except Exception as error:
        raise CleanupError('%s\nRestored %d file(s). Keep the backup and retry after resolving the error.'
                           % (error, restored)) from error
    return {'restored_files': restored, 'already_present': len(manifest['files']) - len(pending)}


class AdbDevice:
    """Serial-bound transport. Paths are quoted, validated and never deleted recursively."""
    def __init__(self, adb):
        self.adb = str(adb)
        self.serial = self._connected_serial()

    def _connected_serial(self):
        cp = subprocess.run([self.adb, 'devices'], capture_output=True, timeout=20, check=True)
        lines = cp.stdout.decode('utf-8', 'strict').splitlines()[1:]
        devices = [line.split() for line in lines if line.strip() and not line.startswith('*')]
        if len(devices) != 1 or len(devices[0]) != 2 or devices[0][1] != 'device':
            raise CleanupError('Connect and authorize exactly one BOOX for storage cleanup.')
        return devices[0][0]

    def ensure_connected(self):
        if self._connected_serial() != self.serial:
            raise CleanupError('The connected device changed. Cleanup stopped.')

    def run(self, args, timeout=90):
        cp = subprocess.run([self.adb, '-s', self.serial] + list(args), capture_output=True, timeout=timeout)
        if cp.returncode:
            raise CleanupError(cp.stderr.decode('utf-8', 'replace').strip() or 'BOOX command failed.')
        return cp.stdout

    def shell(self, command):
        return self.run(['shell', command]).decode('utf-8', 'strict').replace('\r', '')

    def metadata(self):
        return parse_metadata(self.shell('content query --uri ' + shlex.quote(URI)))

    def book_catalogue(self, status=lambda text: None):
        """Inventory every mounted user-storage volume; incomplete scans protect data."""
        primary = self.shell('toybox readlink -f /sdcard').strip()
        if not primary.startswith('/storage/'):
            raise CleanupError('Internal book storage could not be identified.')
        data = self.run(['shell', 'toybox find -L /storage -mindepth 1 -maxdepth 1 -type d -print0'])
        if data and not data.endswith(b'\x00'):
            raise CleanupError('Incomplete storage volume list.')
        roots = {primary}
        for raw in data.split(b'\x00'):
            if not raw:
                continue
            root = raw.decode('utf-8', 'strict')
            if str(PurePosixPath(root).parent) != '/storage':
                raise CleanupError('Unexpected storage volume.')
            if root not in ('/storage/emulated', '/storage/self'):
                roots.add(root)
        # A leftover directory at a removed SD-card mount is not a readable card.
        mounts = self.shell('cat /proc/mounts')
        mounted = set()
        for line in mounts.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                mounted.add(parts[1].replace('\\040', ' ').replace('\\011', '\t').replace('\\134', '\\'))
        for root in roots:
            if root != primary and root not in mounted:
                raise CleanupError('A storage card is unavailable. Cleanup is disabled.')
        if not any(primary == mount or primary.startswith(mount + '/')
                   for mount in mounted if mount.startswith('/storage/')):
            raise CleanupError('Internal book storage is unavailable. Cleanup is disabled.')
        files = []
        for root in sorted(roots):
            status('Checking book locations across internal memory and storage cards…')
            # Follow links for presence checks only. Loops, unreadable directories,
            # incomplete stat results and removals during inventory all fail closed.
            data = self.run(['shell', 'toybox find -L ' + shlex.quote(root) + ' -type f -print0'])
            if data and not data.endswith(b'\x00'):
                raise CleanupError('Incomplete storage file list.')
            paths = sorted({raw.decode('utf-8', 'strict') for raw in data.split(b'\x00') if raw})
            if any(not p.startswith(root + '/') for p in paths):
                raise CleanupError('Unexpected storage file path.')
            for start in range(0, len(paths), 64):
                batch = paths[start:start + 64]
                command = "toybox stat -L -c '%s' " + ' '.join(shlex.quote(p) for p in batch)
                output = self.shell(command).splitlines()
                if len(output) != len(batch) or any(not size.isdigit() for size in output):
                    raise CleanupError('Storage changed or could not be read completely.')
                files.extend(DeviceFile(path, int(size), 0) for path, size in zip(batch, output))
        self._ebook_md5_cache = {}
        return BookCatalogue(files, tuple(sorted(roots)), True, primary)

    def ebook_md5(self, path):
        # This separate read-only method deliberately cannot unlink ebook paths.
        if not path.startswith('/storage/'):
            raise CleanupError('Unexpected book location.')
        cache = getattr(self, '_ebook_md5_cache', {})
        if path not in cache:
            text = self.shell('toybox md5sum ' + shlex.quote(path))
            value = text[:32].lower()
            if not HEX.fullmatch(value):
                raise CleanupError('Could not check a possible renamed book.')
            cache[path] = value
            self._ebook_md5_cache = cache
        return cache[path]

    def book_status(self, paths):
        exists = ' || '.join('[ -f ' + shlex.quote(p) + ' ]' for p in paths)
        parents = sorted({str(PurePosixPath(p).parent) for p in paths})
        readable = ' && '.join('[ -d {0} ] && [ -r {0} ] && [ -x {0} ]'.format(shlex.quote(p)) for p in parents)
        result = self.shell('if ' + exists + '; then printf P; elif ' + readable + '; then printf M; else printf U; fi').strip()
        if result not in ('P', 'M', 'U'):
            raise CleanupError('Book storage could not be checked.')
        return result

    def groups(self, root):
        if root not in ROOTS and root != KINDLE_ROOT:
            raise CleanupError('Unapproved scan folder.')
        q = shlex.quote(root)
        data = self.run(['shell', 'if [ -d ' + q + ' ] && [ -r ' + q + ' ] && [ -x ' + q +
                         ' ]; then toybox find ' + q + ' -mindepth 1 -maxdepth 1 -type d -print0; else exit 1; fi'])
        return sorted(p.decode('utf-8', 'strict') for p in data.split(b'\x00') if p)

    def safe_path(self, path, create=False):
        group = next((str(PurePosixPath(path).parents[i]) for i in range(len(PurePosixPath(path).parents))
                      if valid_group(str(PurePosixPath(path).parents[i]))), None)
        if valid_group(path):
            group = path
        if not group or (path != group and not valid_file(path, group)):
            raise CleanupError('Unapproved file path.')
        root = str(PurePosixPath(group).parent)
        root_real = self.shell('toybox readlink -f ' + shlex.quote(root)).strip()
        sdcard_real = self.shell('toybox readlink -f /sdcard').strip()
        if not sdcard_real.startswith('/storage/') or root_real != sdcard_real + root[len('/sdcard'):]:
            raise CleanupError('Unexpected storage location or redirected folder.')
        # Check each existing ancestor; missing descendants are allowed only for restore.
        relative = PurePosixPath(path).relative_to(root)
        current = root
        for part in relative.parts:
            current += '/' + part
            q = shlex.quote(current)
            code = self.shell('if [ -L ' + q + ' ]; then printf L; elif [ -e ' + q +
                              ' ]; then printf E; else printf M; fi').strip()
            if code == 'L' or code not in ('E', 'M') or (code == 'M' and not create):
                raise CleanupError('Missing file or symbolic link protected: ' + current)
            if code == 'E':
                real = self.shell('toybox readlink -f ' + q).strip()
                if real != root_real + current[len(root):]:
                    raise CleanupError('File escapes its approved folder.')
        return group

    def files(self, group, protected=False):
        if protected:
            if (str(PurePosixPath(group).parent) != KINDLE_ROOT or str(PurePosixPath(group)) != group):
                raise CleanupError('Unexpected Kindle folder.')
        else:
            self.safe_path(group)
        q = shlex.quote(group)
        # Including directories and links lets us reject special entries instead of silently skipping them.
        data = self.run(['shell', 'toybox find ' + q + ' -mindepth 1 -print0'])
        files = []
        for raw in sorted(p for p in data.split(b'\x00') if p):
            path = raw.decode('utf-8', 'strict')
            if not path.startswith(group + '/') or '..' in PurePosixPath(path).parts:
                raise CleanupError('Unexpected file path.')
            if not protected and not valid_file(path, group):
                raise CleanupError('Unapproved file path.')
            stat = self.shell('toybox stat -c ' + shlex.quote('%f %s %Y') + ' ' + shlex.quote(path)).strip().split()
            if len(stat) != 3:
                raise CleanupError('Incomplete file information.')
            mode, size, modified = int(stat[0], 16), int(stat[1]), int(stat[2])
            if mode & 0o170000 == 0o040000:
                continue
            if mode & 0o170000 != 0o100000 or size < 0:
                raise CleanupError('A link or special file is protected.')
            files.append(DeviceFile(path, size, modified))
        return files

    def require_reader_stopped(self):
        text = self.shell('pidof com.onyx.kreader || true').strip()
        if text:
            raise CleanupError('NeoReader restarted. Save and close books before trying again.')

    def pause_reader(self):
        self.shell('am force-stop com.onyx.kreader')
        self.require_reader_stopped()

    def read_into(self, path, local):
        self.safe_path(path)
        with open(local, 'wb') as dst:
            cp = subprocess.run([self.adb, '-s', self.serial, 'exec-out', 'cat ' + shlex.quote(path)],
                                stdout=dst, stderr=subprocess.PIPE, timeout=180)
        if cp.returncode:
            raise CleanupError('Could not back up a device file: ' + cp.stderr.decode('utf-8', 'replace'))

    def checksum(self, path):
        self.safe_path(path)
        out = self.shell('toybox sha256sum ' + shlex.quote(path))
        value = out[:64]
        if not re.fullmatch(r'[a-f0-9]{64}', value):
            raise CleanupError('Could not verify a device file.')
        return value

    def optional_checksum(self, path):
        self.safe_path(path, create=True)
        q = shlex.quote(path)
        present = self.shell('if [ -e ' + q + ' ]; then printf E; else printf M; fi').strip()
        return self.checksum(path) if present == 'E' else None

    def remove_verified(self, path, checksum):
        self.safe_path(path)
        if not re.fullmatch(r'[a-f0-9]{64}', checksum):
            raise CleanupError('Invalid checksum.')
        q = shlex.quote(path)
        command = ('if [ -f ' + q + ' ] && [ ! -L ' + q + ' ] && '
                   '[ "$(toybox sha256sum ' + q + ' | toybox cut -c 1-64)" = ' + shlex.quote(checksum) +
                   ' ]; then toybox rm ' + q + '; else exit 1; fi')
        self.shell(command)

    def restore_file(self, path, local, checksum):
        self.safe_path(path, create=True)
        if self.optional_checksum(path) is not None:
            if self.checksum(path) == checksum:
                return
            raise CleanupError('A different file already exists: ' + path)
        parent = str(PurePosixPath(path).parent)
        self.shell('toybox mkdir -p ' + shlex.quote(parent))
        self.safe_path(parent)
        remote_temp = path + '.neomatrix-' + uuid.uuid4().hex
        self.run(['push', str(local), remote_temp], timeout=180)
        self.safe_path(remote_temp)
        if self.checksum(remote_temp) != checksum:
            raise CleanupError('Restored file verification failed; backup retained.')
        # mv -n never overwrites a concurrently recreated book-data file.
        self.shell('toybox mv -n ' + shlex.quote(remote_temp) + ' ' + shlex.quote(path))
        if self.checksum(path) != checksum:
            raise CleanupError('A file appeared during restore. Existing data was retained.')
