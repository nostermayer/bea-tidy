# bea-tidy — Claude Code Context

## What this project is

bea-tidy is an AI-powered Plex media library organizer running on a TrueNAS home server.
It uses the Google Gemini API to classify, rename, and sort media files into Plex-standard
folder structures automatically.

Named after Beatrix (Bea), Nick's wife.

GitHub: https://github.com/nostermayer/bea-tidy

---

## Architecture

```
Syncthing (receive-only) -> /mnt/tank/sync
        |
        | smart-sync.sh (cron every 30min)
        v
/mnt/tank/media/Sync
        |
        | organizer Docker container (cron at :15 and :45)
        v
/mnt/tank/media/
  Movies/
    Title (Year)/
      Title (Year).mp4
  TV/
    Show Name/
      Season 01/
        Show Name - S01E01.mkv
  Anime Movies/
  Anime Series/
```

---

## Components

### `lib.py` — Shared library
- Gemini client setup, all prompt instructions, shared helpers
- Imported by both organizer.py and cleanup.py
- Loads `.env` automatically via python-dotenv if present

### `organizer/` — Continuous sync watcher
- Runs as a Docker container triggered by cron every 30 minutes (offset by 15 min from sync)
- Watches `$SYNC_DIR` for new files/folders
- Classifies each file using Gemini into Movies / TV / Anime Movies / Anime Series
- Copies files into correct Plex-standard paths under `$BASE_MEDIA_DIR`
- Tracks processed files individually (not folders) to handle partial transfers
- Handles: bare files, single-episode folders, multi-episode folders (e.g. full seasons)
- Skips: 0-byte files, Syncthing `.tmp` files, sample/trailer files (Gemini-detected)
- Rollback: if any file in a multi-episode folder fails, successfully copied files are removed
- Summary: logs `Processed: X | Skipped: Y | Failed: Z` at end of each run
- Notifications: sends run summary to Discord and/or ntfy if configured

### `cleanup/` — One-shot library cleanup
- Run manually to fix an existing badly-named library
- Dry run by default — shows what would change without touching anything
- `--execute` flag applies changes, with a 5-second countdown abort window
- `--category Movies` to limit scope
- `--path /path/to/folder` to target a specific subfolder
- Every log line prefixed with `[DRY RUN]` or `[EXECUTE]`

### `scripts/smart-sync.sh` — Smart rsync wrapper
- Snapshots file mtimes before running rsync
- Skips rsync entirely if nothing has changed (saves I/O on idle periods)
- Guards against empty-source wipe (aborts if SYNC_SRC appears empty)
- Uses flock to prevent overlapping runs
- Snapshot stored persistently in SYNC_DST (survives reboots)
- Run every 30 minutes via TrueNAS cron

---

## TMDB enrichment

After every Gemini classification, `tmdb_enrich(ideal, confidence)` in `lib.py` is called:

1. Parses the Gemini-produced Plex path to extract title, year, and category.
2. Searches the TMDB API (`/search/movie` or `/search/tv`) for the title.
3. If found: rebuilds the path with TMDB's canonical title and year, upgrades confidence to HIGH.
4. If not found: returns the Gemini result unchanged.
5. Silently skips if `TMDB_API_KEY` is not set — fully optional.

This runs **before** the LOW-confidence check, so a TMDB-confirmed result is never
incorrectly sent to `_review_needed.txt`. Uses stdlib `urllib` — no extra dependency.

---

## Gemini API usage

Three types of Gemini calls in lib.py / organizer.py:

1. **`ask_gemini_classify(client, filename)`** — classifies a filename into a full Plex path
   e.g. `jersey.shore.s01e01.dvdrip.avi` → `TV/Jersey Shore/Season 01/Jersey Shore - S01E01.avi`

2. **`ask_gemini_match_folder(client, title, existing_folders)`** — fuzzy matches a title against
   existing library folders to prevent duplicates (e.g. `Euphoria` vs `Euphoria (2019)`)
   Returns existing folder name or `None`

