import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import storage_cleanup as c

A = 'a' * 32
B = 'b' * 32
D = 'd' * 32
POINT = '/sdcard/.ksync/point/' + A
PAGE = '/sdcard/Android/data/com.onyx.kreader/files/page_cache/' + B
UNKNOWN = '/sdcard/.ksync/point/' + D
KINDLE = c.KINDLE_ROOT + '/B000EXAMPLE'


def row(doc=A, md5=B, title='Example: Volume 1', path='/storage/card/Books/gone.epub'):
    return {'title': title, 'location': path, 'size': '20000',
            'extraAttributes': json.dumps({'backend': {'doc_id': doc, 'md5': md5}})}


class FakeDevice:
    def __init__(self):
        self.serial = 'TEST-BOOX'
        self.rows = [row()]
        self.states = {'/storage/card/Books/gone.epub': 'M'}
        self.data = {POINT + "/page/shape's $(nothing)#points": b'original handwriting',
                     PAGE + '/journal': b'page cache' * 256}
        self.modified = 123
        self.directories = {POINT, PAGE}
        self.actions = []
        self.on_pause = None
        self.on_checksum = None
        self.fail_read = False
        self.fail_remove_at = None
        self.running = False
        self.connected = True
        self.catalogue_files = []
        self.catalogue_complete = True
        self.catalogue_error = False

    def ensure_connected(self):
        if not self.connected:
            raise c.CleanupError('Device disconnected')

    def book_catalogue(self, status=lambda text: None):
        if self.catalogue_error:
            raise OSError('SD card could not be read')
        return c.BookCatalogue(self.catalogue_files, ('/storage/card', '/storage/emulated/0'), self.catalogue_complete)

    def ebook_md5(self, path):
        return hashlib.md5(self.data[path]).hexdigest()

    def metadata(self):
        return copy.deepcopy(self.rows)

    def book_status(self, paths):
        states = [self.states.get(p, 'U') for p in paths]
        return 'P' if 'P' in states else ('M' if all(s == 'M' for s in states) else 'U')

    def groups(self, root):
        return sorted(p for p in self.directories if str(Path(p).parent) == root)

    def files(self, group, protected=False):
        return sorted((c.DeviceFile(p, len(data), self.modified) for p, data in self.data.items()
                       if p.startswith(group + '/')), key=lambda f: f.path)

    def pause_reader(self):
        self.actions.append('pause')
        self.running = False
        if self.on_pause:
            self.on_pause(self)

    def require_reader_stopped(self):
        if self.running:
            raise c.CleanupError('Reader restarted')

    def read_into(self, path, local):
        self.actions.append('read')
        if self.fail_read:
            raise OSError('Read failed')
        Path(local).write_bytes(self.data[path])

    def checksum(self, path):
        if self.on_checksum:
            self.on_checksum(self, path)
        return hashlib.sha256(self.data[path]).hexdigest()

    def optional_checksum(self, path):
        return self.checksum(path) if path in self.data else None

    def remove_verified(self, path, checksum):
        removed = sum(action.startswith('remove:') for action in self.actions)
        if self.fail_remove_at == removed:
            raise OSError('Deletion interrupted')
        if self.checksum(path) != checksum:
            raise c.CleanupError('File changed')
        self.actions.append('remove:' + path)
        del self.data[path]

    def restore_file(self, path, local, checksum):
        if path in self.data and self.checksum(path) != checksum:
            raise c.CleanupError('Existing file changed')
        self.data[path] = Path(local).read_bytes()
        self.actions.append('restore:' + path)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeDevice()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backup = Path(self.temp.name) / 'backup.zip'

    def cleanup(self, selected=None):
        return c.clean(self.device, selected or [POINT, PAGE], self.backup)

    def test_scan_is_read_only_and_recognizes_deleted_book(self):
        scan = c.scan_device(self.device)
        self.assertEqual(len(scan.candidates), 2)
        self.assertTrue(all(x.eligible for x in scan.candidates))
        self.assertEqual(self.device.actions, [])
        self.assertEqual(sum(x.size for x in scan.candidates), sum(map(len, self.device.data.values())))

    def test_active_book_protects_both_notes_and_cache(self):
        self.device.states['/storage/card/Books/gone.epub'] = 'P'
        self.assertFalse(any(x.eligible for x in c.scan_device(self.device).candidates))

    def test_unknown_storage_permissions_or_identity_protects_data(self):
        self.device.states.clear()
        self.device.data[UNKNOWN + '/stroke'] = b'unidentified'
        self.device.directories.add(UNKNOWN)
        self.assertFalse(any(x.eligible for x in c.scan_device(self.device).candidates))

    def test_same_identity_in_active_copy_protects_all_shared_data(self):
        self.device.rows.append(row(title='New copy', path='/storage/card/Books/current.epub'))
        self.device.states['/storage/card/Books/current.epub'] = 'P'
        self.assertFalse(any(x.eligible for x in c.scan_device(self.device).candidates))

    def test_same_title_different_volumes_do_not_share_identity(self):
        self.device.rows.append(row(doc=D, md5='e' * 32, title='Example: Volume 2', path='/storage/card/Books/current.epub'))
        self.device.states['/storage/card/Books/current.epub'] = 'P'
        self.device.directories.add(UNKNOWN)
        self.device.data[UNKNOWN + '/stroke'] = b'volume two'
        candidates = {x.path: x for x in c.scan_device(self.device).candidates}
        self.assertTrue(candidates[POINT].eligible)
        self.assertFalse(candidates[UNKNOWN].eligible)

    def test_kindle_cr_containers_protected_even_if_big(self):
        self.device.directories.add(KINDLE)
        self.device.data[KINDLE + '/CR!EXAMPLE.kfx'] = b'kindle content'
        kindle = [x for x in c.scan_device(self.device).candidates if x.kind == 'Kindle book content']
        self.assertEqual(len(kindle), 1)
        self.assertFalse(kindle[0].eligible)
        with self.assertRaises(c.CleanupError):
            self.cleanup([KINDLE])
        self.assertEqual(self.device.actions, [])

    def test_backup_remove_restore_roundtrip_is_byte_exact(self):
        original = copy.deepcopy(self.device.data)
        result = self.cleanup()
        self.assertEqual(result['removed_files'], 2)
        self.assertEqual(result['removed_bytes'], sum(map(len, original.values())))
        self.assertEqual(self.device.data, {})
        manifest = c.verify_backup(self.backup)
        self.assertEqual(manifest['metadata'], self.device.rows)
        restored = c.restore(self.device, self.backup)
        self.assertEqual(restored['restored_files'], 2)
        self.assertEqual(self.device.data, original)
        self.assertEqual(c.restore(self.device, self.backup)['already_present'], 2)

    def test_partial_cleanup_keeps_backup_and_restore_recovers_all(self):
        original = copy.deepcopy(self.device.data)
        self.device.fail_remove_at = 1
        with self.assertRaisesRegex(c.CleanupError, 'Removed 1 file'):
            self.cleanup()
        c.verify_backup(self.backup)
        c.restore(self.device, self.backup)
        self.assertEqual(self.device.data, original)

    def test_failed_backup_never_removes_files(self):
        original = copy.deepcopy(self.device.data)
        self.device.fail_read = True
        with self.assertRaises(OSError):
            self.cleanup()
        self.assertEqual(self.device.data, original)
        self.assertFalse(self.backup.exists())

    def test_book_reappears_after_scan_blocks_removal(self):
        c.scan_device(self.device)
        self.device.on_pause = lambda d: d.states.update({'/storage/card/Books/gone.epub': 'P'})
        with self.assertRaisesRegex(c.CleanupError, 'now present'):
            self.cleanup()
        self.assertFalse(any(a.startswith('remove:') for a in self.device.actions))

    def test_byte_change_after_backup_blocks_all_deletion(self):
        changed = [False]
        def change(d, path):
            if not changed[0]:
                changed[0] = True
                d.data[path] = b'new data, keep me'
        self.device.on_checksum = change
        with self.assertRaisesRegex(c.CleanupError, 'changed after backup'):
            self.cleanup()
        self.assertFalse(any(a.startswith('remove:') for a in self.device.actions))
        c.verify_backup(self.backup)

    def test_new_file_during_backup_blocks_all_removal(self):
        original = self.device.read_into
        def read(path, local):
            original(path, local)
            self.device.data[POINT + '/new-stroke'] = b'new notes'
        self.device.read_into = read
        with self.assertRaisesRegex(c.CleanupError, 'changed after backup'):
            self.cleanup()
        self.assertFalse(any(a.startswith('remove:') for a in self.device.actions))
        c.verify_backup(self.backup)

    def test_reader_restart_blocks_all_removal(self):
        original = self.device.read_into
        def read(path, local):
            original(path, local)
            self.device.running = True
        self.device.read_into = read
        with self.assertRaisesRegex(c.CleanupError, 'Reader restarted'):
            self.cleanup()
        self.assertFalse(any(a.startswith('remove:') for a in self.device.actions))
        c.verify_backup(self.backup)

    def test_disconnection_after_backup_preserves_files_and_backup(self):
        original = self.device.read_into
        def read(path, local):
            original(path, local)
            self.device.connected = False
        self.device.read_into = read
        with self.assertRaisesRegex(c.CleanupError, 'disconnected'):
            self.cleanup()
        self.assertFalse(any(a.startswith('remove:') for a in self.device.actions))
        c.verify_backup(self.backup)

    def test_symlinked_root_or_file_is_rejected_by_transport(self):
        device = object.__new__(c.AdbDevice)
        def shell(command):
            if command == 'toybox readlink -f /sdcard':
                return '/storage/emulated/0'
            if command.startswith('toybox readlink -f'):
                return '/storage/emulated/0/Books'
            return 'E'
        device.shell = shell
        with self.assertRaisesRegex(c.CleanupError, 'redirected folder'):
            device.safe_path(POINT)
        def linked_file(command):
            if command.startswith('toybox readlink -f'):
                path = command.split('toybox readlink -f ', 1)[1].strip("'")
                return '/storage/emulated/0' + path[len('/sdcard'):]
            return 'L'
        device.shell = linked_file
        with self.assertRaisesRegex(c.CleanupError, 'symbolic link'):
            device.safe_path(POINT)

    def test_duplicate_provider_keys_disables_cleanup(self):
        with self.assertRaisesRegex(c.CleanupError, 'Ambiguous'):
            c.parse_metadata('Row: 0 location=/storage/a, location=/storage/b')

    def test_book_moved_from_internal_to_card_is_protected(self):
        self.device.rows[0]['location'] = '/storage/emulated/0/Books/gone.epub'
        self.device.states['/storage/emulated/0/Books/gone.epub'] = 'M'
        path = '/storage/card/Books/gone.epub'
        self.device.catalogue_files = [c.DeviceFile(path, 20000, 0)]
        scan = c.scan_device(self.device)
        self.assertFalse(any(x.eligible for x in scan.candidates))
        self.assertTrue(all('elsewhere' in x.status for x in scan.candidates))
        with self.assertRaises(c.CleanupError):
            self.cleanup()
        self.assertFalse(any(a.startswith('remove:') for a in self.device.actions))

    def test_renamed_ebook_with_same_md5_is_protected(self):
        payload = b'ebook contents at a renamed location'
        path = '/storage/card/OtherFolder/completely-renamed.epub'
        self.device.rows[0]['size'] = str(len(payload))
        self.device.rows[0]['extraAttributes'] = json.dumps({'backend': {'doc_id': A, 'md5': hashlib.md5(payload).hexdigest()}})
        self.device.catalogue_files = [c.DeviceFile(path, len(payload), 0)]
        self.device.data[path] = payload
        self.assertFalse(any(x.eligible for x in c.scan_device(self.device).candidates))

    def test_replacement_filename_protects_even_if_ebook_bytes_differ(self):
        path = '/storage/card/Books/Example - Volume 1 - Author.epub'
        self.device.catalogue_files = [c.DeviceFile(path, 98765, 0)]
        self.assertFalse(any(x.eligible for x in c.scan_device(self.device).candidates))

    def test_current_record_with_different_docid_and_hash_protects_old_copy(self):
        self.device.rows.append(row(doc=D, md5='e' * 32, path='/storage/card/Books/new-version.epub'))
        self.device.states['/storage/card/Books/new-version.epub'] = 'P'
        self.assertFalse(any(x.eligible for x in c.scan_device(self.device).candidates))

    def test_explicit_volume_two_does_not_identify_volume_one_as_present(self):
        source = self.device.rows[0]
        source['title'] = 'Example encyclopedia'
        source['name'] = 'Example encyclopedia Volume 1.epub'
        other = row(doc=D, md5='e' * 32, title='Example encyclopedia', path='/storage/card/Books/Example encyclopedia Volume 2.epub')
        other['name'] = 'Example encyclopedia Volume 2.epub'
        self.device.rows.append(other)
        self.device.states[other['location']] = 'P'
        self.device.catalogue_files = [c.DeviceFile(other['location'], 98765, 0)]
        candidates = {x.path: x for x in c.scan_device(self.device).candidates}
        self.assertTrue(candidates[POINT].eligible)
        self.assertFalse(c.possible_copy(source, other['location']))
        self.assertFalse(c.same_book_record(source, other))

    def test_incomplete_storage_inventory_blocks_cleanup(self):
        self.device.catalogue_complete = False
        self.assertFalse(any(x.eligible for x in c.scan_device(self.device).candidates))
        with self.assertRaises(c.CleanupError):
            self.cleanup()
        self.assertFalse(any(a.startswith('remove:') for a in self.device.actions))

    def test_catalogue_permission_failure_protects_missing_paths(self):
        self.device.catalogue_error = True
        scan = c.scan_device(self.device)
        self.assertTrue(scan.warnings)
        self.assertFalse(any(x.eligible for x in scan.candidates))

    def test_unchecked_storage_card_protects_its_old_book_data(self):
        self.device.rows[0]['location'] = '/storage/ABSENT-CARD/Books/gone.epub'
        self.device.states['/storage/ABSENT-CARD/Books/gone.epub'] = 'M'
        self.assertFalse(any(x.eligible for x in c.scan_device(self.device).candidates))

    def test_checksum_failure_for_possible_renamed_copy_protects_book(self):
        self.device.catalogue_files = [c.DeviceFile('/storage/card/other.epub', 20000, 0)]
        def failure(path):
            raise OSError('Unreadable file')
        self.device.ebook_md5 = failure
        self.assertFalse(any(x.eligible for x in c.scan_device(self.device).candidates))

    def test_missing_book_identity_or_size_cannot_offer_removal(self):
        self.device.rows[0]['size'] = '0'
        self.assertFalse(any(x.eligible for x in c.scan_device(self.device).candidates))

    def test_tiny_cache_and_empty_strokes_are_protected(self):
        self.device.data[PAGE + '/journal'] = b'x' * 31
        point = next(p for p in self.device.data if p.startswith(POINT + '/'))
        self.device.data[point] = b''
        scan = c.scan_device(self.device)
        self.assertFalse(any(x.eligible for x in scan.candidates))
        self.assertEqual({x.size for x in scan.candidates}, {0, 31})

    def test_unreadable_folder_size_is_unknown_not_zero(self):
        def failure(group, protected=False):
            raise OSError('Unreadable folder')
        self.device.files = failure
        scan = c.scan_device(self.device)
        self.assertTrue(all(x.size is None and not x.eligible for x in scan.candidates))

    def test_unmounted_card_directory_rejected_by_catalogue_transport(self):
        device = object.__new__(c.AdbDevice)
        def shell(command):
            return '/storage/emulated/0' if 'readlink' in command else '/dev/fuse /storage/emulated fuse rw 0 0'
        device.shell = shell
        device.run = lambda args: b'/storage/emulated\x00/storage/ABSENT-CARD\x00'
        with self.assertRaisesRegex(c.CleanupError, 'card is unavailable'):
            device.book_catalogue()

    def test_existing_backup_never_overwritten(self):
        self.backup.write_bytes(b'precious backup')
        with self.assertRaisesRegex(c.CleanupError, 'never overwritten'):
            self.cleanup()
        self.assertEqual(self.backup.read_bytes(), b'precious backup')
        self.assertEqual(self.device.actions, [])

    def test_wrong_device_cannot_restore(self):
        self.cleanup()
        self.device.serial = 'DIFFERENT'
        with self.assertRaisesRegex(c.CleanupError, 'different BOOX'):
            c.restore(self.device, self.backup)
        self.assertEqual(self.device.data, {})

    def test_restore_preflights_all_conflicts(self):
        self.cleanup()
        path = PAGE + '/journal'
        self.device.data[path] = b'new book cache'
        with self.assertRaisesRegex(c.CleanupError, 'different file'):
            c.restore(self.device, self.backup)
        self.assertEqual(self.device.data, {path: b'new book cache'})

    def test_malicious_manifest_cannot_restore_kindle_or_traversal(self):
        self.cleanup()
        with zipfile.ZipFile(self.backup) as zf:
            contents = {n: zf.read(n) for n in zf.namelist()}
        for malicious in (KINDLE, POINT + '/../' + D, '/sdcard/Books/' + A):
            manifest = json.loads(contents['manifest.json'])
            manifest['groups'][0] = malicious
            with zipfile.ZipFile(self.backup, 'w') as zf:
                for name, data in contents.items():
                    zf.writestr(name, json.dumps(manifest) if name == 'manifest.json' else data)
            with self.assertRaises(c.CleanupError):
                c.restore(self.device, self.backup)
            self.assertEqual(self.device.data, {})

    def test_corrupted_backup_prevents_restore(self):
        self.cleanup()
        with zipfile.ZipFile(self.backup) as zf:
            contents = {n: zf.read(n) for n in zf.namelist()}
        contents['files/000000'] = b'corruption'
        with zipfile.ZipFile(self.backup, 'w') as zf:
            for name, data in contents.items():
                zf.writestr(name, data)
        with self.assertRaises(c.CleanupError):
            c.restore(self.device, self.backup)
        self.assertEqual(self.device.data, {})

    def test_failed_folder_check_protects_that_folder(self):
        original = self.device.files
        def files(group, protected=False):
            if group == POINT:
                raise c.CleanupError('symlink detected')
            return original(group, protected)
        self.device.files = files
        candidates = {x.path: x for x in c.scan_device(self.device).candidates}
        self.assertFalse(candidates[POINT].eligible)
        with self.assertRaises(c.CleanupError):
            self.cleanup()
        self.assertFalse(any(a.startswith('remove:') for a in self.device.actions))

    def test_incomplete_provider_output_disables_cleanup(self):
        for text in ('', 'No result found.', 'Error while accessing provider', 'Row: 0 missingEquals'):
            with self.assertRaises(c.CleanupError):
                c.parse_metadata(text)

    def test_multiple_or_unauthorized_devices_rejected(self):
        class Result:
            stdout = b'List of devices attached\na\tdevice\nb\tdevice\n'
        with patch.object(c.subprocess, 'run', return_value=Result()):
            with self.assertRaises(c.CleanupError):
                c.AdbDevice('adb')
        Result.stdout = b'List of devices attached\na\tunauthorized\n'
        with patch.object(c.subprocess, 'run', return_value=Result()):
            with self.assertRaises(c.CleanupError):
                c.AdbDevice('adb')


if __name__ == '__main__':
    unittest.main()
