"""Run blocking work away from Qt's GUI thread, keeping ownership until finished."""
from qt.core import QThread, QEventLoop, Qt, pyqtSignal


class Worker(QThread):
    status = pyqtSignal(str)

    def __init__(self, task, parent):
        super().__init__(parent)
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
    worker = Worker(task, owner)
    loop = QEventLoop()
    worker.status.connect(owner.summary.setText, Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(loop.quit, Qt.ConnectionType.QueuedConnection)
    owner._sync_busy = True
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
        owner.setEnabled(True)
        worker.deleteLater()
