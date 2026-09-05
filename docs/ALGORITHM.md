# Weekly Mix algorithm

This is the business-logic record for `mix.py`. Product rules below are locked.
Do not invent opposing ones. Taste quality lives here; token/error hardening
lives in the mixer's HTTP and persist paths.

## Goal

About 40 **popular songs the user does not already have**.

"Popular" means a real audience signal (Last.fm listeners, or Spotify
popularity when the app still has that field). "New to the user" means not on
any playlist they created, not a Liked Song, and not in the local heard log
(`state/played.json`).

## Non-goals

- Recreating Spotify Radio, Discover Weekly, or `GET /recommendations`.
- Seeding from what they played recently (baby/house listening pollutes that).
- Publishing tracks they already saved, even from playlists we refuse to seed.
- Using featured-guest or remix-credit names as taste.
- Filling the mix with sleep/ambient/white-noise catalog when a search misses.

## Why we never seed from `/me/top` or recently-played

`GET /me/top` and recently-played are **listening history**, not library taste.
This account's history includes baby/kids audio and house-listening background
play. Those endpoints would expand similar-artist graphs from that pollution
and the mix would sound like the last week's speakers, not the playlists they
chose to keep.

Recently-played is used only by `log_plays` / `watch_plays` to mark Weekly Mix
tracks as heard. It is never a seed.

Liked Songs are **excludes always**. They become artist seeds only when
`MIX_USE_LIKES=1` (default). They are never output.

## Pipeline

```
created playlists (owner == the user)
        │
        ├─ EXCLUDE: every track (except the Weekly Mix output playlist itself)
        ├─ skip seeding: seasonal (wrong month), baby/kids/nursery,
        │                "house listening", the Weekly Mix playlist
        └─ SEED: primary artist on each remaining track
                 (drop names shorter than 3 chars / blocked words)
                 (drop artists with fewer than MIX_MIN_SEED_COUNT appearances,
                  unless that would empty the set)
                │
                ▼
     similar artists, in order:
       1. Last.fm artist.getSimilar          (if LASTFM_API_KEY)
       2. MusicBrainz name → MBID
          + ListenBrainz labs similar-artists (no key)
       3. Spotify related-artists             (only if this app still has it)
                │
                ▼
     those artists' popular tracks (Path A, then B, then C)
                │
                ▼
     resolve onto Spotify (primary artist must match; junk titles dropped)
     drop if in exclude set / likes / played.json
     drop below MIX_MIN_POPULARITY
     prefer Path A/B artists; cap Path C guesses (MIX_MAX_PATH_C)
     cap MIX_MAX_PER_ARTIST, ~MIX_SIZE tracks, week-stable RNG
                │
                ▼
     last_mix.json → publish replaces the Weekly Mix playlist
```

## Path A / B / C (popularity proxies)

Spotify Dev Mode (2026) has no `/recommendations`, no related-artists, no
artist top-tracks, and no `popularity` field for many apps. Discovery therefore
uses Last.fm / MusicBrainz+ListenBrainz plus Spotify search.

| Path | Source | Popularity | `measured` |
|---|---|---|---|
| A | Last.fm `artist.getTopTracks` → Spotify resolve | `15 * log10(listeners+1)` (~3.2k ≈ 55, 100k ≈ 75, 1M ≈ 90), or Spotify pop if present | yes |
| B | Spotify `/artists/{id}/top-tracks` (extended quota only) | Spotify pop, or 60 if the endpoint exists but the field is missing | yes |
| C | Spotify `search` `artist:"Name"` | Spotify pop if present; else Last.fm `track.getInfo` listeners when a key is set; else a **search-rank guess** | yes if measured; **no** if guessed |

### Why Path C alone produced a flat-55 mix

The guess formula is:

```
min(min_popularity, max(40, 80 - 3 * rank))
```

With `MIX_MIN_POPULARITY=55`, rank 0 is `min(55, 80) = 55`. Ranks 1-8 also
clamp to 55. Every Path C track that cleared the gate **tied at 55**.

When Path A resolve failed (wrong-artist hits, missing Last.fm key, or search
returning ambient lookalikes), **every** candidate was Path C. The weighted
picker then treated 40 interchangeable 55s as equal, and the live mix was
40 tracks, all `popularity: 55`, all `source: spotify-search:<Artist>`.

### What we do about that

1. **Prefer artists that yielded Path A/B** (or Last.fm-verified Path C).
   Path-C-only artists are consulted only if the measured pool is thin.
2. If an artist already has measured tracks, do not also take their guessed
   Path C fillers.
3. Path C search only keeps the top `PATH_C_MAX_RANK` (3) hits, and the
   primary Spotify artist must match the requested name.
4. When `LASTFM_API_KEY` is set, a Path C hit without Spotify popularity
   must get Last.fm `track.getInfo` listeners or it is dropped. A successful
   getInfo is measured (`source: lastfm-info:<Artist>`), not a guess.
5. Guesses stay at or below the popularity gate (so a Last.fm-less run can
   still fill) but they **sort below** any measured track and at most
   `MIX_MAX_PATH_C` (default 10) may appear in the final 40.

## Resolve rules

`resolve_track(title, artist)` may return None. It must not return a track by
a different artist: the caller would score it with the *requested* artist's
Last.fm listeners.

- Normalize case, punctuation, and `&` / `and`.
- The **primary** (first) Spotify artist must match the requested name.
- Feat./ft./with suffixes on the primary are stripped, so
  `The National feat. X` still matches `The National`.
