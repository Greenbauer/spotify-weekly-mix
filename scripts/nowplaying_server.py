from http.server import BaseHTTPRequestHandler, HTTPServer
import json, os

import os
from pathlib import Path

# Repo-relative so this works from any checkout. mix.py's ingest_ui reads
# <repo>/state/nowplaying.jsonl; a hardcoded absolute path meant the writer and
# the reader never pointed at the same file and ingest_ui always saw nothing.
STATE_PATH = Path(
    os.environ.get("MIX_STATE_DIR")
    or (Path(__file__).resolve().parent.parent / "state")
) / "nowplaying.jsonl"

PATH = str(STATE_PATH)
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        if self.path != '/event':
            self.send_response(404); self.end_headers(); return
        try:
            n=int(self.headers.get('Content-Length','0'))
            d=json.loads(self.rfile.read(n))
            title=str(d['title']); artists=str(d['artists']); ts=str(d['ts'])
            if '.' in ts:
                ts=ts[:ts.index('.')]+ 'Z'
            if not ts.endswith('Z'): ts += 'Z'
            key=(title,artists); last=None
            try:
                with open(PATH,'rb') as f:
                    for line in f:
                        pass
                    if line:
                        last=json.loads(line)
            except (FileNotFoundError, UnboundLocalError, json.JSONDecodeError): pass
            if not last or (last.get('title'),last.get('artists')) != key:
                with open(PATH,'a',encoding='utf-8') as f:
                    f.write(json.dumps({'ts':ts,'title':title,'artists':artists},ensure_ascii=False,separators=(',',':'))+'\n')
            self.send_response(204); self.end_headers()
        except Exception:
            self.send_response(400); self.end_headers()
HTTPServer(('127.0.0.1',8765),Handler).serve_forever()
