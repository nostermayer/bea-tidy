# bea-tidy

AI-powered Plex media library organizer for a home server.

Uses the Google Gemini API to automatically classify, rename, and sort media files into Plex-standard folder structures — handling everything from single movies to full multi-season TV show dumps.

Named after Bea.

---

## How it works

```
Syncthing (receive-only) → /mnt/tank/sync
        ↓  smart-sync.sh (every 30 min)
/mnt/tank/media/Sync
        ↓  organizer (every 30 min)
/mnt/tank/media/
  Movies/Title (Year)/Title (Year).mp4
  TV/Show Name/Season 01/Show Name - S01E01.mkv
  Anime Movies/
  Anime Series/
```

Files arrive via Syncthing, get rsynced into a staging folder, then Gemini classifies each one and copies it into the correct Plex-standard path.

---

## Features

- **AI classification** — Gemini identifies title, year, season, episode from any filename format
- **Plex-standard naming** — correct folder structure, zero-padded seasons, SXXEXX episodes
- **Smart deduplication** — fuzzy folder matching prevents duplicate show folders (`Euphoria` vs `Euphoria (2019)`)
- **Multi-season handling** — full season dumps processed episode-by-episode into correct season folders
- **Partial transfer detection** — 0-byte files and Syncthing `.tmp` files skipped and retried next run
- **Sample detection** — Gemini identifies and skips sample/trailer/featurette files
- **Sidecar support** — `.nfo`, `.srt`, `.sfv` files copied alongside their video
- **Integrity checks** — file size verified after every copy
- **Rollback on failure** — if a multi-episode folder partially fails, successfully copied files are removed so the whole folder retries cleanly next run
- **Individual file tracking** — each episode tracked separately so partial folder transfers resume correctly
- **Run summary** — each run logs `Processed: X | Skipped: Y | Failed: Z`
- **Notifications** — optional Discord and/or ntfy push notifications with run summary
- **Library cleanup** — separate one-shot tool to fix existing badly-named libraries
- **TMDB verification** — canonical titles and years verified against The Movie Database before filing; upgrades Gemini's confidence rating when confirmed
- **Confidence flagging** — low-confidence classifications not confirmed by TMDB are flagged for manual review in `_review_needed.txt`

---

## Project structure

```
bea-tidy/
├── lib.py              # Shared Gemini client, prompts, and helpers
├── organizer/          # Continuous sync watcher (runs on cron)
│   ├── Dockerfile
│   └── organizer.py
├── cleanup/            # One-shot library cleanup tool
│   ├── Dockerfile
│   └── cleanup.py
├── scripts/
│   └── smart-sync.sh  # Smart rsync wrapper (skips if no changes)
├── docker-compose.yml
├── .env.example        # Copy to .env and fill in your values
└── CLAUDE.md          # Full context for Claude Code
```

---

## Setup

### Prerequisites

- TrueNAS SCALE (or any Linux server with Docker)
- Syncthing configured as receive-only
- Plex Media Server
- Google Gemini API key (free tier sufficient — 1,500 requests/day)
- TMDB API key (free, optional but recommended — improves title accuracy)

### 1. Clone and configure

```bash
git clone https://github.com/nostermayer/bea-tidy /mnt/apps-pool/bea-tidy
cd /mnt/apps-pool/bea-tidy
cp .env.example .env
# Edit .env with your GEMINI_API_KEY and path settings
```

### 2. Build images

```bash
docker compose build
```

### 3. Deploy smart-sync

```bash
sudo cp scripts/smart-sync.sh /mnt/apps-pool/scripts/
sudo chmod +x /mnt/apps-pool/scripts/smart-sync.sh
```

### 4. TrueNAS cron jobs

| Schedule | Command |
|---|---|
| `*/30 * * * *` | `/mnt/apps-pool/scripts/smart-sync.sh` |
| `15,45 * * * *` | `docker compose -f /mnt/apps-pool/bea-tidy/docker-compose.yml run --rm organizer` |

---

## Usage

### Organizer (automatic)

Triggered by cron — no manual intervention needed. Check logs at `$BASE_MEDIA_DIR/organizer.log`.

### Cleanup (manual, one-off)

```bash
# Dry run first — always
docker compose --profile cleanup run --rm cleanup

# Limit to one category
docker compose --profile cleanup run --rm cleanup --category Movies

# Target a specific folder
docker compose --profile cleanup run --rm cleanup --path /mnt/tank/media/Movies/Godfather

# Apply changes (5 second abort window)
docker compose --profile cleanup run --rm cleanup --execute
```

### Notifications

Set `DISCORD_WEBHOOK_URL` and/or `NTFY_URL` in your `.env` file. Both are optional — bea-tidy sends a run summary after each organizer run to whichever are configured.

---

## Plex naming conventions

| Category | Format |
|---|---|
| Movies | `Movies/Title (Year)/Title (Year).ext` |
| TV | `TV/Show Title/Season XX/Show Title - SXXEXX.ext` |
| Anime Movies | `Anime Movies/Title (Year)/Title (Year).ext` |
| Anime Series | `Anime Series/Show Title/Season XX/Show Title - SXXEXX.ext` |

---

## API costs

Uses `gemini-2.5-flash`. Free tier provides 1,500 requests/day — enough for ~500 files/day at 3 calls per file. Paid cost is negligible (under $0.01 per 1,000 files).

---

## License

MIT — see [LICENSE](LICENSE).
