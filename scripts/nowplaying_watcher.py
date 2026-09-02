import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

STATE = Path('/workspace/spotify-weekly-mix/state/nowplaying.jsonl')

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
