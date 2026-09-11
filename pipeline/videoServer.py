"""Serves finished videos over HTTP so Instagram can fetch them.

The Instagram Graph API does not accept uploaded bytes: it takes a public
`video_url` and Meta's servers download the file themselves. This serves
HorrorVideos/ on 127.0.0.1:8090, and cloudflared exposes that as
VIDEO_PUBLIC_BASE (see docs/SETUP.md).

Only `/v/<VIDEO_URL_SECRET>/<name>.mp4` is served - no listing, no other
extensions, no path traversal - so the public hostname is not a browsable
archive. Meta's fetcher issues HEAD and Range requests, which
SimpleHTTPRequestHandler does not support for Range, so both are handled here.

Usage:  python pipeline/videoServer.py            (reads VIDEO_URL_SECRET from env)
Health: GET /healthz -> 200 "ok"
"""
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paths

HOST = "127.0.0.1"
PORT = int(os.environ.get("VIDEO_SERVER_PORT", "8090"))
SECRET = os.environ.get("VIDEO_URL_SECRET", "").strip()

# Only bare filenames, only .mp4 - anything with a slash or a dot-segment fails.
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.mp4$")


class VideoHandler(BaseHTTPRequestHandler):
    server_version = "VideoServer/1.0"

    def log_message(self, fmt, *args):
        # One line per request in the launcher's .out.log; ranges are noisy, so
        # only log the first byte of the range to keep it readable.
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()

    def _resolve(self):
        """Return the file path for this request, or None if it must 404."""
        prefix = f"/v/{SECRET}/"
        if not SECRET or not self.path.startswith(prefix):
            return None
        name = self.path[len(prefix):].split("?", 1)[0]
        if not NAME_RE.match(name):
            return None
        target = (paths.VIDEOS / name).resolve()
        if target.parent != paths.VIDEOS.resolve() or not target.is_file():
            return None
        return target

    def _send_headers(self, status, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _range(self, size):
        """Parse a single-range Range header into (start, end) or None."""
        header = self.headers.get("Range", "")
        m = re.match(r"^bytes=(\d*)-(\d*)$", header.strip())
        if not m:
            return None
        start, end = m.groups()
        if start == "" and end == "":
            return None
        if start == "":                       # suffix range: last N bytes
            n = min(int(end), size)
            return size - n, size - 1
        start = int(start)
        end = int(end) if end else size - 1
        if start >= size:
            return "unsatisfiable"
        return start, min(end, size - 1)

    def _serve(self, send_body):
        if self.path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return

        target = self._resolve()
        if target is None:
            self.send_error(404)
            return

        size = target.stat().st_size
        rng = self._range(size)
        if rng == "unsatisfiable":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return

        if rng is None:
            start, end, status = 0, size - 1, 200
            extra = {}
        else:
            start, end = rng
            status = 206
            extra = {"Content-Range": f"bytes {start}-{end}/{size}"}

        length = end - start + 1
        self._send_headers(status, length, extra)
        if not send_body:
            return

        with open(target, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    return   # the fetcher hung up; nothing to do
                remaining -= len(chunk)

    def do_GET(self):
        self._serve(send_body=True)

    def do_HEAD(self):
        self._serve(send_body=False)


def main() -> int:
    if not SECRET:
        print("[!] VIDEO_URL_SECRET is not set; refusing to serve. Add it to .env.")
        return 2
    paths.ensure_dirs()
    server = ThreadingHTTPServer((HOST, PORT), VideoHandler)
    print(f"[videoserver] serving {paths.VIDEOS} on http://{HOST}:{PORT}/v/<secret>/<name>.mp4")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
