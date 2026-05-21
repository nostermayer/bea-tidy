# beasync — Claude Code Context

## What this project is

beasync is an AI-powered Plex media library organizer running on a TrueNAS home server.
It uses the Google Gemini API to classify, rename, and sort media files into Plex-standard
folder structures automatically.

Named after Beatrix (Bea), Nick's wife.

---

## Architecture

```
Syncthing (receive-only) -> /mnt/tank/sync
        |
        | smart-sync.sh (cron every 30min)
        v
/mnt/tank/media/Sync
        |
        | gemini-organizer Docker container (cron at :15 and :45)
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

### `organizer/` — Continuous sync watcher
- Runs as a Docker container triggered by cron every 30 minutes (offset by 15 min from sync)
- Watches `/mnt/tank/media/Sync` for new files/folders
- Classifies each file using Gemini into Movies / TV / Anime Movies / Anime Series
- Copies files into correct Plex-standard paths under `/mnt/tank/media/`
- Tracks processed files individually (not folders) to handle partial transfers
- Handles: bare files, single-episode folders, multi-episode folders (e.g. full seasons)
- Skips: 0-byte files (partial Syncthing transfers), sample/trailer files (Gemini-detected)

### `cleanup/` — One-shot library cleanup
- Run manually to fix an existing badly-named library
- Dry run by default — shows what would change without touching anything
- `--execute` flag applies changes, with a 5-second countdown abort window
- `--category Movies` to limit scope
- Every log line prefixed with `[DRY RUN]` or `[EXECUTE]`

### `scripts/smart-sync.sh` — Smart rsync wrapper
- Snapshots file mtimes before running rsync
- Skips rsync entirely if nothing has changed (saves I/O on idle periods)
- Run every 30 minutes via TrueNAS cron

---

## Gemini API usage

Three types of Gemini calls in organizer.py:

1. **`ask_gemini_classify(filename)`** — classifies a filename into a full Plex path
   e.g. `jersey.shore.s01e01.dvdrip.avi` → `TV/Jersey Shore/Season 01/Jersey Shore - S01E01.avi`

2. **`ask_gemini_match_folder(title, existing_folders)`** — fuzzy matches a title against
   existing library folders to prevent duplicates (e.g. `Euphoria` vs `Euphoria (2019)`)
   Returns existing folder name or `NEW`

3. **`is_sample_file(filename)`** — determines if a video file is a real episode/movie
   or a sample/trailer/extra that should be skipped
   Returns `REAL` or `SKIP`

All calls use `gemini-2.5-flash` at `temperature=0.0`.
Free tier: 1,500 requests/day — sufficient for home use.
~3 API calls per file processed.

---

## Tracker format

`/mnt/tank/media/.processed_history.txt`

```
filename.mkv|1748000000.0|ok
another.file.avi|1748000001.0|failed
```

- Keyed by **individual video filename** (not folder name)
- TTL: 14 days for `ok`, 2 hours for `failed` (retry interval matches cron)
- Atomic write via `.tmp` swap

---

## TrueNAS cron setup

| Schedule | Command |
|---|---|
| `*/30 * * * *` | `/mnt/apps-pool/scripts/smart-sync.sh` |
| `15,45 * * * *` | `docker start gemini-organizer` |

---

## Docker setup

### Organizer
```bash
cd organizer/
docker build -t gemini-organizer .
docker run --rm \
  --name gemini-organizer \
  -e GEMINI_API_KEY=your_key \
  -v /mnt/tank/media:/mnt/tank/media \
  gemini-organizer
```

### Cleanup
```bash
cd cleanup/
docker build -t gemini-cleanup .

# Dry run
docker run --rm \
  -e GEMINI_API_KEY=your_key \
  -v /mnt/tank/media:/mnt/tank/media \
  gemini-cleanup

# Execute
docker run --rm \
  -e GEMINI_API_KEY=your_key \
  -v /mnt/tank/media:/mnt/tank/media \
  gemini-cleanup --execute

# Single category
docker run --rm ... gemini-cleanup --category Movies
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

## Planned improvements (not yet implemented)

1. Syncthing completion detection (check for `.syncthing.*.tmp` files)
2. Rollback on partial multi-episode folder failure
3. Run summary at end of each audit (Processed: X | Skipped: Y | Failed: Z)
4. Ntfy or Discord webhook notification with run summary
5. Confidence scoring — flag low-confidence classifications for manual review
6. Cross-category mismatch flagging (e.g. file in Movies/ that Gemini thinks is TV)
7. GitHub Actions — auto-build and push Docker image to GHCR on push to main
8. `.env` file support instead of passing API key on command line
9. General library cleanup mode with `--path` argument for targeted cleanup

---

## Known edge cases handled

- Multi-episode folders (full seasons dumped in one folder) — each file processed individually
- 0-byte files from partial Syncthing transfers — skipped, retried next run
- Sample/trailer files — Gemini-detected and skipped
- Duplicate show folders with year variants — fuzzy matched and renamed to Plex standard
- Sidecar files (`.nfo`, `.srt`, `.sfv`) — copied alongside their matching video
- `Subs/` subdirectories — recursively copied

## Tech stack

- Python 3.12
- google-genai SDK
- Docker (python:3.12-slim base)
- TrueNAS SCALE (host)
- Syncthing (receive-only sync source)
- Plex Media Server
