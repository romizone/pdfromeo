"""Generic QThread worker that runs a function in a background thread.

Design: the Worker just invokes ``fn(*args, **kwargs)`` and emits
``finished(result)`` on success or ``failed(message)`` on exception.
The caller is responsible for any callbacks (log, progress, is_cancelled)
— we don't try to be clever about injecting them, because that leads to
"got multiple values for argument" errors when a function has parameters
that share a name with our auto-injection (this previously bit us in
``BaseTool.run``).
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal


class Worker(QObject):
    """Run a function in a background thread.

    Signals:
        finished(result):  emitted on success, with the function's return value
        failed(message):   emitted on exception, with a string message
        cancelled():       emitted when ``cancel()`` was called before completion
    """
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(int, int)
    log = Signal(str)

    def __init__(self, fn: Callable[..., Any], *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        self.cancelled.emit()

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.finished.emit(result)
        except Exception as e:  # surface to UI
            self.failed.emit(str(e))


def run_in_thread(worker: Worker) -> QThread:
    """Wire a Worker to a QThread and start it. Returns the thread."""
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    worker.cancelled.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread
