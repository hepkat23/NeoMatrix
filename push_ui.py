"""Separate, explicit Calibre to BOOX position push; normal Sync never pushes."""
from pathlib import Path
from qt.core import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QLabel,
                     QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, Qt)
from calibre_plugins.frankenboox.background import run_background
from calibre_plugins.frankenboox.boox_push import AdbReadingDevice, restore_position
from calibre_plugins.frankenboox.library_push import preview, PUSH_KEY


class PositionPushDialog(QDialog):
    def __init__(self, parent, adb, book_ids):
        super().__init__(parent)
        self.owner, self.db, self.adb = parent, parent.db, adb
        self.book_ids = list(book_ids)
        self.device = None
        self.rows = []
        self._sync_busy = False
        self.setWindowTitle('NeoMatrix — push reading positions to BOOX')
        self.resize(1000, 620)
        layout = QVBoxLayout(self)
        note = QLabel('Close and save books in both readers before previewing. This first push feature supports EPUB positions '
                      'already verified in saved BOOX data, such as a mapped BOOX bookmark. Other locations are skipped. '
                      'Selected pushes save a backup on your computer and close NeoReader. Reopen each pushed book on BOOX '
                      'to load the position and refresh its percentage before running Sync. Highlights and notes are not pushed.')
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.preview_btn = QPushButton('Preview connected BOOX')
        self.push_btn = QPushButton('Back up and push selected…')
        self.restore_btn = QPushButton('Restore a position backup…')
        self.push_btn.setEnabled(False)
        self.preview_btn.clicked.connect(self.refresh)
        self.push_btn.clicked.connect(self.push_selected)
        self.restore_btn.clicked.connect(self.restore_backup)
        for button in (self.preview_btn, self.push_btn, self.restore_btn):buttons.addWidget(button)
        layout.addLayout(buttons)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Select', 'Calibre book', 'BOOX copy', 'Result'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 275)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self.update_selection)
        layout.addWidget(self.table)
        self.summary = QLabel('Preview reads the selected books and the connected device. It makes no position changes.')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        close = QPushButton('Close');close.clicked.connect(self.accept);layout.addWidget(close)

    def accept(self):
        if not self._sync_busy:super().accept()
    def reject(self):
        if not self._sync_busy:super().reject()
    def closeEvent(self, event):
        if self._sync_busy:event.ignore()
        else:super().closeEvent(event)

    def selected(self):
        return [row for i, row in enumerate(self.rows) if row['plan']['status'] == 'ready'
                and self.table.item(i, 0).checkState() == Qt.CheckState.Checked]

    def update_selection(self, *args):
        self.push_btn.setEnabled(bool(self.selected()))

    def populate(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            plan = row['plan']
            item = QTableWidgetItem('')
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if plan['status'] == 'ready':
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(i, 0, item)
            for col, text in enumerate((row['title'], row['path'], plan['status'] + ': ' + plan['reason']), 1):
                item = QTableWidgetItem(text);item.setToolTip(text);self.table.setItem(i, col, item)
        self.table.blockSignals(False)
        self.update_selection()

    def refresh(self):
        self.rows = []
        self.table.setRowCount(0)
        self.push_btn.setEnabled(False)
        try:
            def task(status):
                device = AdbReadingDevice(self.adb)
                rows = []
                for bid in self.book_ids:
                    title = self.db.field_for('title', bid) or str(bid)
                    status('Checking ' + title)
                    state, _ = self.owner._archived_state_for_book(bid)
                    try:plan = preview(self.db, bid, state, device)
                    except Exception as error:plan = {'status': 'unsupported', 'reason': str(error)}
                    path = ((state or {}).get('boox_identity') or {}).get('path') or ''
                    rows.append({'book_id': bid, 'title': title, 'path': path, 'plan': plan})
                return device, rows
            self.device, self.rows = run_background(self, 'Reading selected positions…', task)
            self.populate()
            ready = sum(row['plan']['status'] == 'ready' for row in self.rows)
            self.summary.setText(f'{ready} verified position(s) available. Select the books you want to push. Unchanged books need no write.')
        except Exception as error:self.summary.setText('Preview stopped: ' + str(error))

    def push_selected(self):
        chosen = self.selected()
        if not chosen:return
        folder = QFileDialog.getExistingDirectory(self, 'Choose a computer folder for position backups')
        if not folder:return
        names = '\n'.join(row['title'] for row in chosen)
        answer = QMessageBox.question(self, 'Push selected reading positions?',
            'Save a backup and push the verified Calibre position for:\n\n' + names +
            '\n\nNeoReader will close. Reopen each book on BOOX afterward to load the position and refresh its percentage.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:return
        try:
            from calibre_plugins.frankenboox.sync_flow import execute_pushes
            outcomes = run_background(self, 'Preparing backed-up pushes…',
                lambda status: execute_pushes(self.db, self.device, chosen, folder, status))
            self.populate()
            lines = []
            for result in outcomes:
                text = result['title'] + ': ' + result['status']
                if result.get('reason'):text += '; ' + result['reason']
                if result.get('backup'):text += '; backup ' + result['backup']
                lines.append(text)
            self.summary.setText('\n'.join(lines))
        except Exception as error:self.summary.setText('Push stopped: ' + str(error))

    def restore_backup(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose a NeoMatrix position backup', '', 'Position backups (*.json)')
        if not path:return
        if QMessageBox.question(self, 'Restore BOOX position?',
                'Restore the saved position if this BOOX and book still match and no newer reading has occurred?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:return
        try:
            def task(status):
                device = AdbReadingDevice(self.adb)
                result = restore_position(device, path)
                markers = self.db.get_custom_book_data(PUSH_KEY)
                for bid, marker in markers.items():
                    if isinstance(marker, dict) and marker.get('backup') == path and marker.get('serial') == device.serial:
                        self.db.add_custom_book_data(PUSH_KEY, {bid: None}, delete_first=False)
                return result
            result = run_background(self, 'Restoring saved position…', task)
            self.rows = [];self.populate()
            self.summary.setText(result['status'] + '. Reopen NeoReader; the backup remains on your computer.')
        except Exception as error:self.summary.setText('Restore stopped: ' + str(error))


def confirm_sync_pushes(owner, rows, issues):
    box = QMessageBox(owner)
    box.setWindowTitle('Sync — send newer reading positions to BOOX?')
    names = '\n'.join('• ' + row['title'] for row in rows[:8])
    if len(rows) > 8:names += '\n…and ' + str(len(rows) - 8) + ' more'
    box.setText(f'{len(rows)} book(s) have a newer Calibre reading position that can be safely sent to BOOX.\n\n' + names)
    box.setInformativeText('A backup is saved automatically before each change. NeoReader will close. '
                          'Reopen the pushed books on BOOX afterward. Notes and highlights are imported from BOOX only.')
    box.setDetailedText('\n'.join(row['title'] + '\nBOOX copy: ' + row['path'] for row in rows) +
                        ('\n\nPositions kept unchanged:\n' + '\n'.join(issues) if issues else ''))
    send = box.addButton('Sync both ways', QMessageBox.ButtonRole.AcceptRole)
    read = box.addButton('Read BOOX only', QMessageBox.ButtonRole.ActionRole)
    cancel = box.addButton('Cancel', QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(send)
    box.setEscapeButton(cancel)
    box.exec()
    if box.clickedButton() is send:return True
    if box.clickedButton() is read:return False
    return None


def restore_position_backup(owner, adb):
    from calibre_plugins.frankenboox.sync_flow import default_backup_directory
    path, _ = QFileDialog.getOpenFileName(owner, 'Choose a reading-position backup', str(default_backup_directory()), 'Position backups (*.json)')
    if not path:return
    if QMessageBox.question(owner, 'Restore BOOX reading position?',
            'Restore this saved position if the book still matches and you have not read it since the push?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:return
    try:
        def task(status):
            device = AdbReadingDevice(adb)
            result = restore_position(device, path)
            for bid, marker in owner.db.get_custom_book_data(PUSH_KEY).items():
                if isinstance(marker, dict) and marker.get('backup') == path and marker.get('serial') == device.serial:
                    owner.db.add_custom_book_data(PUSH_KEY, {bid: None}, delete_first=False)
            return result
        result = run_background(owner, 'Restoring the saved reading position…', task)
        owner.summary.setText(result['status'] + '. Reopen the book on BOOX; your backup was kept.')
    except Exception as error:owner.summary.setText('Restore stopped: ' + str(error))