3. **`is_sample_file(client, filename)`** — determines if a video file is a real episode/movie
   or a sample/trailer/extra that should be skipped
   Returns `True` (skip) or `False` (keep)

cleanup.py adds:

4. **`ask_gemini_compliant(client, relative_path)`** — checks if an existing path already
   follows Plex conventions. Returns `True` (compliant) or `False` (needs fixing).

All calls use `gemini-2.5-flash` at `temperature=0.0`.
Free tier: 1,500 requests/day — sufficient for home use.
~3 API calls per file processed.

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env`.

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | (required) | Google Gemini API key |
| `BASE_MEDIA_DIR` | `/mnt/tank/media` | Root of Plex media library |
| `SYNC_DIR` | `$BASE_MEDIA_DIR/Sync` | Staging folder organizer watches |
| `SYNC_SRC` | `/mnt/tank/sync` | Syncthing source (smart-sync.sh) |
| `SYNC_DST` | `/mnt/tank/media/Sync` | rsync destination (smart-sync.sh) |
| `DISCORD_WEBHOOK_URL` | (optional) | Discord webhook for run notifications |
| `NTFY_URL` | (optional) | ntfy topic URL for push notifications |
| `TMDB_API_KEY` | (optional) | TMDB API key for title/year verification |

---

## Tracker format

`$BASE_MEDIA_DIR/.processed_history.txt`

```
filename.mkv|1748000000.0|ok
another.file.avi|1748000001.0|failed
```

- Keyed by **individual video filename** (not folder name)
- TTL: 14 days for `ok`, 2 hours for `failed` (retry interval matches cron)
- Loaded once per run (not on every call) and saved atomically via `.tmp` swap
- Expired entries pruned on load so the file doesn't grow unboundedly

---

## TrueNAS cron setup

| Schedule | Command |
|---|---|
| `*/30 * * * *` | `/mnt/apps-pool/scripts/smart-sync.sh` |
| `15,45 * * * *` | `docker compose -f /mnt/apps-pool/bea-tidy/docker-compose.yml run --rm organizer` |

---

## Docker setup

```bash
# Build both images
docker compose build

# Run organizer manually
docker compose run --rm organizer

# Cleanup dry run
docker compose --profile cleanup run --rm cleanup

# Cleanup execute
docker compose --profile cleanup run --rm cleanup --execute
```

---

## Plex naming conventions

| Category | Format |
|---|---|
| Movies | `Movies/Title (Year)/Title (Year).ext` |
| TV | `TV/Show Title/Season XX/Show Title - SXXEXX.ext` |
| Anime Movies | `Anime Movies/Title (Year)/Title (Year).ext` |
| Anime Series | `Anime Series/Show Title/Season XX/Show Title - SXXEXX.ext` |

- Season folders always zero-padded: `Season 01`, `Season 03`
- Episodes always `SXXEXX` format
- Year always included for movies (from filename or Gemini knowledge)
- Year omitted for TV unless title is ambiguous
- Specials go in `Season 00`

---

## Known edge cases handled

- Multi-episode folders (full seasons dumped in one folder) — each file processed individually
- 0-byte files from partial Syncthing transfers — skipped, retried next run
- Syncthing `.syncthing.*.tmp` files — folder skipped until transfer completes
- Sample/trailer files — Gemini-detected and skipped
- Duplicate show folders with year variants — fuzzy matched and renamed to Plex standard
- Sidecar files (`.nfo`, `.srt`, `.sfv`) — copied alongside their matching video
- `Subs/` subdirectories — recursively copied
- Partial multi-episode folder failure — rolled back, full retry next run
- Empty Syncthing source (unmounted drive) — rsync aborted, destination protected

## Tech stack

- Python 3.12
- google-genai SDK
- python-dotenv
- Docker / docker-compose
- TrueNAS SCALE (host)
- Syncthing (receive-only sync source)
- Plex Media Server
