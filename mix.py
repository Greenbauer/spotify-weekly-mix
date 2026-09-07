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

Discovery is Spotify-first. /recommendations and /related-artists are 403 for
new apps since 2024-11-27; Dev Mode also lost /artists/{id}/top-tracks and the
popularity field in Feb 2026. Similar artists come from MusicBrainz+ListenBrainz
(no key). Popular tracks come from Spotify search (and top-tracks if this app
still has it). Last.fm is not used.

  created-playlist artists
    → MusicBrainz + ListenBrainz similar-artists
    → those artists' Spotify search hits (top-tracks if available)
    → drop anything already in created playlists, likes, or the play log
    → drop sleep/ambient/rain-mill titles and unusable seed names
    → keep ~40, search-rank differentiated, max N per artist

Commands: build_mix | publish | log_plays | watch_plays | ingest_ui |
          maybe_refresh | probe | print_auth_url | self_test

Business logic: docs/ALGORITHM.md. This script does not register a Spotify
app and does not start OAuth; oauth.py is the separate, opt-in helper that does.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = ROOT / "state"
SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_ACCOUNTS = "https://accounts.spotify.com/api/token"
MB_API = "https://musicbrainz.org/ws/2"
LB_SIMILAR = "https://labs.api.listenbrainz.org/similar-artists/json"
LB_SIMILAR_ALGO = "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
UA = "spotify-weekly-mix/1.0 (personal discovery mixer)"

# A rebuilt mix this much smaller than the one it replaces is a failed build,
# not a new mix. build_mix overwrites last_mix.json BEFORE publish is ever
# attempted, so without this a rate-limited run destroys the record of the good
# mix and every later maybe_refresh sees mix_size == 0 and silently no-ops.
# A ratio (not an absolute floor) so a legitimately small library still works.
MIX_MIN_REPLACE_RATIO = 0.6

# maybe_refresh's escape hatches. "Every track heard" alone wedges forever on a
# single track that is region-locked, removed, or relinked to another id.
MIX_HEARD_RATIO = 0.9
MIX_MAX_AGE_DAYS = 14

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


def output_playlist_reason(name: str, mix_name: str) -> str | None:
    """Our own output playlist: skipped entirely, both seeds AND excludes.

    Its tracks must NOT join the exclude set — an unheard track from last
    week's mix is deliberately allowed to come back (see played_ids).
    """
    if name.strip().lower() == mix_name.strip().lower():
        return f"output playlist: {name!r}"
    return None


def seed_only_skip_reason(name: str, today: date) -> str | None:
    """Not a taste source — but its tracks still belong in the exclude set.

    Skipping a playlist for SEEDING used to drop it from the excludes too,
    which is how songs already in your own library got published back to you
    as discoveries.
    """
    if SKIP_SEED_NAME_RE.search(name):
        return f"baby/house playlist: {name!r}"
    return seasonal_reason(name, today)


def skip_seed_reason(name: str, today: date, mix_name: str) -> str | None:
    """True when a playlist contributes no seed artists, for any reason."""
    return output_playlist_reason(name, mix_name) or seed_only_skip_reason(name, today)


# ---------------------------------------------------------------------------
# Discovery quality: artist resolve, junk titles, short names
# ---------------------------------------------------------------------------

MIN_ARTIST_NAME_LEN = 3

# Names that resolve to the wrong catalog entry or are not taste. Length < 3
# is already refused; this list catches the rest (common English words and
# credit-line leftovers). "Py" is length 2 so the length rule covers it.
BLOCKED_ARTIST_NAMES = frozenset(
    {
        "the",
        "and",
        "you",
        "me",
        "we",
        "it",
        "he",
        "she",
        "a",
        "an",
        "of",
        "to",
        "for",
        "my",
        "our",
        "your",
        "dj",
        "mc",
        "vs",
        "remix",
        "mix",
        "edit",
        "live",
        "feat",
        "featuring",
        "various",
        "various artists",
        "unknown",
        "artist",
    }
)

# Sleep / ambient mill leakage. Compound rain/thunder phrases, not the
# standalone hit titles "Rain" or "Thunder".
JUNK_DISCOVERY_RE = re.compile(
    r"""
    (?:white|brown|pink)[\s-]*noise |
    pure\s+sleeping |
    sleep(?:ing)?[\s-]*(?:music|sounds?|vibes?|playlist|aid) |
    lullab(?:y|ies) |
    \bspa\b |
    \bmassage\b |
    \byoga\b |
    \bmeditation\b |
    432\s*hz |
    lo-?fi[\s-]+(?:study|beats|hip[\s-]*hop) |
    rain[\s-]+(?:on(?:\s+the)?\s+roof|sounds?|and[\s-]+thunder) |
    thunder[\s-]+(?:and[\s-]+rain|sounds?|rain) |
    soothing(?:[\s-]+(?:sleep|rain|ambient|music|sounds?|vibes?))? |
    nature[\s-]+sounds? |
    ambient(?:[\s-]+(?:sleep|music|rain|sounds?|noise))?
    """,
    re.I | re.X,
)

_FEAT_SUFFIX_RE = re.compile(
    r"\s+(feat\.?|ft\.?|featuring|with)\s+.+$",
    re.I,
)

# Path C may only take this many top search hits per artist. Rank 0 is first.
PATH_C_MAX_RANK = 3


def normalize_artist_name(name: str) -> str:
    """Fold case, punctuation, and '&' / 'and' for artist comparison."""
    s = (name or "").strip().lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_usable_artist_name(name: str) -> bool:
    """Refuse seeds/searches that resolve badly (too short or blocked words)."""
    raw = (name or "").strip()
    if len(raw) < MIN_ARTIST_NAME_LEN:
        return False
    norm = normalize_artist_name(raw)
    if len(norm) < MIN_ARTIST_NAME_LEN:
        return False
    return norm not in BLOCKED_ARTIST_NAMES


