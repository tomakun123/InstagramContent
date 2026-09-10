"""Watches the Metadata directory and renders a video whenever n8n drops a job.

The metadata JSON written by the ContentGenerate workflow is the handoff signal:
once it lands (and stops growing), this launches generateContent.py for the story
number named by that file.

Deliberately does NOT handle on_modified. watchdog's Windows flags include
FILE_NOTIFY_CHANGE_LAST_ACCESS, so merely *reading* a file in this directory
raises a modify event - and PublishingContent reads the metadata file on every
publish. Handling on_modified therefore closed a loop: render -> webhook ->
publish reads metadata -> modify event -> render again, roughly every 12
seconds, re-uploading the same story until something was killed.
"""
import re
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

# HorrorStory<N>_metadata.json - the filename is the authoritative story number.
STORY_RE = re.compile(r"HorrorStory(\d+)_metadata\.json$", re.IGNORECASE)

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

        match = STORY_RE.search(path.name)
        if not match:
            return
        story_number = int(match.group(1))

        key = str(path).lower()
        if key in SEEN:
            return
        SEEN.add(key)

        if not wait_until_file_ready(path):
            print(f"[watchdog] File not ready: {path}", flush=True)
            return

        # Already rendered since this metadata was written - nothing to do. Makes
        # a watcher restart harmless instead of a re-upload of the back catalogue.
        video = paths.VIDEOS / f"HorrorStory{story_number}.mp4"
        if video.exists() and video.stat().st_mtime >= path.stat().st_mtime:
            print(f"[watchdog] Skipping story {story_number}: {video.name} is already current",
                  flush=True)
            return

        print(f"[watchdog] Rendering story {story_number} (triggered by {path.name})", flush=True)
        try:
            # --story pins the render to the file that woke us rather than to
            # whatever counter.txt happens to say now. --notify is required
            # because generateContent defaults to not publishing when --story
            # is given, so that manual re-renders stay safe.
            subprocess.run(
                [PYTHON_EXE, str(paths.GENERATE_CONTENT),
                 "--story", str(story_number), "--notify"],
                cwd=str(paths.ROOT),
                check=True,
            )
            print(f"[watchdog] Finished story {story_number}", flush=True)
        except subprocess.CalledProcessError as e:
            # Keep watching: one bad story should not take the watcher down.
            print(f"[watchdog] generateContent.py failed ({e.returncode}) for story {story_number}",
                  flush=True)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)

    # No on_modified: see the module docstring. A read of this directory raises
    # one on Windows, which is what caused the publish/render loop.


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
