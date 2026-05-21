# beasync 🎬

AI-powered Plex media library organizer for a TrueNAS home server.

Uses the Google Gemini API to automatically classify, rename, and sort media files into Plex-standard folder structures — handling everything from single movies to full multi-season TV show dumps.

Named after Bea 🌸

---

## How it works

```
Syncthing (receive-only) → /mnt/tank/sync
        ↓  smart-sync.sh (every 30 min)
/mnt/tank/media/Sync
        ↓  gemini-organizer (every 30 min)
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
- **Partial transfer detection** — 0-byte files skipped and retried next run
- **Sample detection** — Gemini identifies and skips sample/trailer/featurette files
- **Sidecar support** — `.nfo`, `.srt`, `.sfv` files copied alongside their video
- **Integrity checks** — file size verified after every copy
- **Individual file tracking** — each episode tracked separately so partial folder transfers resume correctly
- **Library cleanup** — separate one-shot tool to fix existing badly-named libraries

---

## Project structure

```
beasync/
├── organizer/          # Continuous sync watcher (runs on cron)
│   ├── Dockerfile
│   └── organizer.py
├── cleanup/            # One-shot library cleanup tool
│   ├── Dockerfile
│   └── cleanup.py
├── scripts/
│   └── smart-sync.sh  # Smart rsync wrapper (skips if no changes)
└── CLAUDE.md          # Full context for Claude Code
```

---

## Setup

### Prerequisites

- TrueNAS SCALE (or any Linux server with Docker)
- Syncthing configured as receive-only
- Plex Media Server
- Google Gemini API key (free tier sufficient — 1,500 requests/day)

### Deploy

```bash
git clone https://github.com/yourusername/beasync /mnt/apps-pool/beasync

# Build organizer
cd /mnt/apps-pool/beasync/organizer
docker build -t gemini-organizer .

# Build cleanup
cd /mnt/apps-pool/beasync/cleanup
docker build -t gemini-cleanup .

# Deploy smart-sync
sudo cp scripts/smart-sync.sh /mnt/apps-pool/scripts/
sudo chmod +x /mnt/apps-pool/scripts/smart-sync.sh
```

### TrueNAS cron jobs

| Schedule | Command |
|---|---|
| `*/30 * * * *` | `/mnt/apps-pool/scripts/smart-sync.sh` |
| `15,45 * * * *` | `docker run --rm --name gemini-organizer -e GEMINI_API_KEY=your_key -v /mnt/tank/media:/mnt/tank/media gemini-organizer` |

---

## Usage

### Organizer (automatic)

Triggered by cron — no manual intervention needed. Check logs at `/mnt/tank/media/organizer.log`.

### Cleanup (manual, one-off)

```bash
# Dry run first — always
docker run --rm \
  -e GEMINI_API_KEY=your_key \
  -v /mnt/tank/media:/mnt/tank/media \
  gemini-cleanup

# Limit to one category
docker run --rm ... gemini-cleanup --category Movies

# Apply changes (5 second abort window)
docker run --rm ... gemini-cleanup --execute
```

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