def is_junk_discovery(title: str, artists: list[str] | None = None) -> bool:
    """True when a title or credited artist is sleep/ambient/rain-mill junk."""
    blobs = [title or ""]
    blobs.extend(artists or [])
    return any(JUNK_DISCOVERY_RE.search(blob) for blob in blobs if blob)


def primary_artist_matches(want: str, primary: str) -> bool:
    """True when the Spotify primary artist is the requested artist.

    Exact match after normalize. A feat./ft./with suffix on the primary is
    stripped so 'The National feat. X' still matches 'The National'.
    Extra words that are not a featuring credit are a different artist:
    'The National Forest' does not match 'The National'.
    """
    w = normalize_artist_name(want)
    p = normalize_artist_name(primary)
    if not w or not p:
        return False
    p_core = _FEAT_SUFFIX_RE.sub("", p).strip()
    return w == p or w == p_core


def path_c_guessed_popularity(rank: int, min_popularity: int) -> int:
    """Search-rank soft score. Rank 0 is strongest.

    Strictly decreasing by rank so hits are not interchangeable. The old
    formula clamped every usable rank to min_popularity (55), so a Path C
    mix was 40 identical scores. Always below min_popularity so a real
    Spotify popularity field, when present, outranks every guess. Path C
    guesses do not use min_popularity as an admission gate: search rank,
    junk filters, and primary-artist match decide eligibility.
    """
    return max(1, min_popularity - 1 - 2 * rank)


def qualify_seed_artists(counts: Counter, min_count: int) -> Counter:
    """Drop one-off primary artists unless that would empty the seed set."""
    if min_count <= 1:
        return counts
    kept = Counter({name: n for name, n in counts.items() if n >= min_count})
    return kept if kept else counts


