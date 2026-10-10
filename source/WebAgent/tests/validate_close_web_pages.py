from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_core.close_web_pages import close_all_web_pages


class _Handler(BaseHTTPRequestHandler):
    closed: list[str] = []

    def do_GET(self):
        if self.path == "/json/list":
            body = json.dumps([
                {"id": "page-1", "type": "page"},
                {"id": "worker-1", "type": "service_worker"},
                {"id": "page-2", "type": "webview"},
            ]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/json/close/"):
            self.closed.append(self.path.rsplit("/", 1)[-1])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Target is closing")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        return


def main() -> int:
    _Handler.closed = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        report = close_all_web_pages(endpoints=[endpoint])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    assert report["status"] == "PASS", report
    assert report["closed_page_count"] == 2, report
    assert sorted(_Handler.closed) == ["page-1", "page-2"], _Handler.closed
    print(json.dumps({"close_web_pages": "PASS", "closed": 2}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
