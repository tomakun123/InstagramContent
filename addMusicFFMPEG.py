import subprocess
from pathlib import Path

voice = Path("voiceOutput.mp3")
music = Path("musicOutput.mp3")
out = Path("finalOutput.mp3")

# Lower music volume, loop it to match voice length, then mix
cmd = [
    "ffmpeg", "-y",
    "-i", str(voice),
    "-stream_loop", "-1", "-i", str(music),
    "-filter_complex",
    "[1:a]volume=0.5,aloop=loop=-1:size=2e+09[a1];"
    "[a1]atrim=0:999999,asetpts=N/SR/TB[a2];"
    "[0:a][a2]amix=inputs=2:duration=first:dropout_transition=2",
    "-c:a", "libmp3lame",
    "-q:a", "2",
    str(out),
]

subprocess.run(cmd, check=True)
print("Wrote:", out)
