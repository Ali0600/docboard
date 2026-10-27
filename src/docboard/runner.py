"""One sync at a time: collect a project's sources, then write them to the store."""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from docboard.config import Config, Project
from docboard.store import ApplyReport, Store
from docboard.sync import SyncResult, collect


class Busy(RuntimeError):
    pass


@contextmanager
def sync_lock(lock_path: Path, wait: float) -> Iterator[None]:
    """A machine-wide lock on a fixed path, released only by the holder that took it."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy("another sync is running") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def run_sync(cfg: Config, project: Project, db_path: Path, wait: float = 60.0,
             now: datetime | None = None) -> tuple[SyncResult, ApplyReport]:  # fmt: skip
    now = now or datetime.now(UTC)
    with sync_lock(db_path.parent / "sync.lock", wait):
        result = collect(cfg, project, now)
        with Store(db_path) as store:
            report = store.apply(result, now.isoformat())
    return result, report
