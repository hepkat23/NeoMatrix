"""Run blocking work away from Qt's GUI thread, keeping ownership until finished."""
from qt.core import QThread, QEventLoop, Qt, pyqtSignal


class Worker(QThread):
    status = pyqtSignal(str)

    def __init__(self, task, parent):
        super().__init__(parent)
        # Calibre's native RAR opener uses over 512 KiB of stack on macOS.
        # Qt's default worker stack can be smaller than that allocation.
        self.setStackSize(8 * 1024 * 1024)
        self.task = task
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.task(self.status.emit)
        except BaseException as error:
            self.error = error


def run_background(owner, message, task):
    owner.summary.setText(message)
    activity_status = getattr(owner, 'activity_status', None)
    activity_bar = getattr(owner, 'activity_bar', None)
    if activity_status is not None:
        activity_status.setText(message)
    if activity_bar is not None:
        activity_bar.show()
    worker = Worker(task, owner)
    loop = QEventLoop()
    worker.status.connect(owner.summary.setText, Qt.ConnectionType.QueuedConnection)
    if activity_status is not None:
        worker.status.connect(activity_status.setText, Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(loop.quit, Qt.ConnectionType.QueuedConnection)
    owner._sync_busy = True
    set_busy = getattr(owner, 'set_busy', None)
    if set_busy is not None:
        set_busy(True)
    else:
        owner.setEnabled(False)
    try:
        worker.start()
        loop.exec()
        worker.wait()
        if worker.error is not None:
            raise worker.error
        return worker.result
    finally:
        owner._sync_busy = False
        if set_busy is not None:
            set_busy(False)
        else:
            owner.setEnabled(True)
        if activity_bar is not None:
            activity_bar.hide()
        worker.deleteLater()
