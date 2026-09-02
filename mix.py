#!/usr/bin/env python3
"""Weekly Spotify discovery mixer.

Seeds taste from playlists YOU created (owner == you). Liked Songs are an
optional artist/track seed only — never the output pool.

Never used as seeds:
  - GET /me/top (listening-history taste profile)
  - recently-played
  - baby / kids / nursery playlists
  - out-of-season holiday playlists
  - the Weekly Mix playlist itself

Discovery (Spotify /recommendations and /related-artists are 403 for new apps
since 2024-11-27; Dev Mode also lost /artists/{id}/top-tracks and the
popularity field in Feb 2026):

  created-playlist artists
    → Last.fm artist.getSimilar  (or ListenBrainz similar-artists)
    → those artists' popular tracks (Last.fm top-tracks, or Spotify search)
    → drop anything already in created playlists, likes, or the play log
    → keep ~40, biased to popular, max N per artist

Commands: build_mix | publish | log_plays | probe | self_test

This script does not register a Spotify app and does not start OAuth.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = ROOT / "state"
SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_ACCOUNTS = "https://accounts.spotify.com/api/token"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
MB_API = "https://musicbrainz.org/ws/2"
LB_SIMILAR = "https://labs.api.listenbrainz.org/similar-artists/json"
LB_SIMILAR_ALGO = "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
UA = "spotify-weekly-mix/1.0 (personal discovery mixer)"

SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-read-recently-played",
    "user-read-currently-playing",
    "user-read-playback-state",
    "user-read-private",
]

# ---------------------------------------------------------------------------
# Seasonal + skip-name rules
# ---------------------------------------------------------------------------

# (name regex, predicate(date) -> in-season)
SEASONAL_RULES: list[tuple[re.Pattern[str], Any]] = [
    (
        re.compile(r"christmas|x-?mas|\bholiday\b|hanukkah|kwanzaa|nye|\bnew\s*year", re.I),
        lambda d: d.month == 12,
    ),
    (
        re.compile(r"halloween|\bspooky\b", re.I),
        lambda d: d.month == 10,
    ),
    (
        re.compile(r"thanksgiving|\bturkey\b", re.I),
        lambda d: d.month == 11 and d.day >= 15,
    ),
    (
        re.compile(
            r"4th of july|fourth of july|independence day|\bjuly\s*4\b",
            re.I,
        ),
        lambda d: (d.month == 6 and d.day >= 25) or (d.month == 7 and d.day <= 10),
    ),
    (
        re.compile(r"valentine", re.I),
        lambda d: d.month == 2,
    ),
]

# Baby / kids playlists must never seed. "House listening" is listening-history
# pollution (we never seed from recently-played / top-items); also skip a
# playlist literally named that way if one exists.
SKIP_SEED_NAME_RE = re.compile(
    r"""
    \b(
        baby|babies|infant|nursery|lullab(?:y|ies)|
        kids?|children|toddler|
        nap(?:time)?|
        house\s+listening
    )\b
    """,
    re.I | re.X,
)


def seasonal_reason(name: str, today: date) -> str | None:
    for rx, in_season in SEASONAL_RULES:
        if rx.search(name) and not in_season(today):
            return f"seasonal (out of season): {name!r}"
    return None


def skip_seed_reason(name: str, today: date, mix_name: str) -> str | None:
    if name.strip().lower() == mix_name.strip().lower():
        return f"output playlist: {name!r}"
    if SKIP_SEED_NAME_RE.search(name):
        return f"baby/house playlist: {name!r}"
    return seasonal_reason(name, today)


# ---------------------------------------------------------------------------
# Tiny .env + JSON state
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'").strip('"')
        os.environ.setdefault(key, val)


def env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


def env_int(name: str, default: int) -> int:
    raw = env(name)
    return default if raw is None else int(raw)


def env_bool(name: str, default: bool) -> bool:
    raw = env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass
class Paths:
    root: Path
    state: Path

    @property
    def config(self) -> Path:
        return self.state / "config.json"

    @property
    def last_mix(self) -> Path:
        return self.state / "last_mix.json"

    @property
    def played(self) -> Path:
        return self.state / "played.json"

    @property
    def similar_cache(self) -> Path:
        return self.state / "similar_cache.json"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class Http:
    def __init__(self, user_agent: str = UA, min_interval: float = 0.0) -> None:
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent
        self.min_interval = min_interval
        self._last = 0.0

    def request(
        self,
        method: str,
        url: str,
        *,
        retries: int = 5,
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        last_exc: Exception | None = None
        for attempt in range(retries):
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            try:
                resp = self.s.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(min(8.0, 0.5 * 2**attempt))
                continue
            self._last = time.monotonic()
            if resp.status_code == 429:
                wait = resp.headers.get("Retry-After")
                wait_s = float(wait) if wait else min(16.0, 1.0 * 2**attempt)
                # Spotify Dev Mode can return Retry-After of many hours for
                # QUOTA_EXCEEDED. Sleeping that long wedges watch_plays; return
                # the 429 so callers can degrade (e.g. skip recently-played).
                if wait_s > 60:
                    return resp
                time.sleep(wait_s)
                continue
            if resp.status_code in {500, 502, 503, 504}:
                time.sleep(min(8.0, 0.5 * 2**attempt))
                continue
            return resp
        if last_exc:
            raise last_exc
        raise RuntimeError(f"HTTP {method} {url} failed after retries")


# ---------------------------------------------------------------------------
# Spotify client (Dev Mode 2026 + Extended Quota dual-path)
# ---------------------------------------------------------------------------

@dataclass
class SpotifyCaps:
    """What this app's token can still do. Filled by probe()."""

    related_artists: bool | None = None
    recommendations: bool | None = None
    artist_top_tracks: bool | None = None
    popularity_field: bool | None = None
    playlist_items_path: str = "items"  # or "tracks"
    create_playlist_path: str = "me"  # or "users"


