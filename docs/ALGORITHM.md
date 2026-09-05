# Weekly Mix algorithm

This is the business-logic record for `mix.py`. Product rules below are locked.
Do not invent opposing ones. Taste quality lives here; token/error hardening
lives in the mixer's HTTP and persist paths.

**Spotify only.** There is no Last.fm in the happy path. Do not require,
recommend, or document `LASTFM_API_KEY`. Similar artists come from
MusicBrainz + ListenBrainz (no key). Popular tracks come from Spotify search,
plus any Spotify endpoints this app still has (rare in Dev Mode 2026).

## Goal

About 40 **popular songs the user does not already have**.

"Popular" means a real Spotify popularity field when the app still has it,
otherwise a **search-rank** (earlier Spotify search hits beat later ones).
"New to the user" means not on any playlist they created, not a Liked Song,
and not in the local heard log (`state/played.json`).

## Non-goals

- Recreating Spotify Radio, Discover Weekly, or `GET /recommendations`.
- Calling Last.fm (or any other scrobble API) for similar artists or
  listener counts.
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
        |
        |- EXCLUDE: every track (except the Weekly Mix output playlist itself)
        |- skip seeding: seasonal (wrong month), baby/kids/nursery,
        |                "house listening", the Weekly Mix playlist
        +- SEED: primary artist on each remaining track
                 (drop names shorter than 3 chars / blocked words)
                 (drop artists with fewer than MIX_MIN_SEED_COUNT appearances,
                  unless that would empty the set)
                |
                v
     similar artists, in order:
       1. MusicBrainz name -> MBID
          + ListenBrainz labs similar-artists (no key)
       2. Spotify related-artists (only if this app still has it)
                |
                v
     those artists' popular tracks:
       1. Spotify /artists/{id}/top-tracks (extended quota only)
       2. Spotify search artist:"Name" type=track  (Dev Mode happy path)
                |
                v
     resolve onto Spotify (primary artist must match; junk titles dropped)
     drop if in exclude set / likes / played.json
     drop measured tracks below MIX_MIN_POPULARITY
     Path C guesses skip that gate and keep distinct search-rank scores
     cap MIX_MAX_PER_ARTIST, ~MIX_SIZE tracks, week-stable RNG
     MIX_MAX_PATH_C only when real Spotify-popularity tracks are also in the pool
                |
                v
     last_mix.json -> publish replaces the Weekly Mix playlist
```

## Path B / Path C (Spotify-first popularity)

Spotify Dev Mode (2026) has no `/recommendations`, no related-artists, no
artist top-tracks, and no `popularity` field for many apps. Discovery is
still Spotify-first: search for tracks, ListenBrainz only for similar names.

| Path | Source | Popularity | `measured` |
|---|---|---|---|
| B | Spotify `/artists/{id}/top-tracks` (extended quota only) | Spotify pop, or 60 if the endpoint exists but the field is missing | yes |
| C | Spotify `search` `artist:"Name"` | Spotify pop if present; else a **search-rank soft score** | yes if pop field; **no** if guessed |

There is no Path A. Last.fm `artist.getTopTracks` / `track.getInfo` /
`artist.getSimilar` are not called.

### Why Path C alone produced a flat-55 mix

The old guess formula was:

```
min(min_popularity, max(40, 80 - 3 * rank))
```

With `MIX_MIN_POPULARITY=55`, rank 0 is `min(55, 80) = 55`. Ranks 1-8 also
clamp to 55. Every Path C track that cleared the gate **tied at 55**.

Dev Mode has no Last.fm and no Spotify popularity field, so **every**
candidate was Path C. The weighted picker treated 40 interchangeable 55s as
equal. The live mix was 40 tracks, all `popularity: 55`, all
`source: spotify-search:<Artist>`, including ambient lookalikes from bad
artist resolves.

### How the Spotify-only ranking and filters fix that

1. **Distinct search-rank scores.** Rank 0 is `min_popularity - 1`, rank 1
   is `min_popularity - 3`, and so on. Hits never share a score. A real
   Spotify popularity of 55 outranks every guess.
2. **Path C guesses do not use the 55 gate.** That gate flattened them.
   Eligibility is primary-artist match, junk title/artist filter, usable
   name, and top `PATH_C_MAX_RANK` (3) search hits per artist.
3. **Path C is the happy path when nothing is measured.** A guess-only pool
   fills to `MIX_SIZE`. `MIX_MAX_PATH_C` (default 10) only caps guesses
   when real Spotify-popularity tracks are already in the mix.
4. **Prefer artists that yielded measured tracks** (Path B, or Path C with
   a popularity field). Do not also take their guessed fillers.
5. **Stricter resolve, junk filter, primary-only seeds, short-name block**
   so search cannot substitute The National Forest or Pure Sleeping Vibes.

## Resolve rules

`resolve_track(title, artist)` may return None. It must not return a track by
a different artist.

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
  - length `< 3` (`Py` -> "Rain On Roof With Thunder" / Pure Sleeping Vibes)
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
- heard ratio >= `MIX_HEARD_RATIO` (0.9, i.e. 36/40). "40/40 heard" is the
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
| Bad artist resolve | Seed `The National` -> "The National Forest"; seed `Py` -> rain/sleep | Primary-artist match + min name length |
| Remix-credit seed pollution | Feature names become seeds and drag in the wrong similar-artists | Primary-only harvest + `MIX_MIN_SEED_COUNT` |
| Sleep-track leakage | Ambient/rain/spa titles in a "popular discovery" mix | Junk filter + stricter resolve + search-rank |
| ListenBrainz miss | Few similar artists; fallback to seed artists' own unheard hits | Normal; MusicBrainz rate limit is ~1 req/s |
| Path C lookalikes | Search returns the wrong act | Primary-artist match; still possible if Spotify's first hit is wrong |
| Dev Mode quota | 429 on search / playlist reads | Thin builds are refused; re-run when quota recovers |
| Unreadable playlist | Used to look empty and drop excludes | `playlist_items` now raises on non-404 errors |

## Knobs (`.env.example`)

MusicBrainz and ListenBrainz need no key and are not configured.

| Variable | Default | Role |
|---|---|---|
| `MIX_PLAYLIST_NAME` | `Weekly Mix` | Output playlist; never a seed; its tracks are not excludes |
| `MIX_SIZE` | `40` | Target length |
| `MIX_MIN_POPULARITY` | `55` | Gate for **measured** Spotify popularity only. Path C guesses stay below it |
| `MIX_MAX_PER_ARTIST` | `2` | Diversity cap on the final mix |
| `MIX_USE_LIKES` | `1` | Likes always exclude; this only controls artist seeding |
| `MIX_MIN_SEED_COUNT` | `2` | Minimum primary-artist appearances before seeding |
| `MIX_MAX_PATH_C` | `10` | Max Path C guesses **when measured tracks exist**. Ignored for a guess-only mix |

`PATH_C_MAX_RANK` (3) is a code constant, not an env var: only the first three
search hits per artist may become Path C candidates.