- Extra words that are not a featuring credit are a different artist:
  `The National Forest` does **not** match `The National`.
- Prefer exact title, then startswith, never "artist name contained in a
  longer artist name".
- Refuse to resolve if the requested artist name is unusable (below).

## Junk title / artist filter

Rejected at candidate creation and again before the final pick (case
insensitive). Aimed at sleep/ambient mills, not every song with the word
"rain" or "thunder" in the title (`Thunder` by Imagine Dragons is kept;
`Rain On Roof With Thunder` / Pure Sleeping Vibes is not).

Patterns include: white/brown/pink noise; sleep/sleeping music/sounds/vibes;
pure sleeping; lullaby; spa; massage; yoga; meditation; 432 Hz; lofi study /
lofi beats; rain on roof / rain sounds / rain and thunder; thunder sounds;
soothing (and soothing + sleep/rain/ambient); nature sounds; ambient
sleep/music/rain.

## Seed artist quality

- Only the **first / primary** credited artist is counted. Featured guests
  and remix-credit names (Madelyn Grant, Naomi Wild, Shy Girls on a
  someone-else track) do not seed and do not expand similar-artists.
- `MIX_MIN_SEED_COUNT` (default 2): an artist must appear as primary at least
  N times across seed playlists (and likes, if enabled). One-off remix
  credits that somehow became primary still cannot dominate. If every artist
  is a one-off, the filter is skipped so the mix can still build.
- Unusable names are never seeded or searched:
  - length `< 3` (`Py` → "Rain On Roof With Thunder" / Pure Sleeping Vibes)
  - blocked common words / credit leftovers (`the`, `dj`, `remix`, ...)

## Exclusion rules

| Source | Seeds? | Excludes from output? |
|---|---|---|
| Created playlist, normal name | yes (primary artists) | yes, every track |
| Created playlist, baby/kids/nursery / house listening | no | yes, every track |
| Created playlist, seasonal name, out of season | no | yes, every track |
| Created playlist, seasonal name, in season | yes | yes, every track |
| Weekly Mix output playlist | no | **no** (unheard tracks may return) |
| Liked Songs | only if `MIX_USE_LIKES=1` | **always** |
| `state/played.json` (heard log) | no | yes |
| Recently-played / `/me/top` | **never** | no (except mix tracks logged as heard) |

Season from **playlist name** (skipped as seeds unless the current month matches):

| Name matches | In-season |
|---|---|
| Christmas / Xmas / holiday | December |
| Halloween | October |
| Thanksgiving | 15-30 November |
| 4th of July | 25 Jun - 10 Jul |
| Valentine | February |

## Mid-week `maybe_refresh`

A mix is "used up" (build + publish a replacement) when any of these is true:

- every remaining mix track is in the heard log
- heard ratio ≥ `MIX_HEARD_RATIO` (0.9, i.e. 36/40). "40/40 heard" is the
  intended happy path; one region-locked or relinked track must not wedge
  refresh forever
- the mix is older than `MIX_MAX_AGE_DAYS` (14), even if unheard

If there is no current mix at all, `maybe_refresh` bootstraps one.

`persist_mix` and `publish` refuse to replace a good mix with a much smaller
one (`MIX_MIN_REPLACE_RATIO` = 0.6) unless `--force`. A rate-limited build
must not destroy `last_mix.json`.

## Known failure modes

| Failure | What you see | What to do |
|---|---|---|
| Bad artist resolve | Seed `The National` → "The National Forest"; seed `Py` → rain/sleep | Primary-artist match + min name length; still possible if Last.fm autocorrects to a mill |
| Remix-credit seed pollution | Feature names become seeds and drag in the wrong similar-artists | Primary-only harvest + `MIX_MIN_SEED_COUNT` |
| Sleep-track leakage | Ambient/rain/spa titles in a "popular discovery" mix | Junk filter + stricter resolve + Path C cap |
| Missing `LASTFM_API_KEY` | Similar-artists via ListenBrainz only; top tracks via search; Path C guesses | Set a free Last.fm key. Without it, Path C is allowed but capped at 10 |
| Path A titles do not resolve | Last.fm names differ from Spotify (remasters / feat. suffixes) | Fallback unquoted search; still requires primary-artist match |
| Dev Mode quota | 429 on search / playlist reads | Thin builds are refused; re-run when quota recovers |
| Unreadable playlist | Used to look empty and drop excludes | `playlist_items` now raises on non-404 errors |

## Knobs (`.env.example`)

| Variable | Default | Role |
|---|---|---|
| `LASTFM_API_KEY` | unset | Similar + top-tracks + Path C `track.getInfo`. Strongly recommended. |
| `MIX_PLAYLIST_NAME` | `Weekly Mix` | Output playlist; never a seed; its tracks are not excludes |
| `MIX_SIZE` | `40` | Target length |
| `MIX_MIN_POPULARITY` | `55` | Gate. Path C guesses may equal this, never exceed it |
| `MIX_MAX_PER_ARTIST` | `2` | Diversity cap on the final mix |
| `MIX_USE_LIKES` | `1` | Likes always exclude; this only controls artist seeding |
| `MIX_MIN_SEED_COUNT` | `2` | Minimum primary-artist appearances before seeding |
| `MIX_MAX_PATH_C` | `10` | Max Path C **guesses** in the final mix |

`PATH_C_MAX_RANK` (3) is a code constant, not an env var: only the first three
search hits per artist may become Path C candidates.
