# Weekly Mix algorithm

Business-logic record for `mix.py`. Taste quality lives in the Mixer;
token and error hardening live in the HTTP and persist paths.

**Spotify only for product rules.** Rolling does not add Last.fm (or any
other scrobble API). Replacements use the same `Mixer.build` pipeline as
`build_mix`: created-playlist / likes / played-log excludes, junk filters,
primary-only seeds, and Path C search-rank rules as implemented in `mix.py`.

## Goal

About 40 **popular songs the user does not already have**, kept current
during the week by rolling heard tracks off the live playlist.

## Rolling playlist

The live Weekly Mix is a sliding window, not a frozen Monday dump.

1. Play detection (`ingest_ui`, `log_plays --recent-only`, `watch_plays`)
   writes a row to `state/played.json` with `track_id` and `played_at`.
2. `mix.py roll_playlist` (alias `roll`) reads `state/config.json`
   `playlist_id`, `state/played.json`, `state/last_mix.json`, and the
   current Spotify playlist items (`GET /playlists/{id}/items`, fallback
   `/tracks`).
3. A playlist track is **eligible to remove** only when its id is in the
   heard set **and** its `played_at` (or `ts` / `heard_at`) is at least
   `MIX_ROLL_DELAY_MIN` minutes ago (default **5**). A track heard more
   recently stays on the playlist. A heard row with no parseable timestamp
   is treated as old enough.
4. Remaining tracks (unheard, or heard but still inside the delay) keep
   their current relative order and stay at the **top**.
5. Need `MIX_SIZE` (default 40) minus `len(remaining)` new tracks. Build
   them with `Mixer.build` and these extra excludes:
   - created playlists, likes, played log (Mixer already)
   - track ids still remaining on the playlist
   - track ids already in `last_mix.json` (do not immediately re-add a
     song that just rolled off)
6. Write the playlist with **one** `replace_playlist_tracks` as
   `remaining_uris + new_uris`. Next-up songs stay at the top. New
   discoveries land at the bottom.
7. Rewrite `last_mix.json` `tracks` to match the new playlist. Keep week
   and `published_at` from the Monday publish. Update `config.track_count`.
8. Print a quiet summary: `ROLL removed=N kept=K added=A size=S`.
   If nothing is eligible to remove and the playlist is already about
   `MIX_SIZE`, print `ROLL noop` and exit 0.

`roll_playlist` does **not** ping the user and does **not** call
currently-playing in a loop. Detection is a separate step.

### Delay plus top-up

```
heard now  ->  stay on playlist for MIX_ROLL_DELAY_MIN
               (so a skip / mis-detect can still be sitting there)
         ->  after the delay, drop it
         ->  keep the rest in order at the top
         ->  append Mixer discoveries until length ~= MIX_SIZE
```

Example: playlist of 40, one track heard 8 minutes ago, delay 5:
remove 1, keep 39, add 1 at the bottom, size 40.

### Hourly pipeline

Skip-watcher / play-log should run, in order:

```
ingest_ui → log_plays --recent-only → roll_playlist → maybe_refresh
```

`maybe_refresh` is less central once rolling is in place. Keep it for a
**full rebuild** when the mix is empty or older than `MIX_MAX_AGE_DAYS`.

Monday `publish` is still a full overwrite (fresh week). Rolling is the
mid-week path.

## Discovery (reused by roll)

`roll_playlist` does not invent a second recommender. It calls
`Mixer.build` with a smaller `target_size` and extra exclude ids.

```
created playlists (owner == the user)
        |
        |- EXCLUDE: every track (except the Weekly Mix output playlist)
        |- skip seeding: seasonal (wrong month), baby/kids/nursery,
        |                "house listening", the Weekly Mix playlist
        +- SEED: artists on remaining tracks
                |
                v
     similar artists (MusicBrainz + ListenBrainz, then Spotify
     related-artists only if this app still has it)
                |
                v
     those artists' popular tracks (Spotify top-tracks if present,
     else Spotify search / Path C)
                |
                v
     drop exclude set, likes, played.json, remaining playlist ids,
     last_mix ids
     cap MIX_MAX_PER_ARTIST, fill `need` tracks, week-stable RNG
```

Never seeded from `GET /me/top` or recently-played. Recently-played is
only for `log_plays` to mark mix tracks heard.

## Mid-week `maybe_refresh`

A mix is "used up" (build + publish a replacement) when any of these is
true:

- every remaining mix track is in the heard log
- heard ratio >= `MIX_HEARD_RATIO` (0.9)
- the mix is older than `MIX_MAX_AGE_DAYS` (14), even if unheard

If there is no current mix, `maybe_refresh` bootstraps one.

`persist_mix` and `publish` refuse to replace a good mix with a much
smaller one (`MIX_MIN_REPLACE_RATIO` = 0.6) unless `--force`. Rolling
does not use that floor: a one-track top-up is the intended write.

## Knobs

| Variable | Default | Role |
|---|---|---|
| `MIX_PLAYLIST_NAME` | `Weekly Mix` | Output playlist |
| `MIX_SIZE` | `40` | Target length after each roll |
| `MIX_MIN_POPULARITY` | `55` | Mixer popularity gate |
| `MIX_MAX_PER_ARTIST` | `2` | Diversity cap |
| `MIX_USE_LIKES` | `1` | Likes always exclude; this only controls artist seeding |
| `MIX_ROLL_DELAY_MIN` | `5` | Minutes a heard track stays on the playlist before removal |
