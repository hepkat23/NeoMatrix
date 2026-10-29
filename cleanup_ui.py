"""Separate, opt-in BOOX cleanup dialog; ordinary Sync remains read-only."""
from pathlib import Path
from qt.core import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
                     QMessageBox, QPushButton, QProgressBar, QTableWidget, QTableWidgetItem,
                     QVBoxLayout, QTabWidget, QWidget, Qt)
from calibre_plugins.frankenboox.background import run_background
from calibre_plugins.frankenboox.storage_cleanup import AdbDevice, clean, restore, scan_device, verify_backup
from calibre_plugins.frankenboox.boox_files import record_token
from calibre_plugins.frankenboox.record_cleanup import (remove_records, restore_records, read_backup, eligible_records,
                                                        backup_folder, suggested_backup_path)


def size_text(size):
    if size is None:
        return 'Unknown'
    if size < 1024:
        return '%d bytes' % size
    if size >= 1024 * 1024:
        return '%.2f MB' % (size / (1024 * 1024))
    return '%.1f KB' % (size / 1024)


class CleanupDialog(QDialog):
    def __init__(self, parent, adb, settings=None):
        super().__init__(parent)
        self.adb = adb
        self.settings = settings
        self.device = None
        self.scan = None
        self.visible_candidates = []
        self.visible_records = []
        self._sync_busy = False
        self.setWindowTitle('NeoMatrix — BOOX cleanup')
        self.resize(1060, 650)
        layout = QVBoxLayout(self)
        note = QLabel('Manage old BOOX book records and unused reader files. '
                      'Removal saves a backup first. Books, notes and bookmarks are kept.')
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.scan_btn = QPushButton('Scan BOOX')
        self.delete_records_btn = QPushButton('Remove selected from BOOX…')
        self.restore_records_btn = QPushButton('Restore record backup…')
        self.delete_records_btn.setAutoDefault(False)
        self.delete_records_btn.clicked.connect(self.remove_book_records)
        self.restore_records_btn.clicked.connect(self.restore_book_records)
        self.exclude_btn = QPushButton('Exclude from sync')
        self.include_btn = QPushButton('Include again')
        self.remove_btn = QPushButton('Back up and remove selected…')
        self.restore_btn = QPushButton('Restore a cleanup backup…')
        self.remove_btn.setEnabled(False)
        self.exclude_btn.setEnabled(False)
        self.include_btn.setEnabled(False)
        self.scan_btn.clicked.connect(self.refresh)
        self.exclude_btn.clicked.connect(lambda: self.set_record_exclusions(True))
        self.include_btn.clicked.connect(lambda: self.set_record_exclusions(False))
        self.remove_btn.clicked.connect(self.remove_selected)
        self.restore_btn.clicked.connect(self.restore_backup)
        buttons.addWidget(self.scan_btn)
        self.select_all_btn = QPushButton('Select all')
        self.clear_selection_btn = QPushButton('Clear selection')
        self.select_all_btn.clicked.connect(lambda: self.select_rows(True))
        self.clear_selection_btn.clicked.connect(lambda: self.select_rows(False))
        buttons.addWidget(self.select_all_btn)
        buttons.addWidget(self.clear_selection_btn)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.activity_status = QLabel('Ready to scan. No sync is needed first.')
        self.activity_status.setWordWrap(True)
        layout.addWidget(self.activity_status)
        self.activity_bar = QProgressBar()
        self.activity_bar.setRange(0, 0)
        self.activity_bar.setTextVisible(False)
        self.activity_bar.hide()
        layout.addWidget(self.activity_bar)
        self.tabs = QTabWidget()
        records_page = QWidget(); records_layout = QVBoxLayout(records_page)
        self.show_current = QCheckBox('Show current books too')
        self.show_current.toggled.connect(lambda checked: self.populate_records() if self.scan else None)
        records_layout.addWidget(self.show_current)
        self.records_table = QTableWidget(0, 4)
        self.records_table.setHorizontalHeaderLabels(['Select', 'Book', 'Status', 'File locations'])
        self.records_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.records_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.records_table.setColumnWidth(0, 55)
        self.records_table.setColumnWidth(1, 260)
        self.records_table.setColumnWidth(2, 250)
        self.records_table.horizontalHeader().setStretchLastSection(True)
        self.records_table.itemChanged.connect(self.update_record_selection)
        records_layout.addWidget(self.records_table)
        self.records_summary = QLabel('Moving or finishing a book keeps its history. Review old locations before removing records.')
        self.records_summary.setWordWrap(True)
        records_layout.addWidget(self.records_summary)
        self.tabs.addTab(records_page, 'Book records')
        files_page = QWidget(); files_layout = QVBoxLayout(files_page)
        self.show_protected = QCheckBox('Show protected entries')
        self.show_protected.toggled.connect(lambda checked: self.populate() if self.scan else None)
        files_layout.addWidget(self.show_protected)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Select', 'Book / download', 'Data', 'Size', 'Status'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(1, 300)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(3, 85)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self.update_selection)
        files_layout.addWidget(self.table)
        self.summary = QLabel('Scan reads the device without changing any files.')
        self.summary.setWordWrap(True)
        files_layout.addWidget(self.summary)
        self.tabs.addTab(files_page, 'Unused files')
        self.tabs.currentChanged.connect(self.update_actions)
        layout.addWidget(self.tabs)
        actions = QHBoxLayout()
        for button in (self.delete_records_btn, self.restore_records_btn, self.exclude_btn, self.include_btn, self.remove_btn, self.restore_btn):
            actions.addWidget(button)
        actions.addStretch()
        close = QPushButton('Close')
        self.close_btn = close
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.update_actions()
        self.update_record_selection()

    def set_busy(self, busy):
        controls = (self.scan_btn, self.select_all_btn, self.clear_selection_btn, self.tabs,
                    self.delete_records_btn, self.restore_records_btn, self.exclude_btn,
                    self.include_btn, self.remove_btn, self.restore_btn, self.close_btn)
        if busy:
            self._busy_enabled = {control: control.isEnabled() for control in controls}
            for control in controls:
                control.setEnabled(False)
        else:
            for control, enabled in self._busy_enabled.items():
                control.setEnabled(enabled)

    def update_actions(self, *args):
        records = self.tabs.currentIndex() == 0
        self.delete_records_btn.setVisible(records)
        self.restore_records_btn.setVisible(records)
        self.exclude_btn.setVisible(records)
        self.include_btn.setVisible(records)
        self.remove_btn.setVisible(not records)
        self.restore_btn.setVisible(not records)
        self.update_bulk_selection()

    def selection_table(self):
        return self.records_table if self.tabs.currentIndex() == 0 else self.table

    def update_bulk_selection(self):
        table = self.selection_table()
        checks = [table.item(index, 0) for index in range(table.rowCount())]
        checks = [item for item in checks if item is not None
                  and item.flags() & Qt.ItemFlag.ItemIsUserCheckable]
        self.select_all_btn.setEnabled(any(item.checkState() != Qt.CheckState.Checked for item in checks))
        self.clear_selection_btn.setEnabled(any(item.checkState() == Qt.CheckState.Checked for item in checks))

    def select_rows(self, selected):
        table = self.selection_table()
        table.blockSignals(True)
        try:
            for index in range(table.rowCount()):
                item = table.item(index, 0)
                if item is not None and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
        finally:
            table.blockSignals(False)
        self.update_record_selection()
        self.update_selection()

    def excluded_tokens(self):
        if not self.scan or self.settings is None:
            return set()
        return set((self.settings.get('excluded_book_records') or {}).get(self.scan.serial, ()))

    def selected_record_tokens(self):
        return {token for index, (_, _, token) in enumerate(self.visible_records)
                if token and self.records_table.item(index, 0).checkState() == Qt.CheckState.Checked}

    def update_record_selection(self, *args):
        selected = self.selected_record_tokens(); excluded = self.excluded_tokens()
        self.exclude_btn.setEnabled(self.settings is not None and bool(selected - excluded))
        self.include_btn.setEnabled(self.settings is not None and bool(selected & excluded))
        removable = [token for record, row, token in self.visible_records
                     if token in selected and record.state != 'P']
        self.delete_records_btn.setEnabled(bool(removable) and len(removable) == len(selected))
        self.delete_records_btn.setToolTip('Remove selected saved library entries from BOOX after backing them up. Current books are protected.')
        self.update_bulk_selection()

    def set_record_exclusions(self, exclude):
        if self.settings is None or not self.scan:
            return
        selected = self.selected_record_tokens()
        if not selected:
            return
        excluded = self.excluded_tokens()
        current = {record_token(self.scan.serial, row) for row in self.scan.metadata}
        excluded.intersection_update(current)
        excluded.update(selected) if exclude else excluded.difference_update(selected)
        settings = dict(self.settings.get('excluded_book_records') or {})
        settings[self.scan.serial] = sorted(excluded)
        self.settings['excluded_book_records'] = settings
        self.populate_records()
        self.activity_status.setText('%d record(s) %s. Notes, bookmarks and history are kept.' %
                                     (len(selected), 'excluded from sync' if exclude else 'included again'))

    def populate_records(self):
        excluded = self.excluded_tokens()
        records = list(zip(self.scan.book_records, self.scan.metadata))
        self.visible_records = [(record, row, record_token(self.scan.serial, row)) for record, row in records
                                if record.state != 'P' or self.show_current.isChecked()
                                or record_token(self.scan.serial, row) in excluded]
        self.records_table.blockSignals(True)
        try:
            self.records_table.setRowCount(len(self.visible_records))
            for index, (record, row, token) in enumerate(self.visible_records):
                check = QTableWidgetItem('')
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if token:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                    check.setCheckState(Qt.CheckState.Unchecked)
                check.setFlags(flags); self.records_table.setItem(index, 0, check)
                status = 'Excluded from sync; history kept' if token in excluded else record.status
                paths = ['Saved: ' + path for path in record.paths]
                paths += ['Possible copy: ' + path for path in record.related_paths]
                location = '\n'.join(paths) or 'No saved file path'
                for col, value in enumerate((record.title, status, location), 1):
                    item = QTableWidgetItem(value); item.setToolTip(location)
                    self.records_table.setItem(index, col, item)
                self.records_table.setRowHeight(index, 62 if record.related_paths else 46)
        finally:
            self.records_table.blockSignals(False)
        old = sum(record.state in ('M', 'C') for record, row in records)
        uncertain = sum(record.state == 'U' for record, row in records)
        ignored = sum(record_token(self.scan.serial, row) in excluded for record, row in records)
        self.records_summary.setText('%d old location(s); %d could not be checked; %d excluded from sync. '
                                     'History is kept. Changed records return to sync automatically.' % (old, uncertain, ignored))
        self.update_record_selection()

    def accept(self):
        if not self._sync_busy:
            super().accept()

    def reject(self):
        if not self._sync_busy:
            super().reject()

    def closeEvent(self, event):
        if self._sync_busy:
            event.ignore()
        else:
            super().closeEvent(event)

    def selected_paths(self):
        if not self.scan:
            return []
        return [candidate.path for index, candidate in enumerate(self.visible_candidates)
                if candidate.eligible and self.table.item(index, 0).checkState() == Qt.CheckState.Checked]

    def update_selection(self, *args):
        self.remove_btn.setEnabled(bool(self.selected_paths()))
        self.update_bulk_selection()

    def refresh(self):
        self.remove_btn.setEnabled(False)
        self.scan = None
        self.visible_candidates = []
        self.visible_records = []
        self.table.setRowCount(0)
        self.records_table.setRowCount(0)
        self.exclude_btn.setEnabled(False); self.include_btn.setEnabled(False)
        self.delete_records_btn.setEnabled(False)
        self.update_bulk_selection()
        try:
            def task(status):
                device = AdbDevice(self.adb)
                return device, scan_device(device, status)
            self.device, self.scan = run_background(self, 'Scanning BOOX storage…', task)
            self.populate()
            self.populate_records()
            self.activity_status.setText('Scan complete. Select items, then use the buttons below the list.')
        except Exception as error:
            self.activity_status.setText('Scan could not complete. No files or exclusions were changed.')
            self.summary.setText('Scan could not complete. No files were changed.')
            self.records_summary.setText('Scan could not complete. No exclusions were changed.')
            QMessageBox.warning(self, 'BOOX storage scan', str(error))

    def populate(self):
        self.table.blockSignals(True)
        try:
            self.visible_candidates = [c for c in self.scan.candidates
                                       if c.eligible or self.show_protected.isChecked()]
            self.table.setRowCount(len(self.visible_candidates))
            for index, candidate in enumerate(self.visible_candidates):
                checkbox = QTableWidgetItem('')
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if candidate.eligible:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                checkbox.setFlags(flags)
                if candidate.eligible:
                    checkbox.setCheckState(Qt.CheckState.Unchecked)
                checkbox.setToolTip(candidate.path)
                self.table.setItem(index, 0, checkbox)
                for col, value in enumerate((candidate.title, candidate.kind, size_text(candidate.size), candidate.status), 1):
                    item = QTableWidgetItem(value)
                    item.setToolTip(candidate.path)
                    self.table.setItem(index, col, item)
        finally:
            self.table.blockSignals(False)
        count = sum(c.eligible for c in self.scan.candidates)
        total = sum(c.size for c in self.scan.candidates if c.eligible)
        summary = '%d old-data folder(s), %s available for backed-up removal.' % (count, size_text(total))
        if count == 0:
            summary = 'No data was identified for removal. Books and uncertain data are protected.'
        summary += ' Use Show protected entries to inspect the rest.'
        if self.scan.warnings:
            summary += ' Some folders could not be checked and are protected.\n' + '\n'.join(self.scan.warnings[:4])
        self.summary.setText(summary)
        self.update_selection()

    def remove_selected(self):
        selected = self.selected_paths()
        if not selected:
            return
        chosen = [c for c in self.scan.candidates if c.path in selected]
        text = ('Save and close any open books on the BOOX first.\n\n'
                'NeoMatrix will stop NeoReader, save a verified backup on this computer, then remove '
                '%s from %d old-data folder(s).\n\n%s\n\n'
                'The backup restores these external files to the same BOOX. Keep it: handwriting can '
                'still be valuable after its book has been deleted. BOOX databases and ebook files are retained.\n\nContinue?') % (
                    size_text(sum(c.size for c in chosen)), len(chosen), '\n'.join(c.title + ' — ' + c.kind for c in chosen[:8]))
        if QMessageBox.question(self, 'Back up and remove old book data', text,
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        filename, _ = QFileDialog.getSaveFileName(self, 'Save cleanup backup on this computer',
                                                 suggested_backup_path('NeoMatrix-BOOX-storage-backup', '.zip'), 'ZIP backup (*.zip)')
        if not filename:
            return
        if not filename.lower().endswith('.zip'):
            filename += '.zip'
        try:
            result = run_background(self, 'Backing up selected data…',
                                    lambda status: clean(self.device, selected, filename, status))
            QMessageBox.information(self, 'Cleanup complete', 'Removed %s in %d file(s).\n\nBackup: %s\n\n'
                                    'You can open NeoReader again. BOOX cloud sync may recreate retained cloud data.' %
                                    (size_text(result['removed_bytes']), result['removed_files'], result['backup']))
        except Exception as error:
            QMessageBox.warning(self, 'Cleanup stopped', str(error) + '\n\nYou can open NeoReader again.')
        self.refresh()

    def restore_backup(self):
        filename, _ = QFileDialog.getOpenFileName(self, 'Choose a NeoMatrix storage backup', str(backup_folder()), 'ZIP backup (*.zip)')
        if not filename:
            return
        try:
            manifest = run_background(self, 'Checking backup…', lambda status: verify_backup(filename))
            text = ('Save and close any open books on the BOOX first.\n\n'
                    'Restore %d backed-up file(s) to their original folders on the same BOOX? '
                    'NeoReader will be stopped. Different existing files will never be overwritten.\n\n'
                    'This restores external files; it does not recreate deleted ebooks or rebuild BOOX databases.') % len(manifest['files'])
            if QMessageBox.question(self, 'Restore cleanup backup', text,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            def task(status):
                device = self.device or AdbDevice(self.adb)
                return restore(device, filename, status)
            result = run_background(self, 'Restoring cleanup backup…', task)
            QMessageBox.information(self, 'Restore complete', '%d file(s) restored; %d identical file(s) already present.\n\n'
                                    'You can open NeoReader again.' % (result['restored_files'], result['already_present']))
        except Exception as error:
            QMessageBox.warning(self, 'Restore stopped', str(error))
        self.refresh()


    def remove_book_records(self):
        if not self.scan or not self.device:
            return
        tokens = self.selected_record_tokens()
        try:
            rows = eligible_records(self.scan, tokens)
        except Exception as error:
            QMessageBox.warning(self, 'BOOX book records', str(error)); return
        text = ('Save and close any open books on BOOX first.\n\n'
                'Back up and remove %d selected library record(s) from BOOX?\n\n%s\n\n'
                'Ebook files, notes, bookmarks and handwriting stay on the device. '
                'NeoReader may hide the removed entries and their history until you restore the backup. '
                'Possible moved copies and unchecked locations need your review. '
                'Cloud sync or a new library scan may recreate entries.') % (
                    len(rows), '\n'.join((row.get('title') or row.get('name') or 'Untitled book') for row in rows[:10]))
        if QMessageBox.question(self, 'Remove selected BOOX records', text,
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        filename, _ = QFileDialog.getSaveFileName(self, 'Save BOOX record backup',
                                                  suggested_backup_path('NeoMatrix-BOOX-record-backup', '.json'), 'Record backup (*.json)')
        if not filename: return
        if not filename.lower().endswith('.json'): filename += '.json'
        try:
            result = run_background(self, 'Backing up selected BOOX records…',
                                    lambda status: remove_records(self.device, self.scan, tokens, filename, status))
            QMessageBox.information(self, 'BOOX records removed', '%d library record(s) removed.\n\nBackup: %s' %
                                    (result['removed_records'], result['backup']))
        except Exception as error:
            QMessageBox.warning(self, 'Record removal stopped', str(error))
        self.refresh()

    def restore_book_records(self):
        filename, _ = QFileDialog.getOpenFileName(self, 'Choose BOOX record backup', str(backup_folder()), 'Record backup (*.json)')
        if not filename: return
        try:
            backup = read_backup(filename)
            if QMessageBox.question(self, 'Restore BOOX book records',
                                    'Save and close open books on BOOX first.\n\nRestore %d book record(s) to the same BOOX? '
                                    'Newer or conflicting records are protected.' % len(backup['records']),
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            def task(status):
                device = self.device or AdbDevice(self.adb)
                return restore_records(device, filename, status)
            result = run_background(self, 'Restoring BOOX book records…', task)
            QMessageBox.information(self, 'BOOX records restored', '%d record(s) restored; %d already present.' %
                                    (result['restored_records'], result['already_present']))
        except Exception as error:
            QMessageBox.warning(self, 'Record restore stopped', str(error))
        self.refresh()
