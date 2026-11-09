"""Watch the assignment folder. Debounced, because editors burst on save."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .. import config
from . import extract
from .pipeline import watch_root

log = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    """Collects touched assignment folders and flushes them after a quiet period."""

    def __init__(self, on_folder: Callable[[Path], None], debounce: float) -> None:
        self.on_folder = on_folder
        self.debounce = debounce
        self._pending: set[Path] = set()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type == "deleted":
            return
        path = Path(str(event.src_path))
        if not extract.is_supported(path):
            return
        root = watch_root().resolve()
        try:
            path.resolve().relative_to(root)
        except ValueError:
            return
        with self._lock:
            self._pending.add(path.parent)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            folders, self._pending = self._pending, set()
            self._timer = None
        for folder in sorted(folders):
            log.info("ingest triggered for %s", folder)
            try:
                self.on_folder(folder)
            except Exception:  # noqa: BLE001 - never kill the watcher thread
                log.exception("ingest callback failed for %s", folder)


class AssignmentWatcher:
    """`on_folder` is called from a watchdog thread — make it thread-safe."""

    def __init__(
        self,
        on_folder: Callable[[Path], None],
        root: Path | None = None,
        debounce: float = config.DEBOUNCE_SECONDS,
    ) -> None:
        self.root = Path(root or watch_root())
        self.handler = _Handler(on_folder, debounce)
        self.observer = Observer()

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.observer.schedule(self.handler, str(self.root), recursive=True)
        self.observer.start()
        log.info("watching %s", self.root)

    def stop(self) -> None:
        self.observer.stop()
        self.observer.join(timeout=5)
