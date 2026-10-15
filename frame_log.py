"""Persistent controller results, read newest first without loading the whole log."""
import json
from datetime import datetime, timedelta, timezone
from functools import wraps
import logging
import threading
from itertools import islice
import os
import shutil


LOG = logging.getLogger("frames")
WRITE_LOCK = threading.RLock()
RETENTION = timedelta(days=3)
PURGE_INTERVAL_SECONDS = 3600


def serialized_write(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with WRITE_LOCK:
            return function(*args, **kwargs)
    return wrapped


@serialized_write
def append(path, entry, previous=None):
    seed = previous if not path.exists() or path.stat().st_size == 0 else None
    # Separate an interrupted write from the next complete record.
    separator = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as existing:
            existing.seek(-1, os.SEEK_END)
            separator = existing.read(1) != b"\n"
    with path.open("a", encoding="utf-8") as stream:
        if separator:
            stream.write("\n")
        if seed:
            stream.write(json.dumps(seed) + "\n")
        stream.write(json.dumps(entry) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@serialized_write
def clear(path):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        # A blank line marks intentionally cleared history, preventing legacy seeding.
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@serialized_write
def copy(source, destination):
    temporary = destination.with_suffix(".tmp")
    if source.exists():
        shutil.copyfile(source, temporary)
    else:
        temporary.write_bytes(b"")
    os.replace(temporary, destination)


def entries(path):
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return
    with stream:
        position = stream.seek(0, os.SEEK_END)
        remainder = b""
        newest = True
        while position:
            size = min(position, 8192)
            position -= size
            stream.seek(position)
            lines = (stream.read(size) + remainder).split(b"\n")
            remainder = lines.pop(0)
            if newest:
                # A writer may still be appending the final line.
                lines.pop()
                newest = False
            for line in reversed(lines):
                if line:
                    try:
                        yield json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue  # An interrupted write must not hide older results.
        if remainder:
            try:
                yield json.loads(remainder)
            except (ValueError, UnicodeDecodeError):
                pass


def page(path, number, size=50):
    records = list(islice(entries(path), (number - 1) * size, number * size + 1))
    return records[:size], len(records) > size


@serialized_write
def purge(path, cutoff):
    """Atomically remove dated records older than cutoff, retaining file order."""
    if not path.exists():
        return
    temporary = path.with_suffix(".tmp")
    try:
        with path.open("rb") as source, temporary.open("wb") as destination:
            # Keep the cleared marker so append cannot re-seed expired status.
            destination.write(b"\n")
            for line in source:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    timestamp = datetime.fromisoformat(entry["updated_at"])
                    # Legacy timestamps without a timezone cannot be aged reliably.
                    expired = timestamp.tzinfo is not None and timestamp < cutoff
                except (ValueError, TypeError, KeyError, UnicodeDecodeError):
                    expired = False
                if not expired:
                    destination.write(line)
                    if not line.endswith(b"\n"):
                        destination.write(b"\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def purge_all(data, now=None):
    cutoff = (now or datetime.now(timezone.utc)) - RETENTION
    for path in data.glob("*.log.jsonl"):
        try:
            purge(path, cutoff)
        except OSError:
            LOG.exception("Cannot purge old frame log entries from %s", path.name)


def maintenance(data, stop):
    """Purge at startup and hourly, even with management/scheduling disabled."""
    while not stop.is_set():
        purge_all(data)
        if stop.wait(PURGE_INTERVAL_SECONDS):
            return
