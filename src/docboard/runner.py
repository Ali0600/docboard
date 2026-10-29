"""One sync at a time: collect a project's sources, then write them to the store."""

from __future__ import annotations

import os
import shlex
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from docboard import paths
from docboard.config import Config, Project
from docboard.store import ApplyReport, FloorError, Store
from docboard.sync import SyncResult, collect

MESSAGE_CAP = 300


if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class Busy(RuntimeError):
    pass


def _try_lock(fd: int) -> bool:
    """Takes the lock without waiting; False when another process holds it. The operating
    system frees it when the holder exits, however it exits."""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # byte 0; locking past the end is allowed
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


class SyncFailed(RuntimeError):
    """A sync crashed; its row in sync_run says why, and nothing else of it was stored."""


def describe(e: BaseException, project: Project) -> str:
    """One line for the page: the error's type and first line, with the project's path shown
    as its name and the home folder as ~ (never a traceback)."""
    text = (str(e).strip().splitlines() or [""])[0]
    for path in {str(project.path.resolve()), str(project.path)}:
        text = text.replace(path, project.name)
    text = text.replace(str(Path.home()), "~")
    line = f"{type(e).__name__}: {text}" if text else type(e).__name__
    return line[:MESSAGE_CAP]


def accept_command(project: str) -> str:
    """What the owner runs to accept a drop the floor refused."""
    return paths.command(f"sync --accept-drop --project {shlex.quote(project)}")


@contextmanager
def sync_lock(lock_path: Path, wait: float) -> Iterator[None]:
    """A machine-wide lock on a fixed path, released only by the holder that took it."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait
    try:
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise Busy("another sync is running")
            time.sleep(0.2)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def run_sync(cfg: Config, project: Project, db_path: Path, wait: float = 60.0,
             now: datetime | None = None,
             accept_drop: bool = False) -> tuple[SyncResult, ApplyReport]:  # fmt: skip
    """Collects and stores one project's sync under the machine-wide lock.

    A refused sync raises FloorError (its row is written by the store); any other failure is
    written as a 'failed' row and raised as SyncFailed, so the page shows it and does not
    restart it on every view. Busy (another sync holds the lock) writes nothing.
    """
    now = now or datetime.now(UTC)
    with sync_lock(db_path.parent / "sync.lock", wait):
        result: SyncResult | None = None
        try:
            result = collect(cfg, project, now)
            with Store(db_path) as store:
                store.backup_if_due(now)
                report = store.apply(result, now.isoformat(), accept_drop=accept_drop)
        except FloorError:
            raise
        except Exception as e:
            message = describe(e, project)
            try:
                with Store(db_path) as store:
                    store.record_failed(project.name, now.isoformat(), message, result)
            except Exception as again:
                e.add_note(f"recording the failure failed too: {describe(again, project)}")
                raise
            raise SyncFailed(message) from e
    return result, report
