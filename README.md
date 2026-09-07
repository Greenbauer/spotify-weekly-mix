# Weekly Spotify discovery mixer

A ~40-track **novel + popular** Weekly Mix for a personal Spotify account (Grok Bot / local agent).
`mix.py` never starts OAuth by itself; `oauth.py` is the one-time, opt-in helper that does
(see [Setup](#what-we-still-need-from-you)). Registering the Spotify app is still on you.

Business-logic decisions (seeds, excludes, Path B/C, failure modes) are in
[docs/ALGORITHM.md](docs/ALGORITHM.md). Spotify-first: no Last.fm. Read that
before changing taste.

```
.venv/bin/python mix.py self_test     # local filters, no network
.venv/bin/python mix.py build_mix     # compute 40 tracks (needs tokens)
.venv/bin/python mix.py publish       # create/replace "Weekly Mix"
.venv/bin/python mix.py publish --dry-run
.venv/bin/python mix.py log_plays     # record plays of OUR playlist only
.venv/bin/python mix.py probe         # which Spotify endpoints still work
.venv/bin/python mix.py ingest_ui     # mark mix tracks from a now-playing JSONL
.venv/bin/python mix.py maybe_refresh # if the current mix is used up, publish a new one
.venv/bin/python mix.py watch_plays   # adaptive now-playing poll so short skips count
```

`build_mix` and `publish` take `--force` to overwrite a mix much smaller than the one it
replaces. Without it they refuse, so a rate-limited build cannot destroy a good mix.

## API reality (August 2026)

Two Spotify lock-downs stacked:

### 1. 27 Nov 2024 — discovery endpoints killed for new apps

[Official post](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api).
New apps and Dev Mode apps without extended quota get **403** on:

| Endpoint | Status for a new app |
|---|---|
| `GET /v1/recommendations` | dead |
| `GET /v1/artists/{id}/related-artists` | dead |
| `GET /v1/audio-features`, `/audio-analysis` | dead |
| Featured / category playlists, 30s previews in multi-get | dead |

Grandfathered **extended quota** apps that already used these still have them.
Individuals generally cannot apply for extended quota anymore (org + 250k MAU bar since 2025).

### 2. Feb / Mar 2026 — Dev Mode surface shrinks further

[Changelog](https://developer.spotify.com/documentation/web-api/references/changes/february-2026)
and [migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide).
**Extended quota apps are unchanged.** Dev Mode apps (what a personal mixer will be):

Still works (and this script uses):

- `GET /me`, `GET /me/playlists`, `POST /me/playlists`, `PUT /playlists/{id}`
- Playlist items: `GET/PUT/POST/DELETE /playlists/{id}/items` (**`/tracks` renamed**)
- Liked songs: `GET /me/tracks`
- Recently played: `GET /me/player/recently-played` (log_plays only)
- Search: `GET /search` (**max limit 10**)
- `GET /artists/{id}`, `GET /artists/{id}/albums`, `GET /albums/{id}/tracks`, `GET /tracks/{id}`
- `GET /me/top/{type}` — **available, but this mixer NEVER calls it**

Removed or stripped in Dev Mode:

- `GET /artists/{id}/top-tracks` — **removed**
- Batch `GET /tracks?ids=`, `GET /artists?ids=` — **removed** (fetch one-by-one)
- `POST /users/{id}/playlists`, `GET /users/{id}/playlists` — use `/me/playlists`
- Track/artist **`popularity` field removed**
- Browse new-releases / categories

Dev Mode also: app owner must have **Premium**, **5 users** per app. Client IDs per
developer raised to 25 in July 2026.

`mix.py probe` reports which of related-artists / recommendations / top-tracks /
popularity still work for *your* token. It only prints: the mixer feature-detects the
same endpoints per run and disables them after the first 4xx, so there is nothing to persist.

## Discovery method (no deprecated recs endpoint)

```
playlists you CREATED  (+ optional Liked Songs as artist seeds)
        │
        ├─ exclude: seasonal (wrong month), baby/kids/nursery, "Weekly Mix"
        ├─ those tracks → EXCLUDE set
        └─ those artists → SEED set
                │
                ▼
     similar artists, in order:
       1. MusicBrainz name → MBID
          + ListenBrainz labs similar-artists (no key)
       2. Spotify related-artists             (only if probe says it still works)
                │
                ▼
     those artists' popular tracks:
       1. Spotify /artists/{id}/top-tracks            (extended quota only)
       2. Spotify search artist:"Name" type=track     (Dev Mode happy path;
          search rank is a soft score, not a flat 55)
                │
                ▼
     drop if in created playlists, likes, or state/played.json
     drop sleep/ambient/rain-mill titles; primary Spotify artist must match
     keep measured Spotify pop >= 55; Path C guesses stay below that and stay distinct
     cap 2 tracks / artist, ~40 tracks, week-stable RNG
```

## Quality

Filters added after a live mix came back as 40 Path C tracks, all popularity 55.
Discovery is Spotify + ListenBrainz. Last.fm is not used.

- **Primary-artist resolve.** `The National Forest` is not `The National`. Featured
  guests are ignored when matching.
- **Junk titles/artists.** Sleep, ambient mills, white noise, rain-on-roof,
  thunder-sounds, spa/massage/yoga/meditation, 432 Hz, lofi study. Standalone
  hit titles like `Thunder` stay.
- **Primary-only seeds.** Remix-credit names do not expand similar-artists.
  `MIX_MIN_SEED_COUNT` (default 2) drops one-off primaries.
- **Short names refused.** Length `< 3` (e.g. `Py`) and blocked words are not
  seeded or searched.
- **Path C ranking.** Search hits get distinct scores from their rank (never
  a flat 55). Real Spotify popularity, when present, sorts above guesses.
  `MIX_MAX_PATH_C` caps guesses only when measured tracks are also in the pool.

**Never seeded from:** `GET /me/top`, recently-played, baby/house listening,
out-of-season holiday playlists, Spotify's own editorial lists.

**Excluded from output regardless:** every track in every playlist you created — including
the ones skipped for seeding — plus Liked Songs and the play log. Skipping a playlist as a
taste source never makes its tracks eligible to be recommended back to you. The only total
skip is the Weekly Mix itself, so an unheard track from last week can still return.

Season from **playlist name** (skipped as seeds unless the current month matches):

| Name matches | In-season |
|---|---|
| Christmas / Xmas / holiday | December |
| Halloween | October |
| Thanksgiving | 15–30 November |
| 4th of July | 25 Jun – 10 Jul |
| Valentine | February |

## Env vars

Copy `.env.example` to `.env` (gitignored).

| Variable | Required | Purpose |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | yes (for live run) | Dashboard app |
| `SPOTIFY_CLIENT_SECRET` | yes | Dashboard secret |
| `SPOTIFY_REFRESH_TOKEN` | yes | Headless token; script refreshes access |
| `SPOTIFY_ACCESS_TOKEN` | no | Skip refresh if still valid |
| `MIX_PLAYLIST_NAME` | no | default `Weekly Mix` |
| `MIX_SIZE` | no | default `40` |
| `MIX_MIN_POPULARITY` | no | default `55` |
| `MIX_MAX_PER_ARTIST` | no | default `2` |
| `MIX_USE_LIKES` | no | default `1`. Likes are ALWAYS excluded from output; this only controls whether their artists also seed. |
| `MIX_MIN_SEED_COUNT` | no | default `2`. Primary artist must appear this many times before seeding. |
| `MIX_MAX_PATH_C` | no | default `10`. Max Path C guesses when measured Spotify-pop tracks exist. Ignored for a guess-only mix. |

## Local state (`state/`)

| File | Written by | Contents |
|---|---|---|
| `config.json` | `publish` | mix playlist id + uri |
| `last_mix.json` | `build_mix` | the 40 tracks + week stamp |
| `played.json` | `log_plays` | track ids played *from our playlist URI* |
| `similar_cache.json` | `build_mix` | ListenBrainz similar-artist cache (14d) |
| `nowplaying.jsonl` | `scripts/nowplaying_*.py` | web-player now-playing log, read by `ingest_ui` |
| `watch_plays.pid` | `watch_plays` | pid of the running watcher |

JSON under `state/` is gitignored except `.gitkeep`.

## What we still need from you

1. **Spotify developer app** at https://developer.spotify.com/dashboard
   (you own it; Premium required for Dev Mode). One redirect URI e.g.
   `http://127.0.0.1:8080/callback`.
2. **OAuth once.** Easiest: `.venv/bin/python oauth.py` — it serves the callback on
   `http://127.0.0.1:8080/callback`, exchanges the code, and writes `SPOTIFY_REFRESH_TOKEN`
   into `.env` with mode 600. It prints the URL to `/tmp/spotify-auth-url.txt`.

   To do it by hand instead, these are the scopes:

   `playlist-read-private playlist-read-collaborative user-library-read playlist-modify-private playlist-modify-public user-read-recently-played user-read-currently-playing user-read-playback-state user-read-private`

   Print the URL (does not open a browser or start a server):

   ```
   .venv/bin/python mix.py print_auth_url --redirect-uri http://127.0.0.1:8080/callback
   ```

   Exchange the `code` yourself:

   ```
   curl -u "$SPOTIFY_CLIENT_ID:$SPOTIFY_CLIENT_SECRET" -d grant_type=authorization_code \
     -d code=THE_CODE -d redirect_uri=http://127.0.0.1:8080/callback \
     https://accounts.spotify.com/api/token
   ```

   Put `refresh_token` in `.env` as `SPOTIFY_REFRESH_TOKEN`.
3. After first `publish`, run `log_plays` often (recently-played is a short window)
   so heard mix tracks stay out of next week's pool.

Similar artists use MusicBrainz + ListenBrainz (no key). Last.fm is not used.

## Layout

```
mix.py            the mixer CLI
oauth.py          one-time OAuth helper (writes .env, mode 600)
docs/ALGORITHM.md business logic (seeds, excludes, Path A/B/C)
requirements.txt .env.example
scripts/          optional web-player skip helpers
state/            local only (gitignored JSON)
```

Do not commit `.env`, play logs, playlist ids, or listening history.
