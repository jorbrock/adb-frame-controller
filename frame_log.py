"""Persistent controller results, read newest first without loading the whole log."""
import json
from itertools import islice
import os
import shutil


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
