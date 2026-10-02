"""Separate, opt-in BOOX cleanup dialog; ordinary Sync remains read-only."""
from pathlib import Path
from qt.core import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
                     QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                     QVBoxLayout, QTabWidget, QWidget, Qt)
from calibre_plugins.frankenboox.background import run_background
from calibre_plugins.frankenboox.storage_cleanup import AdbDevice, clean, restore, scan_device, verify_backup
from calibre_plugins.frankenboox.boox_files import record_token


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
        note = QLabel('Scan finds old book records and unused reader files. '
                      'Book records can be excluded from sync while keeping their history. '
                      'Removing unused files saves a backup first.')
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.scan_btn = QPushButton('Scan BOOX')
        self.exclude_btn = QPushButton('Exclude selected from sync')
        self.include_btn = QPushButton('Include selected again')
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
        for button in (self.scan_btn, self.exclude_btn, self.include_btn, self.remove_btn, self.restore_btn):
            buttons.addWidget(button)
        layout.addLayout(buttons)
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
        self.records_summary = QLabel('Scan checks the saved book locations. Exclusions can be undone here.')
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
        self.update_actions()
        close = QPushButton('Close')
        close.clicked.connect(self.accept)
        layout.addWidget(close)

    def update_actions(self, *args):
        records = self.tabs.currentIndex() == 0
        self.exclude_btn.setVisible(records)
        self.include_btn.setVisible(records)
        self.remove_btn.setVisible(not records)
        self.restore_btn.setVisible(not records)

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
                if token and self.settings is not None:
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

    def refresh(self):
        self.remove_btn.setEnabled(False)
        self.scan = None
        self.visible_candidates = []
        self.visible_records = []
        self.table.setRowCount(0)
        self.records_table.setRowCount(0)
        self.exclude_btn.setEnabled(False); self.include_btn.setEnabled(False)
        try:
            def task(status):
                device = AdbDevice(self.adb)
                return device, scan_device(device, status)
            self.device, self.scan = run_background(self, 'Scanning BOOX storage…', task)
            self.populate()
            self.populate_records()
        except Exception as error:
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
                                                 'NeoMatrix-BOOX-storage-backup.zip', 'ZIP backup (*.zip)')
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
        filename, _ = QFileDialog.getOpenFileName(self, 'Choose a NeoMatrix storage backup', '', 'ZIP backup (*.zip)')
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
