import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import os
from pathlib import Path

# Repo-relative so this works from any checkout. mix.py's ingest_ui reads
# <repo>/state/nowplaying.jsonl; a hardcoded absolute path meant the writer and
# the reader never pointed at the same file and ingest_ui always saw nothing.
STATE_PATH = Path(
    os.environ.get("MIX_STATE_DIR")
    or (Path(__file__).resolve().parent.parent / "state")
) / "nowplaying.jsonl"

STATE = STATE_PATH
STATE.parent.mkdir(parents=True, exist_ok=True)

def last_key():
    try:
        lines = STATE.read_text().splitlines()
        if lines:
            d = json.loads(lines[-1])
            return (d.get('title'), d.get('artists'))
    except Exception:
        pass
    return None

last = last_key()

class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    def do_POST(self):
        global last
        try:
            n = int(self.headers.get('Content-Length', '0'))
            d = json.loads(self.rfile.read(n))
            title = str(d.get('title', ''))
            artists = str(d.get('artists', ''))
            key = (title, artists)
            if title and artists and key != last:
                rec = {'ts': datetime.now(timezone.utc).isoformat().replace('+00:00','Z'), 'title': title, 'artists': artists}
                with STATE.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(',', ':')) + '\n')
                last = key
            self.send_response(204)
        except Exception:
            self.send_response(400)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
    def log_message(self, *_):
        pass

HTTPServer(('127.0.0.1', 8765), Handler).serve_forever()
