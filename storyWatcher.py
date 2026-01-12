from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pathlib import Path
import time
import subprocess

# CHANGE THIS to your actual folder
WATCH_DIR = Path(r"C:\Users\Thomas M\Desktop\InstagramContent\Metadata")

def to_str_path(p) -> str:
    # watchdog may provide str, bytes, or other buffer types depending on platform/stubs
    if isinstance(p, str):
        return p
    if isinstance(p, (bytes, bytearray, memoryview)):
        b = bytes(p)  # converts bytearray/memoryview -> bytes
        return b.decode("mbcs", errors="ignore")  # Windows filesystem encoding
    return str(p)  # last resort

class StoryHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return

        src = to_str_path(event.src_path)
        path = Path(src)

        # Only react to .txt files
        if path.suffix.lower() != ".txt":
            return

        print("hello")
        subprocess.run(["python", "generateContent.py", str(path)], check=True)


if __name__ == "__main__":
    print("Watching folder:", WATCH_DIR)
    observer = Observer()
    observer.schedule(StoryHandler(), str(WATCH_DIR), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()
