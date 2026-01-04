from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pathlib import Path
import time
import subprocess

# CHANGE THIS to your actual folder
WATCH_DIR = Path(r"C:\Users\Thomas M\Desktop\InstagramContent\HorrorStories")

class StoryHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return

        path = Path(event.src_path)

        # Only react to .txt files
        if path.suffix.lower() == ".txt":
            print("hello")

        # RUNS TEXT TO SPEECH SCRIPT
        subprocess.run(
            ["python", "textToSpeech.py", str(path)],
            check=True
        )

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