def primary_artist_name(track: dict) -> str | None:
    artists = track.get("artists") or []
    if not artists:
        return None
    name = (artists[0].get("name") or "").strip()
    return name or None


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
    """Spotify client that owns its own token refresh.

    `refresh` returns a fresh access token. Every request goes through
    `_request`, which retries ONCE on a 401 with a new token, so callers never
    have to handle expiry themselves. Access tokens are short-lived and the
    cached one in .env carries no recorded expiry, so a 401 is the only
    reliable signal that it went stale.
    """

    def __init__(self, access_token: str, refresh: Any = None) -> None:
        self.http = Http()
        self._set_token(access_token)
        self.caps = SpotifyCaps()
        self._refresh = refresh

    def _set_token(self, access_token: str) -> None:
        self.http.s.headers["Authorization"] = f"Bearer {access_token}"

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        resp = self.http.request(method, f"{SPOTIFY_API}{path}", **kwargs)
        if resp.status_code == 401 and self._refresh is not None:
            self._set_token(self._refresh())
            resp = self.http.request(method, f"{SPOTIFY_API}{path}", **kwargs)
        return resp

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self._request("GET", path, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"GET {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    def _send(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        return self._request(method, path, **kwargs)

    def get_ok(self, path: str, params: dict | None = None) -> tuple[int, Any]:
        resp = self._request("GET", path, params=params)
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
        """Return track objects. Tries /items (Dev Mode 2026) then /tracks.

        Raises on any non-404 error. An unreadable playlist must never look
        like an empty one: its track ids would drop out of the exclude set and
        songs already in your library would be published back as discoveries.
        404 alone means "wrong path shape for this API version" — keep trying.
        """
        tracks: list[dict] = []
        suffixes = list(dict.fromkeys([self.caps.playlist_items_path, "items", "tracks"]))
        for suffix in suffixes:
            code, body = self.get_ok(
                f"/playlists/{playlist_id}/{suffix}",
                {"limit": 50},
            )
            if code == 404:
                continue
            if code >= 400:
                raise RuntimeError(
                    f"playlist {playlist_id} /{suffix} -> {code}. Refusing to treat "
                    f"an unreadable playlist as empty."
                )
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
        raise RuntimeError(
            f"playlist {playlist_id}: no readable items path (tried {suffixes})"
        )

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
# MusicBrainz / ListenBrainz  (related-artists replacement; no API key)
# ---------------------------------------------------------------------------

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
    # False = Path C search-rank guess (no Spotify popularity field).
    measured: bool = True


@dataclass
class MixConfig:
    playlist_name: str = "Weekly Mix"
    size: int = 40
    min_popularity: int = 55
    max_per_artist: int = 2
    use_likes: bool = True
    min_seed_count: int = 2
    max_path_c: int = 10
    today: date = field(default_factory=lambda: date.today())


def artist_names(track: dict) -> list[str]:
    return [a.get("name") for a in (track.get("artists") or []) if a.get("name")]


def artist_ids(track: dict) -> list[str]:
    return [a.get("id") for a in (track.get("artists") or []) if a.get("id")]


def track_uri(track: dict) -> str:
    return track.get("uri") or f"spotify:track:{track['id']}"


def _primary_key(c: Candidate) -> str:
    return c.artist_ids[0] if c.artist_ids else (c.artists[0] if c.artists else c.track_id)


def _weighted_pick(
    pool: list[Candidate],
    need: int,
    rng: random.Random,
    per_artist: Counter,
    max_per_artist: int,
) -> list[Candidate]:
    picked: list[Candidate] = []
    remaining = pool[:]
    while remaining and len(picked) < need:
        eligible: list[Candidate] = []
        weights: list[float] = []
        for c in remaining:
            if per_artist[_primary_key(c)] >= max_per_artist:
                continue
            eligible.append(c)
            weights.append(max(1, c.popularity) ** 1.4)
        if not eligible:
            break
        choice = rng.choices(eligible, weights=weights, k=1)[0]
        picked.append(choice)
        per_artist[_primary_key(choice)] += 1
        remaining = [c for c in remaining if c.track_id != choice.track_id]
    return picked


def select_mix_tracks(
    pool: list[Candidate],
    *,
    size: int,
    max_per_artist: int,
    max_path_c: int,
    rng: random.Random,
) -> list[Candidate]:
    """Pick the final mix. Real Spotify popularity first; Path C ranked next.

    When any measured tracks exist, Path C guesses fill remaining slots up to
    max_path_c. When the pool is Path C only (Dev Mode, no popularity field),
    that is the happy path: fill to size. Search-rank scores stay distinct and
    below the popularity gate so they never all tie at 55.
    """
    measured = [c for c in pool if c.measured]
    guessed = [c for c in pool if not c.measured]
    measured.sort(key=lambda c: c.popularity, reverse=True)
    guessed.sort(key=lambda c: c.popularity, reverse=True)

    per_artist: Counter = Counter()
    picked = _weighted_pick(measured, size, rng, per_artist, max_per_artist)
    if len(picked) < size and guessed:
        leftover = size - len(picked)
        need = leftover if not picked else min(max_path_c, leftover)
        picked.extend(_weighted_pick(guessed, need, rng, per_artist, max_per_artist))

    picked.sort(key=lambda c: (0 if c.measured else 1, -c.popularity))
    return picked


class Mixer:
    def __init__(self, sp: Spotify, paths: Paths, cfg: MixConfig) -> None:
        self.sp = sp
        self.paths = paths
        self.cfg = cfg
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

        Two independent decisions, which used to be one:
          - EXCLUDE: every track you already own, from every created playlist
            and from Liked Songs. Never publish these back to you.
          - SEED: only playlists that represent taste. Kids, out-of-season and
            the output playlist are not taste sources.
        A playlist skipped for seeding still contributes its excludes; only the
        output playlist is skipped for both.
        """
        mix_name = self.cfg.playlist_name
        today = self.cfg.today
        created = self.sp.created_playlists(user_id)

        exclude: set[str] = set()
        seed_artist_names: Counter = Counter()
        seed_playlists: list[dict] = []
        skipped_seeding: list[str] = []

        print(f"created playlists: {len(created)}")
        for pl in created:
            name = pl.get("name") or ""
            if output_playlist_reason(name, mix_name):
                print(f"  skip entirely: {name!r} (our own output)")
                continue

            tracks = self.sp.playlist_items(pl["id"])
            for t in tracks:
                if t.get("id"):
                    exclude.add(t["id"])

            reason = seed_only_skip_reason(name, today)
            if reason:
                skipped_seeding.append(reason)
                print(f"  {name!r}: {len(tracks)} tracks (excludes only — {reason})")
                continue

            seed_playlists.append(pl)
            print(f"  {name!r}: {len(tracks)} tracks (seeds + excludes)")
            for t in tracks:
                self._count_primary_seed(seed_artist_names, t)

        # Likes are always excludes ("likes = seeds + exclude" per the README);
        # MIX_USE_LIKES only decides whether they also seed.
        likes = self.sp.liked_tracks()
        for t in likes:
            if t.get("id"):
                exclude.add(t["id"])
        if self.cfg.use_likes:
            print(f"liked songs (excludes + artist seeds): {len(likes)}")
            for t in likes:
                self._count_primary_seed(seed_artist_names, t)
        else:
            print(f"liked songs (excludes only): {len(likes)}")

        print(f"seed playlists:    {len(seed_playlists)}")
        exclude |= self.played_ids()
        print(f"exclude track ids: {len(exclude)}")
        return seed_playlists, exclude, seed_artist_names

    @staticmethod
    def _count_primary_seed(dest: Counter, track: dict) -> None:
        """Count the primary artist only. Featured / remix credits do not seed."""
        name = primary_artist_name(track)
        if name and is_usable_artist_name(name):
            dest[name] += 1

    def similar_for(self, artist_name: str, limit: int = 10) -> list[str]:
        key = artist_name.strip().lower()
        cached = self._similar_cache.get(key)
        if isinstance(cached, dict) and cached.get("names"):
            ts = cached.get("ts") or 0
            if time.time() - ts < 14 * 86400:
                return list(cached["names"])[:limit]

        names: list[str] = []
        mbid = self.mb.artist_mbid(artist_name)
        if mbid:
            names = [n for n, _ in self.lb.similar_artists(mbid, limit=limit)]
        # Spotify related-artists only if this app still has it (extended quota).
        if not names and self.sp.caps.related_artists is not False:
            sp_artist = self.sp.search_artist(artist_name)
            if sp_artist and sp_artist.get("id"):
                rel = self.sp.related_artists(sp_artist["id"])
                names = [a.get("name") for a in rel if a.get("name")][:limit]

        names = [n for n in names if is_usable_artist_name(n)]
        self._similar_cache[key] = {"ts": int(time.time()), "names": names}
        return names[:limit]

    def resolve_track(self, title: str, artist: str) -> dict | None:
        """Resolve (title, artist) onto a Spotify track, or None.

        The primary Spotify artist must be the requested artist (normalized).
        'The National Forest' is not 'The National'. A fuzzy credited-artist
        contains check used to accept that. Junk sleep/rain titles are dropped
        here, not after they have already been scored.
        """
        if not is_usable_artist_name(artist):
            return None
        q = f'track:"{title}" artist:"{artist}"'
        hits = self.sp.search_tracks(q, limit=5)
        if not hits:
            hits = self.sp.search_tracks(f"{title} {artist}", limit=5)
        want_t = title.strip().lower()
        exact = None
        starts = None
        for h in hits:
            if not h.get("id"):
                continue
            credited = artist_names(h)
            if not credited or not primary_artist_matches(artist, credited[0]):
                continue
            hname = (h.get("name") or "").strip()
            if is_junk_discovery(hname, credited):
                continue
            h_l = hname.lower()
            if h_l == want_t:
                exact = h
                break
            if starts is None and (h_l.startswith(want_t) or want_t.startswith(h_l)):
                starts = h
        return exact or starts

    def _candidate_from_hit(
        self,
        hit: dict,
        *,
        artist_name: str,
        popularity: int,
        source: str,
        measured: bool,
    ) -> Candidate | None:
        if not hit.get("id"):
            return None
        name = hit.get("name") or ""
        artists = artist_names(hit) or [artist_name]
        if is_junk_discovery(name, artists):
            return None
        return Candidate(
            track_id=hit["id"],
            uri=track_uri(hit),
            name=name,
            artists=artists,
            artist_ids=artist_ids(hit),
            popularity=popularity,
            source=source,
            measured=measured,
        )

    def popular_tracks_for(self, artist_name: str, n: int = 8) -> list[Candidate]:
        """Top/popular tracks for an artist, resolved onto Spotify."""
        if not is_usable_artist_name(artist_name):
            return []
        found: list[Candidate] = []

        # Path B: Spotify artist top-tracks (extended quota / grandfathered).
        if self.sp.caps.artist_top_tracks is not False:
            sp_artist = self.sp.search_artist(artist_name)
            if sp_artist and sp_artist.get("id"):
                for hit in self.sp.artist_top_tracks(sp_artist["id"]):
                    pop = hit.get("popularity")
                    if not isinstance(pop, int):
                        pop = 60  # endpoint exists ⇒ these ARE the popular ones
                    cand = self._candidate_from_hit(
                        hit,
                        artist_name=artist_name,
                        popularity=pop,
                        source=f"spotify-top:{artist_name}",
                        measured=True,
                    )
                    if cand:
                        found.append(cand)

        # Path C: Spotify search. This is the Dev Mode happy path. Search
        # order is a soft rank: earlier hits score higher, never all equal
        # at the popularity gate.
        if len(found) < 3:
            hits = self.sp.search_tracks(f'artist:"{artist_name}"', limit=10)
            for i, hit in enumerate(hits):
                if i >= PATH_C_MAX_RANK:
                    break
                credited = artist_names(hit)
                if not credited or not primary_artist_matches(artist_name, credited[0]):
                    continue
                pop = hit.get("popularity")
                if isinstance(pop, int):
                    self.sp.caps.popularity_field = True
                    cand = self._candidate_from_hit(
                        hit,
                        artist_name=artist_name,
                        popularity=pop,
                        source=f"spotify-search:{artist_name}",
                        measured=True,
                    )
                    if cand:
                        found.append(cand)
                    continue
                cand = self._candidate_from_hit(
                    hit,
                    artist_name=artist_name,
                    popularity=path_c_guessed_popularity(i, self.cfg.min_popularity),
                    source=f"spotify-search:{artist_name}",
                    measured=False,
                )
                if cand:
                    found.append(cand)

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
        _, exclude, raw_seeds = self.collect_library(user_id)
        seed_names = qualify_seed_artists(raw_seeds, self.cfg.min_seed_count)
        if seed_names is not raw_seeds:
            print(
                f"seed artists after min count {self.cfg.min_seed_count}: "
                f"{len(seed_names)} (from {len(raw_seeds)} unique primaries)"
            )
        if not seed_names:
            raise RuntimeError("no seed artists — create some playlists first")

        top_seeds = [name for name, _ in seed_names.most_common(40)]
        print(f"seed artists: {len(top_seeds)} (from {len(seed_names)} unique)")

        similar: Counter = Counter()
        for name in top_seeds:
            for rel in self.similar_for(name, limit=8):
                if not is_usable_artist_name(rel):
                    continue
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

        by_artist: list[tuple[str, list[Candidate]]] = []
        for artist in similar_ranked:
            if not is_usable_artist_name(artist):
                continue
            by_artist.append((artist, self.popular_tracks_for(artist, n=8)))
            n_cands = sum(len(cs) for _, cs in by_artist)
            if n_cands >= self.cfg.size * 8:
                break

        measured_groups = [(a, cs) for a, cs in by_artist if any(c.measured for c in cs)]
        guess_groups = [(a, cs) for a, cs in by_artist if not any(c.measured for c in cs)]
        # Prefer artists that produced real Spotify popularity (Path B, or
        # Path C with the popularity field). Path-C-only artists are the
        # Dev Mode happy path and are always consulted if measured is thin.
        ordered_groups = measured_groups
        measured_n = sum(len(cs) for _, cs in measured_groups)
        if measured_n < self.cfg.size * 3:
            ordered_groups = measured_groups + guess_groups

        pool: list[Candidate] = []
        seen_ids = set(exclude)
        for _artist, cands in ordered_groups:
            artist_has_measured = any(c.measured for c in cands)
            for cand in cands:
                if cand.track_id in seen_ids:
                    continue
                if is_junk_discovery(cand.name, cand.artists):
                    continue
                if cand.measured and cand.popularity < self.cfg.min_popularity:
                    continue
                if artist_has_measured and not cand.measured:
                    continue
                seen_ids.add(cand.track_id)
                pool.append(cand)
            if len(pool) >= self.cfg.size * 6:
                break

        print(f"candidate pool after filters: {len(pool)}")
        picked = select_mix_tracks(
            pool,
            size=self.cfg.size,
            max_per_artist=self.cfg.max_per_artist,
            max_path_c=self.cfg.max_path_c,
            rng=rng,
        )
        return picked

    def persist_mix(self, tracks: list[Candidate], user_id: str, force: bool = False) -> dict:
        previous = len(read_json(self.paths.last_mix, {}).get("tracks") or [])
        floor = int(previous * MIX_MIN_REPLACE_RATIO)
        if previous and not force and len(tracks) < floor:
            raise RuntimeError(
                f"refusing to replace a {previous}-track mix with {len(tracks)}: "
                f"below the {floor}-track floor. A source is probably rate-limited "
                f"or down — re-run when it recovers, or pass --force to overwrite."
            )
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "week": self.cfg.today.isocalendar()[:2],
            "playlist_name": self.cfg.playlist_name,
            "rules": {
                "size": self.cfg.size,
                "min_popularity": self.cfg.min_popularity,
                "max_per_artist": self.cfg.max_per_artist,
                "min_seed_count": self.cfg.min_seed_count,
                "max_path_c": self.cfg.max_path_c,
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


def write_env_value(path: Path, key: str, value: str) -> None:
    """Set one key in a .env file atomically, preserving its mode.

    Same temp+os.replace as write_json. This file holds SPOTIFY_REFRESH_TOKEN
    and SPOTIFY_CLIENT_SECRET, and a watch_plays daemon can be rewriting it
    while a log_plays cron does the same; a truncate-then-write loses it.
    """
    if not path.is_file():
        return
    lines: list[str] = []
    seen = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith(f"{key}="):
            lines.append(f"{key}={value}")
            seen = True
        else:
            lines.append(raw)
    if not seen:
        lines.append(f"{key}={value}")
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)


def load_client(require_token: bool = True, force_refresh: bool = False) -> Spotify:
    """Build a Spotify client that can refresh its own token.

    The returned client retries once on 401, so every entry point self-heals
    from an expired cached token. This is what .env.example has always
    promised ("If set, refresh is skipped until 401").
    """
    access = None if force_refresh else env("SPOTIFY_ACCESS_TOKEN")
    refresh_token = env("SPOTIFY_REFRESH_TOKEN")
    cid = env("SPOTIFY_CLIENT_ID")
    secret = env("SPOTIFY_CLIENT_SECRET")
    can_refresh = bool(cid and secret and refresh_token)

    def mint() -> str:
        token = refresh_access_token(cid, secret, refresh_token)
        # Keep a fresh access token so a separate build_mix process does not 401.
        write_env_value(ROOT / ".env", "SPOTIFY_ACCESS_TOKEN", token)
        os.environ["SPOTIFY_ACCESS_TOKEN"] = token
        return token

    if not access:
        if not can_refresh:
            if require_token:
                raise SystemExit(
                    "Missing Spotify credentials. Set SPOTIFY_CLIENT_ID, "
                    "SPOTIFY_CLIENT_SECRET, and SPOTIFY_REFRESH_TOKEN "
                    f"(see {ROOT / '.env.example'}). This script does not start OAuth."
                )
            raise SystemExit("no token")
        access = mint()
    return Spotify(access, refresh=mint if can_refresh else None)


def mix_config_from_env(today: date | None = None) -> MixConfig:
    return MixConfig(
        playlist_name=env("MIX_PLAYLIST_NAME", "Weekly Mix") or "Weekly Mix",
        size=env_int("MIX_SIZE", 40),
        min_popularity=env_int("MIX_MIN_POPULARITY", 55),
        max_per_artist=env_int("MIX_MAX_PER_ARTIST", 2),
        use_likes=env_bool("MIX_USE_LIKES", True),
        min_seed_count=env_int("MIX_MIN_SEED_COUNT", 2),
        max_path_c=env_int("MIX_MAX_PATH_C", 10),
        today=today or date.today(),
    )


def cmd_build_mix(paths: Paths, persist: bool = True, force: bool = False) -> list[Candidate]:
    sp = load_client()
    cfg = mix_config_from_env()
    mixer = Mixer(sp, paths, cfg)
    user = sp.me()
    tracks = mixer.build(user)
    if persist:
        mixer.persist_mix(tracks, user["id"], force=force)
        print(f"wrote {paths.last_mix}")
    print_mix(tracks)
    return tracks


def cmd_publish(paths: Paths, dry_run: bool = False, force: bool = False) -> None:
    cfg = mix_config_from_env()
    last = read_json(paths.last_mix, {})
    tracks = last.get("tracks") or []
    if len(tracks) < 1:
        print("no last_mix.json — running build_mix first")
        cmd_build_mix(paths, force=force)
        last = read_json(paths.last_mix, {})
        tracks = last.get("tracks") or []
    uris = [t["uri"] for t in tracks if t.get("uri")]
    if not uris:
        raise SystemExit("last mix has no track URIs")

    # Same floor as persist_mix: replacing the live playlist is destructive and
    # maybe_refresh does it unattended, so a thin build must not overwrite a
    # full one just because it produced at least one URI.
    published = read_json(paths.config, {}).get("track_count") or 0
    floor = int(published * MIX_MIN_REPLACE_RATIO)
    if published and not force and len(uris) < floor:
        raise SystemExit(
            f"refusing to replace a {published}-track playlist with {len(uris)}: "
            f"below the {floor}-track floor. Re-run when sources recover, "
            f"or pass --force."
        )

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
            "track_count": len(uris),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    write_json(paths.config, config)
    last["playlist_id"] = playlist_id
    last["published_at"] = config["updated_at"]
    write_json(paths.last_mix, last)
    print(f"published {len(uris)} tracks → {config['playlist_uri']}")


def _age_days(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400.0


def mix_heard_progress(paths: Paths) -> dict:
    """How much of this week's mix has been heard, and may it be replaced yet.

    "Finished" is not "every track heard": one region-locked, removed, or
    relinked track would wedge maybe_refresh permanently. A mix also counts as
    finished once MIX_HEARD_RATIO of it is heard, or once it is simply old.
    """
    last = read_json(paths.last_mix, {})
    tracks = [t for t in (last.get("tracks") or []) if t.get("id")]
    mix_ids = {t["id"] for t in tracks}
    played = read_json(paths.played, {"plays": []})
    heard = _heard_ids(played) & mix_ids
    missing = [t for t in tracks if t["id"] not in heard]
    ratio = (len(heard) / len(mix_ids)) if mix_ids else 0.0
    age = _age_days(last.get("published_at"))

    reason = None
    if mix_ids:
        if not missing:
            reason = "all heard"
        elif ratio >= MIX_HEARD_RATIO:
            reason = f"{ratio:.0%} heard (>= {MIX_HEARD_RATIO:.0%})"
        elif age is not None and age >= MIX_MAX_AGE_DAYS:
            reason = f"published {age:.0f}d ago (>= {MIX_MAX_AGE_DAYS}d)"

    return {
        "mix_size": len(mix_ids),
        "heard": len(heard),
        "remaining": len(missing),
        "heard_ratio": ratio,
        "age_days": age,
        "finished": reason is not None,
        "finished_reason": reason,
        "published_at": last.get("published_at"),
        "playlist_id": last.get("playlist_id"),
        "missing_names": [t.get("name") for t in missing],
    }


def cmd_maybe_refresh(paths: Paths) -> int:
    """If this week's mix is used up, build and publish a replacement.

    Prints MIX_FINISHED then NEW_MIX so callers know to ping Zach.
    "Used up" is all-heard, mostly-heard, or simply old — see
    mix_heard_progress. With no mix at all, bootstraps one rather than
    no-opping forever.
    """
    prog = mix_heard_progress(paths)
    print(
        f"mix progress: {prog['heard']}/{prog['mix_size']} heard, "
        f"{prog['remaining']} remaining"
    )
    if not prog["mix_size"]:
        print("no current mix — building the first one")
    elif not prog["finished"]:
        print("mix still open")
        return 0
    else:
        print(f"MIX_FINISHED ({prog['finished_reason']})")
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
        # No 401 handling here: Spotify refreshes and retries internally, so a
        # 401 that reaches us is a real auth failure, not an expired token.
        current = sp.currently_playing()
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
        items = sp.recently_played(limit=50)
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
        # Keyed by title AND artist only. A title-alone key marks the wrong
        # track heard whenever two songs share a name ("Alive", "Home",
        # "Stay"), which permanently excludes a track you never played.
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
    # Deliberately NOT persisted: nothing ever read probe.json back, and the
    # caps self-disable after the first 4xx within a process anyway.


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
    ramp0 = [path_c_guessed_popularity(i, 55) for i in range(5)]
    check(all(ramp0[i] > ramp0[i + 1] for i in range(4)), "Path C search ranks are strictly decreasing")
    check(len(set(ramp0)) == len(ramp0), "Path C ranks never share a score")

    # ---------------------------------------------------------------- regressions
    # One per defect found in the 2026-09-02 review. Each of these failed
    # before its fix. Still no network: small fakes, no requests.Session use.

    class _Resp:
        def __init__(self, code: int, payload: Any = None) -> None:
            self.status_code = code
            self._p = payload
            self.text = json.dumps(payload or {})
            self.headers: dict = {}

        def json(self) -> Any:
            if self._p is None:
                raise ValueError("no json")
            return self._p

    class _Session:
        """Answers 401 until the Authorization header carries FRESH."""

        def __init__(self) -> None:
            self.headers: dict = {}
            self.calls = 0

        def request(self, method: str, url: str, **kw: Any) -> Any:
            self.calls += 1
            if self.headers.get("Authorization") == "Bearer FRESH":
                return _Resp(200, {"id": "u1"})
            return _Resp(401, {"error": {"status": 401}})

    # 1. An expired cached token self-heals on any entry point, once.
    minted = []
    sp = Spotify("STALE", refresh=lambda: (minted.append(1), "FRESH")[1])
    sp.http.s = _Session()
    sp._set_token("STALE")
    check(sp.me().get("id") == "u1", "401 is retried once with a refreshed token")
    check(len(minted) == 1, "a stale token mints exactly one replacement")

    sp_noref = Spotify("STALE")
    sp_noref.http.s = _Session()
    sp_noref._set_token("STALE")
    check(sp_noref.get_ok("/me")[0] == 401, "without credentials a 401 stays a 401")

    # 2. A thin rebuild cannot destroy the record of a good mix.
    with tempfile.TemporaryDirectory() as td:
        p = Paths(root=Path(td), state=Path(td) / "state")
        write_json(p.last_mix, {"tracks": [{"id": f"T{i}"} for i in range(40)]})
        mx = Mixer.__new__(Mixer)
        mx.paths, mx.cfg = p, MixConfig()
        thin = [Candidate("x", "spotify:track:x", "n", ["a"], ["ai"], 60, "s")]
        try:
            Mixer.persist_mix(mx, thin, "u1")
            check(False, "persist_mix refuses a materially smaller mix")
        except RuntimeError:
            check(True, "persist_mix refuses a materially smaller mix")
        Mixer.persist_mix(mx, thin, "u1", force=True)
        check(
            len(read_json(p.last_mix, {})["tracks"]) == 1,
            "persist_mix --force still overwrites",
        )

    # 3. maybe_refresh cannot be wedged by one unplayable track.
    with tempfile.TemporaryDirectory() as td:
        p = Paths(root=Path(td), state=Path(td) / "state")
        write_json(p.last_mix, {"tracks": [{"id": f"T{i}"} for i in range(40)]})
        write_json(p.played, {"plays": [{"track_id": f"T{i}"} for i in range(39)]})
        check(mix_heard_progress(p)["finished"], "39/40 heard counts as finished")
        write_json(p.played, {"plays": [{"track_id": "T0"}]})
        check(not mix_heard_progress(p)["finished"], "1/40 heard does not")
        old = (datetime.now(timezone.utc) - timedelta(days=MIX_MAX_AGE_DAYS + 1)).isoformat()
        write_json(
            p.last_mix,
            {"tracks": [{"id": f"T{i}"} for i in range(40)], "published_at": old},
        )
        check(mix_heard_progress(p)["finished"], "an old mix ages out even if unheard")

    # 4. Skipping a playlist for SEEDING still contributes its excludes.
    class _FakeSp:
        caps = SpotifyCaps()

        def created_playlists(self, uid: str) -> list[dict]:
            return [
                {"id": "kids", "name": "Kid A"},
                {"id": "good", "name": "Deep Cuts"},
                {"id": "mine", "name": "Weekly Mix"},
            ]

        def playlist_items(self, pid: str) -> list[dict]:
            return [{"id": f"{pid}-t", "artists": [{"id": f"{pid}-a", "name": f"{pid}Artist"}]}]

        def liked_tracks(self) -> list[dict]:
            return [{"id": "liked-t", "artists": [{"id": "liked-a", "name": "LikedArtist"}]}]

    with tempfile.TemporaryDirectory() as td:
        p = Paths(root=Path(td), state=Path(td) / "state")
        mx = Mixer.__new__(Mixer)
        mx.sp, mx.paths, mx.cfg = _FakeSp(), p, MixConfig(today=today)
        _, exclude, names = Mixer.collect_library(mx, "u1")
        check("kids-t" in exclude, "a kids playlist still contributes excludes")
        check("kidsArtist" not in names, "a kids playlist contributes no seeds")
        check("good-t" in exclude and "goodArtist" in names, "a normal playlist does both")
        check("mine-t" not in exclude, "our own output playlist is skipped entirely")
        check("liked-t" in exclude, "likes contribute excludes")

    # 5. A search-rank guess never outranks a real Spotify popularity score.
    #    Ranks stay distinct and below the gate (the old clamp-to-55 is gone).
    cfg55 = MixConfig(min_popularity=55)
    ramp = [path_c_guessed_popularity(i, cfg55.min_popularity) for i in range(10)]
    check(max(ramp) < cfg55.min_popularity, "Path C guesses stay below min_popularity")
    check(ramp[0] > ramp[1] > ramp[2], "top Path C ranks are not a flat tie")
    check(55 > max(ramp), "a Spotify-pop-55 track outranks any search guess")

    # 6. A cover by another artist is dropped, not substituted.
    class _SearchSp:
        caps = SpotifyCaps()

        def __init__(self, artists: list[str]) -> None:
            self._artists = artists

        def search_tracks(self, q: str, limit: int = 5) -> list[dict]:
            return [
                {
                    "id": "H",
                    "uri": "spotify:track:H",
                    "name": "Blue Monday",
                    "artists": [{"id": "a", "name": n}],
                }
                for n in self._artists
            ]

    mx = Mixer.__new__(Mixer)
    mx.sp, mx.cfg = _SearchSp(["Orgy"]), MixConfig()
    check(
        Mixer.resolve_track(mx, "Blue Monday", "New Order") is None,
        "resolve_track drops a hit by the wrong artist",
    )
    mx.sp = _SearchSp(["New Order"])
    check(
        (Mixer.resolve_track(mx, "Blue Monday", "New Order") or {}).get("id") == "H",
        "resolve_track still accepts the right artist",
    )

    # 8. The National Forest is not The National (primary-artist exact match).
    class _NationalSp:
        caps = SpotifyCaps()

        def search_tracks(self, q: str, limit: int = 5) -> list[dict]:
            if "Cascades" in q or "National Forest" in q:
                return [
                    {
                        "id": "NF",
                        "uri": "spotify:track:NF",
                        "name": "Queen Of The Cascades",
                        "artists": [{"id": "nf", "name": "The National Forest"}],
                    }
                ]
            return [
                {
                    "id": "TN",
                    "uri": "spotify:track:TN",
                    "name": "Bloodbuzz Ohio",
                    "artists": [{"id": "tn", "name": "The National"}],
                }
            ]

    mx = Mixer.__new__(Mixer)
    mx.sp, mx.cfg = _NationalSp(), MixConfig()
    check(
        Mixer.resolve_track(mx, "Queen Of The Cascades", "The National") is None,
        "resolve_track rejects The National Forest for The National",
    )
    check(
        (Mixer.resolve_track(mx, "Bloodbuzz Ohio", "The National") or {}).get("id") == "TN",
        "resolve_track still accepts The National as primary",
    )
    check(
        not primary_artist_matches("The National", "The National Forest"),
        "primary_artist_matches rejects a longer lookalike",
    )
    check(
        primary_artist_matches("Simon & Garfunkel", "Simon and Garfunkel"),
        "primary_artist_matches folds ampersand",
    )

    # 9. Sleep / rain mill titles never enter the pool.
    check(
        is_junk_discovery("Rain On Roof With Thunder", ["Pure Sleeping Vibes"]),
        "rain-on-roof + sleeping-vibes is junk",
    )
    check(
        is_junk_discovery("Weightless", ["Marconi Union"]) is False,
        "a normal title is not junk",
    )
    check(
        is_junk_discovery("Thunder", ["Imagine Dragons"]) is False,
        "standalone Thunder (real hit) is kept",
    )
    check(is_junk_discovery("432 Hz Meditation", ["Spa Yoga"]), "432 Hz / spa / yoga is junk")

    # 10. Path C guesses are capped and sort below measured tracks.
    rng = random.Random(0)
    measured_pool = [
        Candidate(f"M{i}", f"spotify:track:M{i}", f"m{i}", ["A"], [f"a{i}"], 80, "spotify-top:A", True)
        for i in range(15)
    ]
    guess_pool = [
        Candidate(
            f"C{i}",
            f"spotify:track:C{i}",
            f"c{i}",
            ["B"],
            [f"b{i}"],
            path_c_guessed_popularity(i % 3, 55),
            "spotify-search:B",
            False,
        )
        for i in range(40)
    ]
    picked = select_mix_tracks(
        measured_pool + guess_pool,
        size=40,
        max_per_artist=2,
        max_path_c=10,
        rng=rng,
    )
    n_guess = sum(1 for c in picked if not c.measured)
    check(len(picked) <= 40, "select_mix_tracks respects size")
    check(n_guess <= 10, "Path C guesses are capped at MIX_MAX_PATH_C")
    check(any(c.measured for c in picked), "measured tracks are preferred")
    check(picked[0].measured, "final order puts measured tracks first")

    # Path C is the Dev Mode happy path: a guess-only pool fills to size.
    only_c = select_mix_tracks(
        guess_pool,
        size=40,
        max_per_artist=2,
        max_path_c=10,
        rng=random.Random(1),
    )
    check(len(only_c) == 40, "Path-C-only mix fills to size")
    check(all(not c.measured for c in only_c), "Path-C-only pick is all guesses")
    check(len({c.popularity for c in only_c}) > 1, "Path-C-only mix is not a flat score")

    # 11. Seed harvest uses the primary artist only; short names are refused.
    class _FeatSp:
        caps = SpotifyCaps()

        def created_playlists(self, uid: str) -> list[dict]:
            return [{"id": "good", "name": "Deep Cuts"}]

        def playlist_items(self, pid: str) -> list[dict]:
            return [
                {
                    "id": "t1",
                    "artists": [
                        {"id": "main", "name": "Main Act"},
                        {"id": "feat", "name": "Madelyn Grant"},
                    ],
                },
                {
                    "id": "t2",
                    "artists": [
                        {"id": "main", "name": "Main Act"},
                        {"id": "feat2", "name": "Naomi Wild"},
                    ],
                },
                {
                    "id": "t3",
                    "artists": [{"id": "py", "name": "Py"}],
                },
            ]

        def liked_tracks(self) -> list[dict]:
            return []

    with tempfile.TemporaryDirectory() as td:
        p = Paths(root=Path(td), state=Path(td) / "state")
        mx = Mixer.__new__(Mixer)
        mx.sp, mx.paths, mx.cfg = _FeatSp(), p, MixConfig(today=today, use_likes=False)
        _, _exclude, feat_names = Mixer.collect_library(mx, "u1")
        check("Main Act" in feat_names, "primary artist is a seed")
        check("Madelyn Grant" not in feat_names, "featured guest is not a seed")
        check("Naomi Wild" not in feat_names, "remix-credit name is not a seed")
        check("Py" not in feat_names, "artist names shorter than 3 are not seeds")
        check(feat_names["Main Act"] == 2, "primary appearances are counted")
        qualified = qualify_seed_artists(feat_names, 2)
        check("Main Act" in qualified, "artist at min seed count is kept")
        one_off = Counter({"Main Act": 5, "One Hit": 1})
        check(
            "One Hit" not in qualify_seed_artists(one_off, 2),
            "one-off primary is dropped when min_seed_count=2",
        )

    check(not is_usable_artist_name("Py"), "Py is too short to seed or search")
    check(not is_usable_artist_name("DJ"), "blocked short credit words are refused")
    check(is_usable_artist_name("The National"), "a real artist name is usable")

    # 7. An unreadable playlist is loud, not silently empty.
    class _ForbiddenSession:
        def __init__(self) -> None:
            self.headers: dict = {}

        def request(self, method: str, url: str, **kw: Any) -> Any:
            return _Resp(403, {"error": {"status": 403}})

    sp403 = Spotify("t")
    sp403.http.s = _ForbiddenSession()
    try:
        sp403.playlist_items("p1")
        check(False, "playlist_items raises instead of returning []")
    except RuntimeError:
        check(True, "playlist_items raises instead of returning []")

    print("self_test failures:", failures)
    return 1 if failures else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Weekly Spotify discovery mixer (no live OAuth).")
    p.add_argument("--state-dir", default=str(DEFAULT_STATE))
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    sub = p.add_subparsers(dest="cmd", required=True)
    bld = sub.add_parser("build_mix", help="compute ~40 tracks and print them")
    bld.add_argument(
        "--force",
        action="store_true",
        help="overwrite last_mix.json even with a much smaller mix",
    )
    pub = sub.add_parser("publish", help="create or replace the Weekly Mix playlist")
    pub.add_argument("--dry-run", action="store_true", help="print URIs; do not touch Spotify playlists")
    pub.add_argument(
        "--force",
        action="store_true",
        help="replace the playlist even with a much smaller mix",
    )
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
        cmd_build_mix(paths, force=args.force)
        return 0
    if args.cmd == "publish":
        cmd_publish(paths, dry_run=args.dry_run, force=args.force)
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
