"""Watches the Metadata directory and renders a video whenever n8n drops a job.

The metadata JSON written by the ContentGenerate workflow is the handoff signal:
once it lands (and stops growing), this launches generateContent.py for the story
number currently in counter.txt.
"""
import subprocess
import sys
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import paths

# Use the same interpreter that is running the watcher, so the subprocess is
# guaranteed to see the same virtualenv and installed dependencies.
PYTHON_EXE = sys.executable

SEEN = set()


def to_str_path(p) -> str:
    if isinstance(p, str):
        return p
    if isinstance(p, (bytes, bytearray, memoryview)):
        return bytes(p).decode("mbcs", errors="ignore")
    return str(p)


def wait_until_file_ready(path: Path, timeout=15.0, poll=0.2) -> bool:
    """Block until the file's size stops changing, so we never read a partial write."""
    start = time.time()
    last_size = -1

    while time.time() - start < timeout:
        if path.exists():
            try:
                size = path.stat().st_size
            except OSError:
                size = -1

            if size == last_size and size > 0:
                return True

            last_size = size

        time.sleep(poll)

    return path.exists()


class StoryHandler(FileSystemEventHandler):
    def _handle(self, src_path):
        path = Path(to_str_path(src_path))

        # Only react to .json files
        if path.suffix.lower() != ".json":
            return

        key = str(path).lower()
        if key in SEEN:
            return
        SEEN.add(key)

        if not wait_until_file_ready(path):
            print(f"[watchdog] File not ready: {path}", flush=True)
            return

        print(f"[watchdog] Running generateContent.py for: {path.name}", flush=True)
        try:
            subprocess.run(
                [PYTHON_EXE, str(paths.GENERATE_CONTENT)],
                cwd=str(paths.ROOT),
                check=True,
            )
            print(f"[watchdog] Finished: {path.name}", flush=True)
        except subprocess.CalledProcessError as e:
            # Keep watching: one bad story should not take the watcher down.
            print(f"[watchdog] generateContent.py failed ({e.returncode}) for {path.name}", flush=True)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)


if __name__ == "__main__":
    paths.ensure_dirs()

    print("Watching folder:", paths.METADATA, flush=True)
    print("Python:", PYTHON_EXE, flush=True)

    observer = Observer()
    observer.schedule(StoryHandler(), str(paths.METADATA), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()
