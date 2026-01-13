from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pathlib import Path
import time
import subprocess

WATCH_DIR = Path(r"C:\Users\Thomas M\Desktop\InstagramContent\Metadata")
PYTHON_EXE = "py"  # safer than "python" on Windows

SEEN = set()

def to_str_path(p) -> str:
    if isinstance(p, str):
        return p
    if isinstance(p, (bytes, bytearray, memoryview)):
        return bytes(p).decode("mbcs", errors="ignore")
    return str(p)

def wait_until_file_ready(path: Path, timeout=15.0, poll=0.2) -> bool:
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
        src = to_str_path(src_path)
        path = Path(src)

        # Only react to .json files
        if path.suffix.lower() != ".json":
            return

        key = str(path).lower()
        if key in SEEN:
            return
        SEEN.add(key)

        if not wait_until_file_ready(path):
            print(f"[watchdog] File not ready: {path}")
            return

        print(f"[watchdog] Running generateContent.py for: {path}")
        subprocess.run(
            [PYTHON_EXE, "-3", "generateContent.py", str(path)],
            check=True
        )

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
    print("Watching folder:", WATCH_DIR.resolve())
    print("Exists:", WATCH_DIR.exists())

    observer = Observer()
    observer.schedule(StoryHandler(), str(WATCH_DIR), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()
