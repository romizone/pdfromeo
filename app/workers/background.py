"""Generic QThread worker that runs a function and reports progress."""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal


class Worker(QObject):
    """Run a function in a background thread.

    Signals:
        finished(result):  emitted on success, with the function's return value
        failed(message):   emitted on exception, with a string message
        progress(value, total):  optional, only used if fn calls worker_progress
    """
    finished = Signal(object)
    failed = Signal(str)
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

    def run(self) -> None:
        try:
            # Inject callbacks so the function can report progress
            result = self._fn(
                *self._args,
                log=self._emit_log,
                progress=self._emit_progress,
                is_cancelled=lambda: self._cancelled,
                **self._kwargs,
            )
            if not self._cancelled:
                self.finished.emit(result)
        except Exception as e:  # surface to UI
            self.failed.emit(str(e))

    def _emit_log(self, msg: str) -> None:
        self.log.emit(msg)

    def _emit_progress(self, value: int, total: int) -> None:
        self.progress.emit(value, total)


def run_in_thread(worker: Worker) -> QThread:
    """Wire a Worker to a QThread and start it. Returns the thread."""
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread
