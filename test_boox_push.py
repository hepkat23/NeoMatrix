import copy
import json
import tempfile
import unittest
from pathlib import Path
from boox_push import apply_position, restore_position, PushError, _values, AdbReadingDevice
import test_reader_push as fixtures
MD5 = fixtures.MD5


class Device:
    serial = 'synthetic-device'
    def __init__(self, row):
        self.row = copy.deepcopy(row)
        self.stops = self.writes = 0
        self.hash = fixtures.Book.digest
        self.change_on_stop = False
        self.race = False
        self.corrupt = False
    def query(self, uuid):
        if uuid != self.row['uuid']:
            raise PushError('Unknown identity')
        return copy.deepcopy(self.row)
    def book_sha256(self, path):
        return self.hash
    def stop_reader(self):
        self.stops += 1
        if self.change_on_stop:
            self.row['lastAccess'] = '4000'
    def update(self, uuid, expected, values):
        if self.race:
            self.row['lastAccess'] = '5000'
        if self.row != expected:
            return 0
        self.writes += 1
        if not self.corrupt:
            self.row.update(values)
        return 1


class GuardedWrites(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.PushPlans(); fixture.setUp()
        self.plan = fixture.plan()
        self.plan['serial'] = Device.serial
        self.device = Device(fixture.row)
        self.old = copy.deepcopy(self.device.row)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backup = Path(self.temp.name) / 'backup.json'

    def test_backup_then_write_and_restore_keep_other_fields(self):
        result = apply_position(self.plan, self.device, self.backup)
        self.assertEqual(result['status'], 'written')
        backup = json.loads(self.backup.read_text())
        self.assertEqual(backup['original_row'], self.old)
        self.assertEqual(json.loads(self.device.row['extraAttributes'])['retained'], {'theme': 'unchanged'})
        self.assertEqual(self.device.row['location'], self.old['location'])
        self.assertEqual(restore_position(self.device, self.backup)['status'], 'restored')
        self.assertEqual(self.device.row, self.old)
        self.assertEqual(restore_position(self.device, self.backup)['status'], 'unchanged')

    def test_existing_backup_never_overwritten(self):
        self.backup.write_text('retain')
        with self.assertRaises(FileExistsError):
            apply_position(self.plan, self.device, self.backup)
        self.assertEqual(self.backup.read_text(), 'retain')
        self.assertEqual(self.device.writes, 0)
        self.assertEqual(self.device.stops, 0)

    def test_newer_boox_before_backup_blocks_all_writes(self):
        self.device.row['lastAccess'] = '3000'
        with self.assertRaises(PushError):
            apply_position(self.plan, self.device, self.backup)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.device.writes, 0)

    def test_changes_on_stop_block_write_and_retain_backup(self):
        self.device.change_on_stop = True
        with self.assertRaises(PushError):
            apply_position(self.plan, self.device, self.backup)
        self.assertTrue(self.backup.exists())
        self.assertEqual(self.device.writes, 0)

    def test_race_at_update_is_rejected_conditionally(self):
        self.device.race = True
        with self.assertRaises(PushError):
            apply_position(self.plan, self.device, self.backup)
        self.assertTrue(self.backup.exists())
        self.assertEqual(self.device.writes, 0)
        self.assertEqual(self.device.row['lastAccess'], '5000')

    def test_failed_readback_is_not_reported_as_success(self):
        self.device.corrupt = True
        with self.assertRaises(PushError):
            apply_position(self.plan, self.device, self.backup)
        self.assertTrue(self.backup.exists())

    def test_replaced_content_is_protected(self):
        self.device.hash = 'c' * 64
        with self.assertRaises(PushError):
            apply_position(self.plan, self.device, self.backup)
        self.assertEqual(self.device.writes, 0)

    def test_other_device_is_protected(self):
        self.device.serial = 'other-device'
        with self.assertRaises(PushError):
            apply_position(self.plan, self.device, self.backup)
        self.assertEqual(self.device.writes, 0)

    def test_post_push_reading_is_not_overwritten_by_restore(self):
        apply_position(self.plan, self.device, self.backup)
        self.device.row['lastAccess'] = '9000'
        with self.assertRaises(PushError):
            restore_position(self.device, self.backup)
        self.assertEqual(self.device.row['lastAccess'], '9000')

    def test_unverified_plan_never_writes(self):
        self.plan['status'] = 'unsupported'
        with self.assertRaises(PushError):
            apply_position(self.plan, self.device, self.backup)
        self.assertEqual(self.device.writes, 0)

    def test_wrong_original_book_in_backup_is_rejected(self):
        apply_position(self.plan, self.device, self.backup)
        backup = json.loads(self.backup.read_text())
        backup['original_row']['uuid'] = 'another-volume'
        self.backup.write_text(json.dumps(backup))
        with self.assertRaises(PushError):
            restore_position(self.device, self.backup)
        self.assertEqual(self.device.writes, 1)

    def test_failed_pending_marker_prevents_device_write(self):
        def remember():
            self.assertTrue(self.backup.exists())
            self.assertEqual(json.loads(self.backup.read_text())['original_row'], self.old)
            raise RuntimeError('Library is read only')
        with self.assertRaises(RuntimeError):
            apply_position(self.plan, self.device, self.backup, before_write=remember)
        self.assertEqual(self.device.writes, 0)
        self.assertEqual(self.device.row, self.old)


class AndroidBindings(unittest.TestCase):
    def test_legacy_or_unknown_android_blocked_before_writes(self):
        from unittest.mock import patch
        for sdk in ('29', 'unknown', ''):
            with self.subTest(sdk=sdk), patch.object(AdbReadingDevice, '_run', return_value='List of devices attached\nsynthetic\tdevice\n'), patch.object(AdbReadingDevice, '_shell', return_value=sdk):
                with self.assertRaises(PushError):AdbReadingDevice('synthetic-adb')

    def test_android_12_or_newer_can_preview(self):
        from unittest.mock import patch
        with patch.object(AdbReadingDevice, '_run', return_value='List of devices attached\nsynthetic\tdevice\n'), patch.object(AdbReadingDevice, '_shell', return_value='33'):
            self.assertEqual(AdbReadingDevice('synthetic-adb').serial, 'synthetic')

    def test_json_colons_and_backslashes_survive_android_13_parser(self):
        device = object.__new__(AdbReadingDevice)
        calls = []
        device._shell = lambda *args: calls.append(args) or ''
        values = {'extraAttributes': json.dumps({'path': 'C:\\Books:volume', 'quote': "O'Brien"}), 'lastAccess': '6000'}
        device.query = lambda uuid: dict(values)
        self.assertEqual(device.update('u', {'uuid': 'u', 'location': '/sdcard/test.epub', 'extraAttributes': '{}', 'lastAccess': '1'}, values), 1)
        for i, token in enumerate(calls[0]):
            if token != '--bind':
                continue
            # Reproduce the public AOSP content parser, not Python split().
            chunks, part, escaped = [], '', False
            for char in calls[0][i + 1]:
                if escaped:
                    part += char; escaped = False
                elif char == '\\':
                    escaped = True
                elif char == ':':
                    chunks.append(part); part = ''
                else:
                    part += char
            chunks.append(part)
            self.assertEqual(len(chunks), 3)
            self.assertEqual(chunks[1], 's')
            self.assertEqual(chunks[2], values[chunks[0]])

    def test_silent_response_without_readback_is_not_success(self):
        device = object.__new__(AdbReadingDevice)
        device._shell = lambda *args: ''
        device.query = lambda uuid: {'extraAttributes': '{}', 'lastAccess': '1'}
        self.assertEqual(device.update('u', {'uuid': 'u', 'location': '/sdcard/test.epub', 'extraAttributes': '{}', 'lastAccess': '1'}, {'extraAttributes': '{"new":1}', 'lastAccess': '2'}), 0)


if __name__ == '__main__':
    unittest.main()
