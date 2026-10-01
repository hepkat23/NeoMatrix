"""Separate, opt-in BOOX cleanup dialog; ordinary Sync remains read-only."""
from pathlib import Path
from qt.core import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
                     QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                     QVBoxLayout, Qt)
from calibre_plugins.frankenboox.background import run_background
from calibre_plugins.frankenboox.storage_cleanup import AdbDevice, clean, restore, scan_device, verify_backup


def size_text(size):
    if size is None:
        return 'Unknown'
    if size < 1024:
        return '%d bytes' % size
    if size >= 1024 * 1024:
        return '%.2f MB' % (size / (1024 * 1024))
    return '%.1f KB' % (size / 1024)


class CleanupDialog(QDialog):
    def __init__(self, parent, adb):
        super().__init__(parent)
        self.adb = adb
        self.device = None
        self.scan = None
        self.visible_candidates = []
        self._sync_busy = False
        self.setWindowTitle('NeoMatrix — BOOX storage cleanup')
        self.resize(1060, 650)
        layout = QVBoxLayout(self)
        note = QLabel('Scan checks NeoReader handwriting and page caches against book files on internal memory and storage cards. '
                      'Books still on the device, unknown data and Kindle downloads are protected. '
                      'Removal first saves a verified backup on your computer. Shared BOOX databases are retained.')
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.scan_btn = QPushButton('Scan BOOX storage')
        self.remove_btn = QPushButton('Back up and remove selected…')
        self.restore_btn = QPushButton('Restore a cleanup backup…')
        self.remove_btn.setEnabled(False)
        self.scan_btn.clicked.connect(self.refresh)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.restore_btn.clicked.connect(self.restore_backup)
        for button in (self.scan_btn, self.remove_btn, self.restore_btn):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.show_protected = QCheckBox('Show protected entries')
        self.show_protected.toggled.connect(lambda checked: self.populate() if self.scan else None)
        layout.addWidget(self.show_protected)
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
        layout.addWidget(self.table)
        self.summary = QLabel('Scan reads the device without changing any files.')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        close = QPushButton('Close')
        close.clicked.connect(self.accept)
        layout.addWidget(close)

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
        self.table.setRowCount(0)
        try:
            def task(status):
                device = AdbDevice(self.adb)
                return device, scan_device(device, status)
            self.device, self.scan = run_background(self, 'Scanning BOOX storage…', task)
            self.populate()
        except Exception as error:
            self.summary.setText('Scan could not complete. No files were changed.')
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
