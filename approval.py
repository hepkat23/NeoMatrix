"""Non-blocking ADB authorization wait; the device must grant access itself."""
import time
from qt.core import QDialog, QVBoxLayout, QLabel, QPushButton, QProcess, QTimer

class ApprovalDialog(QDialog):
    def __init__(self, parent, adb, timeout=120):
        super().__init__(parent)
        self.adb = adb
        self.deadline = time.monotonic() + timeout
        self.setWindowTitle('NeoMatrix — BOOX approval')
        self.label = QLabel('Unlock your BOOX and approve USB debugging.\nWaiting for approval…', self)
        layout = QVBoxLayout(self); layout.addWidget(self.label)
        cancel = QPushButton('Cancel',self); cancel.clicked.connect(self.reject); layout.addWidget(cancel)
        self.process = QProcess(self)
        self.process.finished.connect(self.checked)
        self.process.errorOccurred.connect(self.failed)
        self.timer = QTimer(self); self.timer.setInterval(1000); self.timer.timeout.connect(self.poll)
        self.timer.start(); QTimer.singleShot(0,self.poll)

    def poll(self):
        remaining = max(0,int(self.deadline-time.monotonic()))
        if not remaining:
            self.label.setText('Approval timed out. Unlock the BOOX, reconnect USB, and try Sync again.')
            self.timer.stop()
            if self.process.state() != QProcess.ProcessState.NotRunning:
                self.process.kill()
            return
        self.label.setText('Unlock your BOOX and approve USB debugging.\nSync continues as soon as you approve.')
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self.process.start(self.adb,['get-state'])

    def checked(self, *args):
        if not self.timer.isActive():
            return
        out = bytes(self.process.readAllStandardOutput()).decode('utf-8','replace').strip()
        err = bytes(self.process.readAllStandardError()).decode('utf-8','replace')
        if self.process.exitCode() == 0 and out == 'device':
            self.accept()
        elif 'more than one' in err:
            self.timer.stop(); self.label.setText('More than one ADB device is connected. Leave only the BOOX connected and retry.')

    def failed(self, *args):
        if self.process.error() == QProcess.ProcessError.FailedToStart:
            self.timer.stop(); self.label.setText('Could not start ADB. Check that Android Platform Tools is installed.')

    def done(self, result):
        self.timer.stop()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)
        super().done(result)
