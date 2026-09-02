#!/usr/bin/env python3
"""One-time Spotify OAuth. Exchanges the code in the callback handler so a
kept-open browser tab cannot hang shutdown() before the token is saved.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
REDIRECT = "http://127.0.0.1:8080/callback"
SCOPES = (
    "playlist-read-private playlist-read-collaborative user-library-read "
    "playlist-modify-private playlist-modify-public user-read-recently-played "
    "user-read-currently-playing user-read-playback-state "
    "user-read-private"
)


def load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'").strip('"')
    return out


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines: list[str] = []
    seen: set[str] = set()
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.strip().startswith("#"):
                k = raw.split("=", 1)[0].strip()
                if k in updates:
                    lines.append(f"{k}={updates[k]}")
                    seen.add(k)
                    continue
            lines.append(raw)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> int:
    env = load_dotenv(ENV_PATH)
    cid = env.get("SPOTIFY_CLIENT_ID") or os.environ.get("SPOTIFY_CLIENT_ID")
    secret = env.get("SPOTIFY_CLIENT_SECRET") or os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        print("Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env", file=sys.stderr)
        return 1

    box: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: ARG002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("error"):
                box["error"] = qs["error"][0]
                body = b"Authorization denied. You can close this tab."
            else:
                code = (qs.get("code") or [""])[0]
                box["code"] = code
                try:
                    resp = requests.post(
                        "https://accounts.spotify.com/api/token",
                        data={
                            "grant_type": "authorization_code",
                            "code": code,
                            "redirect_uri": REDIRECT,
                        },
                        auth=(cid, secret),
                        timeout=30,
                    )
                    if resp.status_code >= 400:
                        box["error"] = f"token {resp.status_code}"
                        body = b"Token exchange failed. You can close this tab."
                    else:
                        data = resp.json()
                        updates = {}
                        if data.get("refresh_token"):
                            updates["SPOTIFY_REFRESH_TOKEN"] = data["refresh_token"]
                        if data.get("access_token"):
                            updates["SPOTIFY_ACCESS_TOKEN"] = data["access_token"]
                        if updates:
                            upsert_env(ENV_PATH, updates)
                            box["saved"] = "1"
                            print("REFRESH_TOKEN_SAVED", flush=True)
                            body = b"Spotify authorized. You can close this tab."
                        else:
                            box["error"] = "empty token response"
                            body = b"Token exchange returned nothing. You can close this tab."
                except Exception as exc:
                    box["error"] = str(exc)
                    body = b"Token exchange error. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    httpd = HTTPServer(("127.0.0.1", 8080), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    params = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": SCOPES,
        "show_dialog": "true",
    }
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)
    Path("/tmp/spotify-auth-url.txt").write_text(url, encoding="utf-8")
    print("AUTH_URL_WRITTEN /tmp/spotify-auth-url.txt", flush=True)
    print("Listening on", REDIRECT, flush=True)

    deadline = time.time() + 600
    while time.time() < deadline and "saved" not in box and "error" not in box:
        time.sleep(0.25)
    threading.Thread(target=httpd.shutdown, daemon=True).start()
    time.sleep(0.4)

    if "saved" in box:
        return 0
    if "error" in box:
        print("oauth error:", box["error"], file=sys.stderr)
        return 2
    print("timed out waiting for callback", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