class Spotify:
    def __init__(self, access_token: str) -> None:
        self.http = Http()
        self.http.s.headers["Authorization"] = f"Bearer {access_token}"
        self.caps = SpotifyCaps()

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self.http.request("GET", f"{SPOTIFY_API}{path}", params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"GET {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    def _send(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        return self.http.request(method, f"{SPOTIFY_API}{path}", **kwargs)

    def get_ok(self, path: str, params: dict | None = None) -> tuple[int, Any]:
        resp = self.http.request("GET", f"{SPOTIFY_API}{path}", params=params)
        body: Any = None
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:300]
        return resp.status_code, body

    def me(self) -> dict:
        return self._get("/me")

    def paginate(self, path: str, params: dict | None = None, key: str = "items") -> list:
        params = dict(params or {})
        params.setdefault("limit", 50)
        out: list = []
        url_path = path
        query = params
        while True:
            data = self._get(url_path, query)
            out.extend(data.get(key) or [])
            nxt = data.get("next")
            if not nxt:
                break
            # next is an absolute URL; strip the API prefix
            if nxt.startswith(SPOTIFY_API):
                url_path = nxt[len(SPOTIFY_API) :]
                query = None
            else:
                break
        return out

    def created_playlists(self, user_id: str) -> list[dict]:
        items = self.paginate("/me/playlists", {"limit": 50})
        mine = []
        for pl in items:
            owner = (pl.get("owner") or {}).get("id")
            if owner == user_id and pl.get("id"):
                mine.append(pl)
        return mine

    def playlist_items(self, playlist_id: str) -> list[dict]:
        """Return track objects. Tries /items (Dev Mode 2026) then /tracks."""
        tracks: list[dict] = []
        for suffix in (self.caps.playlist_items_path, "items", "tracks"):
            code, body = self.get_ok(
                f"/playlists/{playlist_id}/{suffix}",
                {"limit": 50},
            )
            if code == 404:
                continue
            if code >= 400:
                continue
            self.caps.playlist_items_path = suffix
            items = list(body.get("items") or [])
            nxt = body.get("next")
            while nxt and nxt.startswith(SPOTIFY_API):
                rel = nxt[len(SPOTIFY_API) :]
                data = self._get(rel)
                items.extend(data.get("items") or [])
                nxt = data.get("next")
            for raw in items:
                t = _playlist_item_track(raw)
                if t:
                    tracks.append(t)
            return tracks
        return tracks

    def liked_tracks(self) -> list[dict]:
        rows = self.paginate("/me/tracks", {"limit": 50})
        out = []
        for row in rows:
            t = row.get("track") or row.get("item")
            if t and t.get("id") and not t.get("is_local"):
                out.append(t)
        return out

    def search_tracks(self, q: str, limit: int = 5) -> list[dict]:
        limit = max(1, min(limit, 10))  # Dev Mode max is 10
        code, body = self.get_ok("/search", {"q": q, "type": "track", "limit": limit})
        if code >= 400:
            return []
        return list(((body or {}).get("tracks") or {}).get("items") or [])

    def search_artist(self, name: str) -> dict | None:
        code, body = self.get_ok("/search", {"q": f'artist:"{name}"', "type": "artist", "limit": 5})
        if code >= 400:
            return None
        items = list(((body or {}).get("artists") or {}).get("items") or [])
        if not items:
            code, body = self.get_ok("/search", {"q": name, "type": "artist", "limit": 5})
            items = list(((body or {}).get("artists") or {}).get("items") or [])
        lowered = name.strip().lower()
        for a in items:
            if (a.get("name") or "").strip().lower() == lowered:
                return a
        return items[0] if items else None

    def related_artists(self, artist_id: str) -> list[dict]:
        code, body = self.get_ok(f"/artists/{artist_id}/related-artists")
        if code >= 400:
            self.caps.related_artists = False
            return []
        self.caps.related_artists = True
        return list((body or {}).get("artists") or [])

    def artist_top_tracks(self, artist_id: str, market: str = "US") -> list[dict]:
        code, body = self.get_ok(f"/artists/{artist_id}/top-tracks", {"market": market})
        if code >= 400:
            self.caps.artist_top_tracks = False
            return []
        self.caps.artist_top_tracks = True
        return list((body or {}).get("tracks") or [])

    def recently_played(self, limit: int = 50) -> list[dict]:
        # recently-played max limit is 50; we do not paginate far — Spotify
        # only retains a short window. Call log_plays often.
        code, body = self.get_ok("/me/player/recently-played", {"limit": min(limit, 50)})
        if code == 429:
            # Quota exceeded — degrade so currently-playing skip logging still works.
            return []
        if code >= 400:
            raise RuntimeError(f"recently-played -> {code}: {body}")
        return list((body or {}).get("items") or [])

    def currently_playing(self) -> dict | None:
        """Now-playing, including tracks skipped after a few seconds.

        204 = nothing playing. 401 = token needs refresh.
        """
        code, body = self.get_ok(
            "/me/player/currently-playing",
            {"additional_types": "track"},
        )
        if code == 401:
            raise RuntimeError("currently-playing -> 401")
        if code in {204, 202} or not body:
            return None
        if code >= 400:
            return None
        return body if isinstance(body, dict) else None

    def create_playlist(self, user_id: str, name: str, description: str, public: bool = False) -> dict:
        payload = {"name": name, "description": description, "public": public}
        resp = self._send("POST", "/me/playlists", json=payload)
        if resp.status_code >= 400:
            resp = self._send("POST", f"/users/{user_id}/playlists", json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"create playlist -> {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    def change_playlist(self, playlist_id: str, **fields: Any) -> None:
        resp = self._send("PUT", f"/playlists/{playlist_id}", json=fields)
        if resp.status_code >= 400:
            raise RuntimeError(f"change playlist -> {resp.status_code}: {resp.text[:400]}")

    def replace_playlist_tracks(self, playlist_id: str, uris: list[str]) -> None:
        body = {"uris": uris}
        for suffix in (self.caps.playlist_items_path, "items", "tracks"):
            resp = self._send("PUT", f"/playlists/{playlist_id}/{suffix}", json=body)
            if resp.status_code < 400:
                self.caps.playlist_items_path = suffix
                return
        raise RuntimeError(f"replace playlist items failed for {playlist_id}")

    def probe(self, sample_artist_id: str | None = None) -> SpotifyCaps:
        """Hit a few endpoints once to record what this app can still call."""
        # Taylor Swift as a well-known catalog id if we have no seed yet.
        aid = sample_artist_id or "06HL4z0CvFAxyc27GXpf02"
        code, body = self.get_ok(f"/artists/{aid}/related-artists")
        self.caps.related_artists = code < 400
        code, _ = self.get_ok(
            "/recommendations",
            {"seed_artists": aid, "limit": 1},
        )
        self.caps.recommendations = code < 400
        code, top = self.get_ok(f"/artists/{aid}/top-tracks", {"market": "US"})
        self.caps.artist_top_tracks = code < 400
        pop = None
        if isinstance(top, dict):
            tracks = top.get("tracks") or []
            if tracks:
                pop = tracks[0].get("popularity")
        if pop is None:
            code, tr = self.get_ok("/search", {"q": "track:believe artist:cher", "type": "track", "limit": 1})
            if code < 400:
                items = ((tr or {}).get("tracks") or {}).get("items") or []
                if items:
                    pop = items[0].get("popularity")
        self.caps.popularity_field = pop is not None
        return self.caps


def _playlist_item_track(raw: dict) -> dict | None:
    """Handle classic {track: ...} and Feb 2026 {item: ...} shapes."""
    obj = raw.get("item") or raw.get("track")
    if obj is None:
        return None
    if obj.get("type") == "episode":
        return None
    if obj.get("type") in {None, "track"} and obj.get("id"):
        if obj.get("is_local"):
            return None
        return obj
    nested = obj.get("track")
    if isinstance(nested, dict) and nested.get("id") and not nested.get("is_local"):
        return nested
    return None


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(
        SPOTIFY_ACCOUNTS,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(client_id, client_secret),
        headers={"User-Agent": UA},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"token refresh -> {resp.status_code}: {resp.text[:400]}")
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("token refresh returned no access_token")
    return token


def authorize_url(client_id: str, redirect_uri: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
    }
    return "https://accounts.spotify.com/authorize?" + urlencode(params)


# ---------------------------------------------------------------------------
# Last.fm / MusicBrainz / ListenBrainz  (related-artists replacement)
# ---------------------------------------------------------------------------

class LastFm:
    def __init__(self, api_key: str) -> None:
        self.key = api_key
        self.http = Http(min_interval=0.22)

    def _call(self, method: str, **params: Any) -> dict:
        q = {"method": method, "api_key": self.key, "format": "json", **params}
        resp = self.http.request("GET", LASTFM_API, params=q)
        if resp.status_code >= 400:
            return {}
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            return {}
        return data if isinstance(data, dict) else {}

    def similar_artists(self, artist: str, limit: int = 12) -> list[tuple[str, float]]:
        data = self._call("artist.getSimilar", artist=artist, limit=str(limit), autocorrect="1")
        rows = ((data.get("similarartists") or {}).get("artist")) or []
        if isinstance(rows, dict):
            rows = [rows]
        out: list[tuple[str, float]] = []
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            try:
                match = float(row.get("match") or 0)
            except (TypeError, ValueError):
                match = 0.0
            out.append((name, match))
        return out

    def top_tracks(self, artist: str, limit: int = 10) -> list[dict]:
        data = self._call("artist.getTopTracks", artist=artist, limit=str(limit), autocorrect="1")
        rows = ((data.get("toptracks") or {}).get("track")) or []
        if isinstance(rows, dict):
            rows = [rows]
        out = []
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            try:
                listeners = int(row.get("listeners") or 0)
            except (TypeError, ValueError):
                listeners = 0
            try:
                playcount = int(row.get("playcount") or 0)
            except (TypeError, ValueError):
                playcount = 0
            out.append(
                {
                    "name": name,
                    "artist": ((row.get("artist") or {}).get("name")) or artist,
                    "listeners": listeners,
                    "playcount": playcount,
                }
            )
        return out


class MusicBrainz:
    def __init__(self) -> None:
        self.http = Http(min_interval=1.05)  # MusicBrainz asks for ~1 req/s

    def artist_mbid(self, name: str) -> str | None:
        resp = self.http.request(
            "GET",
            f"{MB_API}/artist/",
            params={"query": f'artist:"{name}"', "fmt": "json", "limit": 1},
        )
        if resp.status_code >= 400:
            return None
        artists = (resp.json() or {}).get("artists") or []
        if not artists:
            return None
        return artists[0].get("id")


class ListenBrainz:
    def __init__(self) -> None:
        self.http = Http(min_interval=0.15)

    def similar_artists(self, mbid: str, limit: int = 12) -> list[tuple[str, float]]:
        resp = self.http.request("GET", LB_SIMILAR, params={"artist_mbids": mbid, "algorithm": LB_SIMILAR_ALGO})
        if resp.status_code >= 400:
            return []
        try:
            payload = resp.json()
        except Exception:
            return []
        rows: list[dict] = []
        if isinstance(payload, list):
            for block in payload:
                if isinstance(block, list):
                    rows.extend(x for x in block if isinstance(x, dict))
                elif isinstance(block, dict):
                    rows.append(block)
        out: list[tuple[str, float]] = []
        seen: set[str] = set()
        for row in rows:
            name = (
                row.get("artist_credit_name")
                or row.get("artist_name")
                or row.get("name")
                or ""
            ).strip()
            other = (row.get("artist_mbid") or "").lower()
            if not name or other == mbid.lower() or name.lower() in seen:
                continue
            seen.add(name.lower())
            try:
                score = float(row.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            out.append((name, score))
        out.sort(key=lambda x: x[1], reverse=True)
        return out[:limit]

def lastfm_listeners_to_popularity(listeners: int) -> int:
    """Map Last.fm listener counts onto roughly Spotify's 0–100 popularity scale.

    15 * log10(n+1):  ~3.2k listeners ≈ 55, 100k ≈ 75, 1M ≈ 90.
    """
    import math

    if listeners <= 0:
        return 0
    return int(min(100, round(15.0 * math.log10(listeners + 1))))


# ---------------------------------------------------------------------------
# Mix builder
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    track_id: str
    uri: str
    name: str
    artists: list[str]
    artist_ids: list[str]
    popularity: int
    source: str


@dataclass
class MixConfig:
    playlist_name: str = "Weekly Mix"
    size: int = 40
    min_popularity: int = 55
    max_per_artist: int = 2
    use_likes: bool = True
    today: date = field(default_factory=lambda: date.today())


def artist_names(track: dict) -> list[str]:
    return [a.get("name") for a in (track.get("artists") or []) if a.get("name")]


def artist_ids(track: dict) -> list[str]:
    return [a.get("id") for a in (track.get("artists") or []) if a.get("id")]


def track_uri(track: dict) -> str:
    return track.get("uri") or f"spotify:track:{track['id']}"


class Mixer:
    def __init__(self, sp: Spotify, paths: Paths, cfg: MixConfig, lastfm: LastFm | None) -> None:
        self.sp = sp
        self.paths = paths
        self.cfg = cfg
        self.lastfm = lastfm
        self.mb = MusicBrainz()
        self.lb = ListenBrainz()
        self._similar_cache: dict[str, Any] = read_json(paths.similar_cache, {})
        self._artist_id_cache: dict[str, str] = {}

    def _save_similar_cache(self) -> None:
        write_json(self.paths.similar_cache, self._similar_cache)

    def played_ids(self) -> set[str]:
        data = read_json(self.paths.played, {"plays": []})
        ids = {p.get("track_id") for p in data.get("plays") or [] if p.get("track_id")}
        # last_mix tracks also count as "already offered" until they age out
        # via log_plays; we still exclude them so republish doesn't reshuffle
        # the same 40 back in next week if they were never played. The play
        # log is the long-term exclude; last_mix is the previous batch.
        # Only songs actually heard (including short skips logged by
        # watch_plays). Unheard last-mix tracks may return next week.
        return ids

    def collect_library(self, user_id: str) -> tuple[list[dict], set[str], Counter]:
        """Created playlists → seed artists + exclude track ids.

        Liked songs join the exclude set; optionally their artists join seeds.
        """
        mix_name = self.cfg.playlist_name
        today = self.cfg.today
        created = self.sp.created_playlists(user_id)
        seed_playlists = []
        skipped = []
        for pl in created:
            reason = skip_seed_reason(pl.get("name") or "", today, mix_name)
            if reason:
                skipped.append(reason)
                continue
            seed_playlists.append(pl)

        print(f"created playlists: {len(created)}")
        print(f"seed playlists:    {len(seed_playlists)}")
        for reason in skipped:
            print(f"  skip seed: {reason}")

        exclude: set[str] = set()
        seeds: Counter = Counter()
        seed_artist_names: Counter = Counter()

        for pl in seed_playlists:
            tracks = self.sp.playlist_items(pl["id"])
            print(f"  {pl.get('name')!r}: {len(tracks)} tracks")
            for t in tracks:
                if t.get("id"):
                    exclude.add(t["id"])
                for a in t.get("artists") or []:
                    if a.get("id"):
                        seeds[a["id"]] += 1
                    if a.get("name"):
                        seed_artist_names[a["name"]] += 1

        if self.cfg.use_likes:
            likes = self.sp.liked_tracks()
            print(f"liked songs (exclude + artist seeds): {len(likes)}")
            for t in likes:
                if t.get("id"):
                    exclude.add(t["id"])
                for a in t.get("artists") or []:
                    if a.get("id"):
                        seeds[a["id"]] += 1
                    if a.get("name"):
                        seed_artist_names[a["name"]] += 1

        exclude |= self.played_ids()
        print(f"exclude track ids: {len(exclude)}")
        return seed_playlists, exclude, seed_artist_names

    def similar_for(self, artist_name: str, limit: int = 10) -> list[str]:
        key = artist_name.strip().lower()
        cached = self._similar_cache.get(key)
        if isinstance(cached, dict) and cached.get("names"):
            ts = cached.get("ts") or 0
            if time.time() - ts < 14 * 86400:
                return list(cached["names"])[:limit]

        names: list[str] = []
        if self.lastfm:
            names = [n for n, _ in self.lastfm.similar_artists(artist_name, limit=limit)]
        if not names:
            mbid = self.mb.artist_mbid(artist_name)
            if mbid:
                names = [n for n, _ in self.lb.similar_artists(mbid, limit=limit)]
        # Spotify related-artists only if this app still has it (extended quota).
        if not names and self.sp.caps.related_artists is not False:
            sp_artist = self.sp.search_artist(artist_name)
            if sp_artist and sp_artist.get("id"):
                rel = self.sp.related_artists(sp_artist["id"])
                names = [a.get("name") for a in rel if a.get("name")][:limit]

        self._similar_cache[key] = {"ts": int(time.time()), "names": names}
        return names[:limit]

    def resolve_track(self, title: str, artist: str) -> dict | None:
        q = f'track:"{title}" artist:"{artist}"'
        hits = self.sp.search_tracks(q, limit=5)
        if not hits:
            hits = self.sp.search_tracks(f"{title} {artist}", limit=5)
        want_t, want_a = title.strip().lower(), artist.strip().lower()
        best = None
        for h in hits:
            hname = (h.get("name") or "").strip().lower()
            hans = [x.strip().lower() for x in artist_names(h)]
            if not h.get("id"):
                continue
            if hname == want_t and want_a in hans:
                return h
            if best is None and (want_t in hname or hname in want_t):
                best = h
        return best or (hits[0] if hits else None)

    def popular_tracks_for(self, artist_name: str, n: int = 8) -> list[Candidate]:
        """Top/popular tracks for an artist, resolved onto Spotify."""
        found: list[Candidate] = []

        # Path A: Last.fm ranking (best popularity proxy when Spotify
        # popularity is stripped in Dev Mode).
        if self.lastfm:
            for row in self.lastfm.top_tracks(artist_name, limit=n):
                pop = lastfm_listeners_to_popularity(row["listeners"])
                hit = self.resolve_track(row["name"], row["artist"])
                if not hit:
                    continue
                sp_pop = hit.get("popularity")
                if isinstance(sp_pop, int):
                    self.sp.caps.popularity_field = True
                    pop = sp_pop
                found.append(
                    Candidate(
                        track_id=hit["id"],
                        uri=track_uri(hit),
                        name=hit.get("name") or row["name"],
                        artists=artist_names(hit) or [artist_name],
                        artist_ids=artist_ids(hit),
                        popularity=pop,
                        source=f"lastfm-top:{artist_name}",
                    )
                )

        # Path B: Spotify artist top-tracks (extended quota / grandfathered).
        if len(found) < n and self.sp.caps.artist_top_tracks is not False:
            sp_artist = self.sp.search_artist(artist_name)
            if sp_artist and sp_artist.get("id"):
                for hit in self.sp.artist_top_tracks(sp_artist["id"]):
                    if not hit.get("id"):
                        continue
                    pop = hit.get("popularity")
                    if not isinstance(pop, int):
                        pop = 60  # endpoint exists ⇒ these ARE the popular ones
                    found.append(
                        Candidate(
                            track_id=hit["id"],
                            uri=track_uri(hit),
                            name=hit.get("name") or "",
                            artists=artist_names(hit) or [artist_name],
                            artist_ids=artist_ids(hit),
                            popularity=pop,
                            source=f"spotify-top:{artist_name}",
                        )
                    )

        # Path C: Spotify search ranking ≈ popularity (Dev Mode fallback).
        if len(found) < 3:
            hits = self.sp.search_tracks(f'artist:"{artist_name}"', limit=10)
            for i, hit in enumerate(hits):
                if not hit.get("id"):
                    continue
                pop = hit.get("popularity")
                if not isinstance(pop, int):
                    # Search is relevance-ranked; earlier hits ≈ more popular.
                    pop = max(40, 80 - 3 * i)
                found.append(
                    Candidate(
                        track_id=hit["id"],
                        uri=track_uri(hit),
                        name=hit.get("name") or "",
                        artists=artist_names(hit) or [artist_name],
                        artist_ids=artist_ids(hit),
                        popularity=pop,
                        source=f"spotify-search:{artist_name}",
                    )
                )

        # de-dupe preserving order
        seen: set[str] = set()
        uniq: list[Candidate] = []
        for c in found:
            if c.track_id in seen:
                continue
            seen.add(c.track_id)
            uniq.append(c)
        return uniq

    def build(self, user: dict) -> list[Candidate]:
        user_id = user["id"]
        _, exclude, seed_names = self.collect_library(user_id)
        if not seed_names:
            raise RuntimeError("no seed artists — create some playlists first")

        top_seeds = [name for name, _ in seed_names.most_common(40)]
        print(f"seed artists: {len(top_seeds)} (from {len(seed_names)} unique)")

        similar: Counter = Counter()
        for name in top_seeds:
            for rel in self.similar_for(name, limit=8):
                if rel.strip().lower() == name.strip().lower():
                    continue
                similar[rel] += 1
        self._save_similar_cache()

        # Prefer similar artists that showed up from several seeds.
        similar_ranked = [n for n, _ in similar.most_common(80)]
        print(f"similar artists: {len(similar_ranked)}")
        if not similar_ranked:
            print("warning: no similar artists found; falling back to seed artists' own unheard hits")
            similar_ranked = top_seeds[:]

        year, week, _ = self.cfg.today.isocalendar()
        rng = random.Random(f"{year}-W{week:02d}-{user_id}")

        pool: list[Candidate] = []
        seen_ids = set(exclude)
        for artist in similar_ranked:
            for cand in self.popular_tracks_for(artist, n=8):
                if cand.track_id in seen_ids:
                    continue
                if cand.popularity < self.cfg.min_popularity:
                    continue
                seen_ids.add(cand.track_id)
                pool.append(cand)
            if len(pool) >= self.cfg.size * 6:
                break

        print(f"candidate pool after filters: {len(pool)}")
        # popularity-weighted sample with per-artist cap
        pool.sort(key=lambda c: c.popularity, reverse=True)
        picked: list[Candidate] = []
        per_artist: Counter = Counter()

        def primary(c: Candidate) -> str:
            return (c.artist_ids[0] if c.artist_ids else (c.artists[0] if c.artists else c.track_id))

        remaining = pool[:]
        while remaining and len(picked) < self.cfg.size:
            weights = []
            eligible = []
            for c in remaining:
                a = primary(c)
                if per_artist[a] >= self.cfg.max_per_artist:
                    continue
                eligible.append(c)
                weights.append(max(1, c.popularity) ** 1.4)
            if not eligible:
                break
            choice = rng.choices(eligible, weights=weights, k=1)[0]
            picked.append(choice)
            per_artist[primary(choice)] += 1
            remaining = [c for c in remaining if c.track_id != choice.track_id]

        picked.sort(key=lambda c: c.popularity, reverse=True)
        return picked

    def persist_mix(self, tracks: list[Candidate], user_id: str) -> dict:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "week": self.cfg.today.isocalendar()[:2],
            "playlist_name": self.cfg.playlist_name,
            "rules": {
                "size": self.cfg.size,
                "min_popularity": self.cfg.min_popularity,
                "max_per_artist": self.cfg.max_per_artist,
                "use_likes_as_seeds": self.cfg.use_likes,
                "never_seed_from": [
                    "recently-played",
                    "GET /me/top",
                    "baby/kids playlists",
                    "out-of-season seasonal playlists",
                ],
            },
            "tracks": [
                {
                    "id": t.track_id,
                    "uri": t.uri,
                    "name": t.name,
                    "artists": t.artists,
                    "popularity": t.popularity,
                    "source": t.source,
                }
                for t in tracks
            ],
        }
        write_json(self.paths.last_mix, payload)
        return payload


def print_mix(tracks: list[Candidate]) -> None:
    print()
    print(f"{'#':>3}  {'pop':>3}  artists — title")
    print("-" * 72)
    for i, t in enumerate(tracks, 1):
        artists = ", ".join(t.artists) or "?"
        print(f"{i:3d}  {t.popularity:3d}  {artists} — {t.name}")
    print("-" * 72)
    print(f"{len(tracks)} tracks")


def load_client(require_token: bool = True, force_refresh: bool = False) -> Spotify:
    access = None if force_refresh else env("SPOTIFY_ACCESS_TOKEN")
    refresh = env("SPOTIFY_REFRESH_TOKEN")
    cid = env("SPOTIFY_CLIENT_ID")
    secret = env("SPOTIFY_CLIENT_SECRET")
    if not access:
        if not (cid and secret and refresh):
            if require_token:
                raise SystemExit(
                    "Missing Spotify credentials. Set SPOTIFY_CLIENT_ID, "
                    "SPOTIFY_CLIENT_SECRET, and SPOTIFY_REFRESH_TOKEN "
                    f"(see {ROOT / '.env.example'}). This script does not start OAuth."
                )
            raise SystemExit("no token")
        access = refresh_access_token(cid, secret, refresh)
        # Keep a fresh access token so the next call does not 401.
        env_path = ROOT / ".env"
        if env_path.is_file():
            lines = []
            seen = False
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                if raw.startswith("SPOTIFY_ACCESS_TOKEN="):
                    lines.append(f"SPOTIFY_ACCESS_TOKEN={access}")
                    seen = True
                else:
                    lines.append(raw)
            if not seen:
                lines.append(f"SPOTIFY_ACCESS_TOKEN={access}")
            env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            os.environ["SPOTIFY_ACCESS_TOKEN"] = access
    return Spotify(access)


def mix_config_from_env(today: date | None = None) -> MixConfig:
    return MixConfig(
        playlist_name=env("MIX_PLAYLIST_NAME", "Weekly Mix") or "Weekly Mix",
        size=env_int("MIX_SIZE", 40),
        min_popularity=env_int("MIX_MIN_POPULARITY", 55),
        max_per_artist=env_int("MIX_MAX_PER_ARTIST", 2),
        use_likes=env_bool("MIX_USE_LIKES", True),
        today=today or date.today(),
    )


def cmd_build_mix(paths: Paths, persist: bool = True) -> list[Candidate]:
    sp = load_client()
    cfg = mix_config_from_env()
    lastfm_key = env("LASTFM_API_KEY")
    lastfm = LastFm(lastfm_key) if lastfm_key else None
    if not lastfm:
        print(
            "note: LASTFM_API_KEY unset — similar artists via MusicBrainz+ListenBrainz; "
            "top tracks via Spotify search (weaker popularity signal)."
        )
    mixer = Mixer(sp, paths, cfg, lastfm)
    user = sp.me()
    tracks = mixer.build(user)
    if persist:
        mixer.persist_mix(tracks, user["id"])
        print(f"wrote {paths.last_mix}")
    print_mix(tracks)
    return tracks


def cmd_publish(paths: Paths, dry_run: bool = False) -> None:
    cfg = mix_config_from_env()
    last = read_json(paths.last_mix, {})
    tracks = last.get("tracks") or []
    if len(tracks) < 1:
        print("no last_mix.json — running build_mix first")
        cmd_build_mix(paths)
        last = read_json(paths.last_mix, {})
        tracks = last.get("tracks") or []
    uris = [t["uri"] for t in tracks if t.get("uri")]
    if not uris:
        raise SystemExit("last mix has no track URIs")

    description = (
        "Agent mix of popular new-to-you tracks. Seeded from playlists you created "
        "(likes as taste signal only). Not based on recently played, top tracks, "
        "or baby/house listening."
    )
    print(f"publish {len(uris)} tracks → playlist {cfg.playlist_name!r}")
    if dry_run:
        print("dry-run: not calling Spotify playlist CRUD")
        for u in uris:
            print(" ", u)
        return

    sp = load_client()
    user = sp.me()
    config = read_json(paths.config, {})
    playlist_id = config.get("playlist_id")
    if playlist_id:
        # confirm it still exists / is ours
        code, body = sp.get_ok(f"/playlists/{playlist_id}")
        if code >= 400:
            print(f"stored playlist {playlist_id} not reachable ({code}); creating a new one")
            playlist_id = None
        else:
            owner = ((body or {}).get("owner") or {}).get("id")
            if owner and owner != user["id"]:
                playlist_id = None

    if not playlist_id:
        created = sp.create_playlist(user["id"], cfg.playlist_name, description, public=False)
        playlist_id = created["id"]
        print(f"created playlist {playlist_id}")
    else:
        sp.change_playlist(playlist_id, name=cfg.playlist_name, description=description)
        print(f"updating playlist {playlist_id}")

    sp.replace_playlist_tracks(playlist_id, uris)
    config.update(
        {
            "playlist_id": playlist_id,
            "playlist_uri": f"spotify:playlist:{playlist_id}",
            "user_id": user["id"],
            "name": cfg.playlist_name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    write_json(paths.config, config)
    last["playlist_id"] = playlist_id
    last["published_at"] = config["updated_at"]
    write_json(paths.last_mix, last)
    print(f"published {len(uris)} tracks → {config['playlist_uri']}")


def mix_heard_progress(paths: Paths) -> dict:
    """How many of this week's mix tracks are in the heard log."""
    last = read_json(paths.last_mix, {})
    tracks = [t for t in (last.get("tracks") or []) if t.get("id")]
    mix_ids = {t["id"] for t in tracks}
    played = read_json(paths.played, {"plays": []})
    heard = _heard_ids(played) & mix_ids
    missing = [t for t in tracks if t["id"] not in heard]
    return {
        "mix_size": len(mix_ids),
        "heard": len(heard),
        "remaining": len(missing),
        "finished": bool(mix_ids) and not missing,
        "published_at": last.get("published_at"),
        "playlist_id": last.get("playlist_id"),
        "missing_names": [t.get("name") for t in missing],
    }


def cmd_maybe_refresh(paths: Paths) -> int:
    """If this week's mix is fully heard, build and overwrite a new one.

    Prints MIX_FINISHED then NEW_MIX so callers know to ping Zach.
    No-op if any mix track is still unheard.
    """
    prog = mix_heard_progress(paths)
    print(
        f"mix progress: {prog['heard']}/{prog['mix_size']} heard, "
        f"{prog['remaining']} remaining"
    )
    if not prog["mix_size"]:
        print("no current mix")
        return 0
    if not prog["finished"]:
        print("mix still open")
        return 0
    print("MIX_FINISHED")
    cmd_build_mix(paths)
    cmd_publish(paths)
    last = read_json(paths.last_mix, {})
    n = len(last.get("tracks") or [])
    print(f"NEW_MIX {n}")
    return 0


def _mix_track_ids(paths: Paths) -> set[str]:
    last = read_json(paths.last_mix, {})
    return {t.get("id") for t in (last.get("tracks") or []) if t.get("id")}


def _heard_ids(played: dict) -> set[str]:
    return {p.get("track_id") for p in (played.get("plays") or []) if p.get("track_id")}


def _append_heard(played: dict, *, track_id: str, uri: str | None, name: str | None,
                  played_at: str, source: str) -> bool:
    if track_id in _heard_ids(played):
        return False
    played.setdefault("plays", []).append(
        {
            "track_id": track_id,
            "uri": uri or f"spotify:track:{track_id}",
            "name": name,
            "played_at": played_at,
            "source": source,
        }
    )
    return True


def harvest_plays(
    paths: Paths,
    sp: Spotify | None = None,
    *,
    include_recent: bool = True,
    include_current: bool = True,
) -> dict:
    """Log Weekly Mix tracks that were heard, including short skips.

    A track counts as heard if:
      - it is currently playing and is on this week's mix, OR the player
        context is the Weekly Mix playlist, OR
      - include_recent and it appears in recently-played on this week's mix.
    Recently-played is never used as a taste seed.

    Watch loops should pass include_recent=False so we only hit
    currently-playing (one request per poll). Dev Mode quota dies otherwise.
    Hourly play-log should pass include_current=False and only hit
    recently-played (one cheap request). Spotify usually omits skips under
    ~30s, so this is a backup for listened-through mix tracks, not skip
    detection.
    """
    sp = sp or load_client()
    config = read_json(paths.config, {})
    playlist_id = config.get("playlist_id")
    if not playlist_id:
        raise SystemExit("no playlist_id in state/config.json — publish first")
    mix_uri = config.get("playlist_uri") or f"spotify:playlist:{playlist_id}"
    mix_ids = _mix_track_ids(paths)
    played = read_json(paths.played, {"playlist_id": playlist_id, "plays": []})
    added = 0
    now = datetime.now(timezone.utc).isoformat()
    on_mix_now = False
    from_mix_now = False
    is_playing = False

    current = None
    if include_current:
        try:
            current = sp.currently_playing()
        except RuntimeError as exc:
            if "401" in str(exc):
                sp = load_client(force_refresh=True)
                current = sp.currently_playing()
            else:
                raise
    if current:
        item = current.get("item") or current.get("track") or {}
        ctx = current.get("context") or {}
        tid = item.get("id")
        ctx_uri = ctx.get("uri") or ""
        is_playing = bool(current.get("is_playing") and tid)
        on_mix_now = bool(tid and tid in mix_ids)
        from_mix_now = ctx_uri == mix_uri
        if tid and (on_mix_now or from_mix_now):
            if _append_heard(
                played,
                track_id=tid,
                uri=item.get("uri"),
                name=item.get("name"),
                played_at=now,
                source="currently-playing",
            ):
                added += 1
                progress = current.get("progress_ms") or 0
                print(
                    f"heard (now playing, {progress}ms): "
                    f"{item.get('name')}"
                )

    items: list = []
    if include_recent:
        try:
            items = sp.recently_played(limit=50)
        except RuntimeError as exc:
            if "401" in str(exc):
                sp = load_client(force_refresh=True)
                items = sp.recently_played(limit=50)
            else:
                raise
        for row in items:
            track = row.get("track") or row.get("item") or {}
            tid = track.get("id")
            if not tid:
                continue
            ctx_uri = ((row.get("context") or {}).get("uri") or "")
            if tid not in mix_ids and ctx_uri != mix_uri:
                continue
            at = row.get("played_at") or now
            if _append_heard(
                played,
                track_id=tid,
                uri=track.get("uri"),
                name=track.get("name"),
                played_at=at,
                source="recently-played",
            ):
                added += 1
                print(f"heard (recent): {track.get('name')}")

    played["playlist_id"] = playlist_id
    played["updated_at"] = now
    write_json(paths.played, played)
    return {
        "added": added,
        "inspected_recent": len(items),
        "log_size": len(played.get("plays") or []),
        "currently_playing": bool(current and current.get("item")),
        "is_playing": is_playing,
        "on_mix": on_mix_now or from_mix_now,
    }


def cmd_log_plays(paths: Paths, *, recent_only: bool = False) -> None:
    stats = harvest_plays(
        paths,
        include_recent=True,
        include_current=not recent_only,
    )
    print(f"recently-played items inspected: {stats['inspected_recent']}")
    print(f"new mix plays recorded: {stats['added']}")
    print(f"play log size: {stats['log_size']}")
    if recent_only:
        print("note: recently-played only (mix intersect history). Skips under ~30s are usually missing.")
    else:
        print("note: now-playing is used to catch skips; never as a taste seed.")


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def ingest_ui_nowplaying(paths: Paths) -> dict:
    """Mark mix tracks heard from the web-player now-playing log.

    The log is written by a browser watcher of open.spotify.com (no Web API).
    Each JSON line: {"ts": iso, "title": "...", "artists": "A, B"}
    """
    log_path = paths.state / "nowplaying.jsonl"
    last = read_json(paths.last_mix, {})
    mix = last.get("tracks") or []
    by_key: dict[str, dict] = {}
    for t in mix:
        title = _norm(t.get("name") or "")
        artists = [_norm(a) for a in (t.get("artists") or [])]
        if not title or not t.get("id"):
            continue
        by_key[title] = t
        for a in artists:
            by_key[f"{title}|{a}"] = t
    played = read_json(paths.played, {"plays": []})
    added = 0
    scanned = 0
    if not log_path.is_file():
        return {"added": 0, "scanned": 0, "log_size": len(played.get("plays") or [])}
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        scanned += 1
        title = _norm(row.get("title") or row.get("name") or "")
        artists_raw = row.get("artists") or row.get("artist") or ""
        if isinstance(artists_raw, list):
            artist_list = [_norm(a) for a in artists_raw if a]
        else:
            artist_list = [_norm(a) for a in str(artists_raw).split(",") if a.strip()]
        hit = None
        for a in artist_list:
            hit = by_key.get(f"{title}|{a}")
            if hit:
                break
        if not hit:
            hit = by_key.get(title)
        if not hit:
            continue
        ts = row.get("ts") or datetime.now(timezone.utc).isoformat()
        if _append_heard(
            played,
            track_id=hit["id"],
            uri=hit.get("uri"),
            name=hit.get("name"),
            played_at=str(ts),
            source="web-player-ui",
        ):
            added += 1
            print(f"heard (web player): {hit.get('name')}")
    config = read_json(paths.config, {})
    played["playlist_id"] = config.get("playlist_id")
    played["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json(paths.played, played)
    return {"added": added, "scanned": scanned, "log_size": len(played.get("plays") or [])}


def cmd_ingest_ui(paths: Paths) -> None:
    stats = ingest_ui_nowplaying(paths)
    print(f"ui nowplaying lines scanned: {stats['scanned']}")
    print(f"new mix plays recorded: {stats['added']}")
    print(f"play log size: {stats['log_size']}")
    print("note: web-player UI log, no Web API quota.")


def cmd_watch_plays(
    paths: Paths,
    interval: float = 12.0,
    idle_interval: float = 90.0,
    other_interval: float = 60.0,
) -> None:
    """Adaptive now-playing poll so short skips still count without burning quota.

    Only calls currently-playing each loop (never recently-played). Sleeps longer
    when nothing is playing or playback is outside Weekly Mix.
    Defaults: ~12s on the mix, ~60s on other playback, ~90s when idle.
    """
    pid_path = paths.state / "watch_plays.pid"
    paths.state.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    print(
        f"watch_plays pid={os.getpid()} "
        f"on_mix={interval}s other={other_interval}s idle={idle_interval}s "
        f"(currently-playing only)"
    )
    sp = load_client(force_refresh=True)
    while True:
        sleep_for = idle_interval
        try:
            stats = harvest_plays(paths, sp=sp, include_recent=False)
            if stats["added"]:
                print(
                    f"logged {stats['added']} new heard track(s); "
                    f"log={stats['log_size']}"
                )
            if stats.get("on_mix") and stats.get("is_playing"):
                sleep_for = interval
            elif stats.get("currently_playing"):
                sleep_for = other_interval
            else:
                sleep_for = idle_interval
        except RuntimeError as exc:
            msg = str(exc)
            print(f"watch_plays error: {exc}")
            if "429" in msg or "QUOTA" in msg.upper():
                sleep_for = max(idle_interval, 300)
            else:
                try:
                    sp = load_client(force_refresh=True)
                except Exception as exc2:
                    print(f"token refresh failed: {exc2}")
                    sleep_for = max(idle_interval, 60)
        except Exception as exc:
            print(f"watch_plays error: {exc}")
            sleep_for = idle_interval
        time.sleep(sleep_for)


def cmd_probe(paths: Paths) -> None:
    sp = load_client()
    me = sp.me()
    print(f"user: {me.get('id')}  display={me.get('display_name')!r}")
    caps = sp.probe()
    print("endpoint probe (new apps: related/recs are 403 since 2024-11-27;")
    print("Dev Mode also dropped top-tracks + popularity in Feb 2026):")
    print(f"  related-artists:     {caps.related_artists}")
    print(f"  recommendations:     {caps.recommendations}")
    print(f"  artist top-tracks:   {caps.artist_top_tracks}")
    print(f"  popularity field:    {caps.popularity_field}")
    write_json(
        paths.state / "probe.json",
        {
            "probed_at": datetime.now(timezone.utc).isoformat(),
            "user_id": me.get("id"),
            "caps": {
                "related_artists": caps.related_artists,
                "recommendations": caps.recommendations,
                "artist_top_tracks": caps.artist_top_tracks,
                "popularity_field": caps.popularity_field,
            },
        },
    )


def cmd_self_test() -> int:
    """No network. Verifies seasonal + baby skip rules for 2026-08-30 and other dates."""
    failures = 0

    def check(cond: bool, msg: str) -> None:
        nonlocal failures
        if cond:
            print(f"  ok  {msg}")
        else:
            print(f" FAIL {msg}")
            failures += 1

    today = date(2026, 8, 30)
    check(seasonal_reason("Christmas Bangers", today) is not None, "Christmas skipped in August")
    check(seasonal_reason("Halloween Mix", today) is not None, "Halloween skipped in August")
    check(seasonal_reason("Thanksgiving Dinner", today) is not None, "Thanksgiving skipped in August")
    check(seasonal_reason("4th of July BBQ", today) is not None, "July 4 skipped in August")
    check(seasonal_reason("Valentine's Day", today) is not None, "Valentine skipped in August")
    check(seasonal_reason("Road Trip 2024", today) is None, "non-seasonal kept in August")
    check(seasonal_reason("Christmas Bangers", date(2026, 12, 15)) is None, "Christmas kept in December")
    check(seasonal_reason("Halloween Mix", date(2026, 10, 20)) is None, "Halloween kept in October")
    check(seasonal_reason("Thanksgiving Dinner", date(2026, 11, 10)) is not None, "Thanksgiving skipped early Nov")
    check(seasonal_reason("Thanksgiving Dinner", date(2026, 11, 25)) is None, "Thanksgiving kept late Nov")
    check(seasonal_reason("4th of July BBQ", date(2026, 7, 4)) is None, "July 4 kept on July 4")
    check(skip_seed_reason("Baby Sleep", today, "Weekly Mix") is not None, "baby playlist skipped")
    check(skip_seed_reason("Kids Party", today, "Weekly Mix") is not None, "kids playlist skipped")
    check(skip_seed_reason("Nursery Rhymes", today, "Weekly Mix") is not None, "nursery skipped")
    check(skip_seed_reason("House Listening", today, "Weekly Mix") is not None, "house listening skipped")
    check(skip_seed_reason("Weekly Mix", today, "Weekly Mix") is not None, "output playlist skipped")
    check(skip_seed_reason("Deep Cuts", today, "Weekly Mix") is None, "normal playlist kept")
    check(lastfm_listeners_to_popularity(3200) >= 50, "Last.fm ~3k listeners maps near 55")
    check(lastfm_listeners_to_popularity(1_000_000) >= 85, "Last.fm 1M listeners maps high")
    print("self_test failures:", failures)
    return 1 if failures else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Weekly Spotify discovery mixer (no live OAuth).")
    p.add_argument("--state-dir", default=str(DEFAULT_STATE))
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build_mix", help="compute ~40 tracks and print them")
    pub = sub.add_parser("publish", help="create or replace the Weekly Mix playlist")
    pub.add_argument("--dry-run", action="store_true", help="print URIs; do not touch Spotify playlists")
    logp = sub.add_parser("log_plays", help="record heard mix tracks (now-playing + recently-played)")
    logp.add_argument(
        "--recent-only",
        action="store_true",
        help="only GET /me/player/recently-played (no currently-playing). Cheap hourly backup.",
    )
    sub.add_parser("ingest_ui", help="ingest open.spotify.com now-playing log (no Web API)")
    sub.add_parser(
        "maybe_refresh",
        help="if this week's mix is fully heard, build and publish a replacement",
    )
    watch = sub.add_parser(
        "watch_plays",
        help="adaptive now-playing poll so short skips count without burning quota",
    )
    watch.add_argument(
        "--interval",
        type=float,
        default=12.0,
        help="seconds between polls while Weekly Mix is playing (default 12)",
    )
    watch.add_argument(
        "--idle-interval",
        type=float,
        default=90.0,
        help="seconds between polls when nothing is playing (default 90)",
    )
    watch.add_argument(
        "--other-interval",
        type=float,
        default=60.0,
        help="seconds between polls when playing something else (default 60)",
    )
    sub.add_parser("probe", help="detect which Spotify endpoints still work for this app")
    sub.add_parser("self_test", help="local season/baby filter tests (no network)")
    auth = sub.add_parser("print_auth_url", help="print the authorize URL (does not open a browser)")
    auth.add_argument("--redirect-uri", default="http://127.0.0.1:8080/callback")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    load_dotenv(Path(args.env_file))
    paths = Paths(root=ROOT, state=Path(args.state_dir))
    paths.state.mkdir(parents=True, exist_ok=True)

    if args.cmd == "self_test":
        return cmd_self_test()
    if args.cmd == "print_auth_url":
        cid = env("SPOTIFY_CLIENT_ID")
        if not cid:
            print("Set SPOTIFY_CLIENT_ID first. This command only prints a URL.")
            return 1
        print(authorize_url(cid, args.redirect_uri))
        print("scopes:", " ".join(SCOPES))
        print("This script does not start a server or exchange the code.")
        return 0
    if args.cmd == "build_mix":
        cmd_build_mix(paths)
        return 0
    if args.cmd == "publish":
        cmd_publish(paths, dry_run=args.dry_run)
        return 0
    if args.cmd == "log_plays":
        cmd_log_plays(paths, recent_only=args.recent_only)
        return 0
    if args.cmd == "ingest_ui":
        cmd_ingest_ui(paths)
        return 0
    if args.cmd == "maybe_refresh":
        return cmd_maybe_refresh(paths)
    if args.cmd == "watch_plays":
        cmd_watch_plays(
            paths,
            interval=args.interval,
            idle_interval=args.idle_interval,
            other_interval=args.other_interval,
        )
        return 0
    if args.cmd == "probe":
        cmd_probe(paths)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
