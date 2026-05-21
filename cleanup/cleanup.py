#!/usr/bin/env python3
"""
cleanup.py — One-shot Plex library cleanup.

Walks existing library categories, classifies every video it finds using
Gemini, and renames files/folders to Plex standard naming conventions.

Usage:
  python3 cleanup.py                      # dry run (no changes made)
  python3 cleanup.py --execute            # actually rename/move files
  python3 cleanup.py --category Movies    # limit to one category
  python3 cleanup.py --execute --category TV
"""

import os
import sys
import time
import argparse
import logging
from logging.handlers import RotatingFileHandler
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_KEY     = os.getenv("GEMINI_API_KEY")
BASE_MEDIA_DIR = "/mnt/tank/media"
LOG_FILE       = os.path.join(BASE_MEDIA_DIR, "cleanup.log")
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.m4v'}
CATEGORIES     = ["Movies", "TV", "Anime Movies", "Anime Series"]

# ---------------------------------------------------------------------------
# Args — parsed early so we know DRY_RUN before anything else
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description="Plex library cleanup using Gemini AI")
parser.add_argument("--execute", action="store_true",
                    help="Actually rename/move files. Without this, runs as dry run.")
parser.add_argument("--category", type=str, default=None,
                    help="Limit to one category e.g. Movies")
args = parser.parse_args()

DRY_RUN = not args.execute
MODE_TAG = "[DRY RUN]" if DRY_RUN else "[EXECUTE]"

# ---------------------------------------------------------------------------
# Logging — every line is prefixed with mode tag
# ---------------------------------------------------------------------------

class ModeTagFormatter(logging.Formatter):
    def format(self, record):
        record.msg = f"{MODE_TAG} {record.msg}"
        return super().format(record)

logger = logging.getLogger("Cleanup")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3)
formatter = ModeTagFormatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

# ---------------------------------------------------------------------------
# Startup banner — unmissable
# ---------------------------------------------------------------------------

if DRY_RUN:
    print("\n" + "=" * 60)
    print("  DRY RUN MODE")
    print("  No files will be renamed, moved, or deleted.")
    print("  Every action is prefixed with [DRY RUN]")
    print("  To apply changes, rerun with --execute")
    print("=" * 60 + "\n")
else:
    print("\n" + "!" * 60)
    print("  EXECUTE MODE — FILES WILL BE RENAMED/MOVED")
    print("  Starting in 5 seconds. Press Ctrl+C to abort.")
    print("!" * 60 + "\n")
    for i in range(5, 0, -1):
        print(f"  {i}...")
        time.sleep(1)
    print("  Proceeding.\n")

if not GEMINI_KEY:
    logger.error("GEMINI_API_KEY missing.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_KEY)

# ---------------------------------------------------------------------------
# Gemini prompts
# ---------------------------------------------------------------------------

CLASSIFY_INSTRUCTION = """You are a media file classifier that outputs Plex-compatible folder paths.

Given a filename, identify the title, year (if present), season, and episode (if present), then return a Plex-standard relative path.

MOVIES format:
  Movies/Title (Year)/Title (Year).ext

TV format:
  TV/Show Title/Season XX/Show Title - SXXEXX.ext

Anime Movies format:
  Anime Movies/Title (Year)/Title (Year).ext

Anime Series format:
  Anime Series/Show Title/Season XX/Show Title - SXXEXX.ext

Rules:
- Output ONLY the relative path, one line, no explanation, no markdown
- Use proper title casing (not the raw filename dots/underscores)
- Preserve the original file extension exactly
- Year: ALWAYS include the release year in parentheses for movies and anime movies. If the year is in the filename use it. If not, use your knowledge of the title to determine the correct release year. Only omit the year if you genuinely cannot determine it
- For TV and anime series, omit the year from the show folder unless the show title is ambiguous (e.g. same name remade in different years)
- Season folder is always zero-padded two digits e.g. Season 01, Season 03
- Episode uses SXXEXX format

Multi-episode files:
- If the filename contains multiple episodes (e.g. E01E02, E01-E02), use the first episode number only

Specials:
- If the filename contains S00 or the word Special/OVA, treat as a special and place in Season 00

Examples:
  Input:  A.Knights.Tale.2001.1080p.BluRay.x265.mp4
  Output: Movies/A Knight's Tale (2001)/A Knight's Tale (2001).mp4

  Input:  Euphoria.US.S03E06.1080p.HEVC.x265-MeGusta.mkv
  Output: TV/Euphoria/Season 03/Euphoria - S03E06.mkv

  Input:  I.Robot.2004.1080p.BluRay.x265.mkv
  Output: Movies/I, Robot (2004)/I, Robot (2004).mkv

  Input:  Breaking.Bad.S02E05E06.1080p.mkv
  Output: TV/Breaking Bad/Season 02/Breaking Bad - S02E05.mkv"""


CHECK_COMPLIANT_INSTRUCTION = """You are a Plex naming compliance checker.

Given a file path relative to the media root, decide if it already follows Plex naming conventions.

Plex conventions:
- Movies:       Movies/Title (Year)/Title (Year).ext
- TV:           TV/Show Title/Season XX/Show Title - SXXEXX.ext
- Anime Movies: Anime Movies/Title (Year)/Title (Year).ext
- Anime Series: Anime Series/Show Title/Season XX/Show Title - SXXEXX.ext

Reply with ONLY one of:
  COMPLIANT
  NON_COMPLIANT

No explanation. One word only."""


def ask_gemini_classify(filename):
    prompt = f'Classify this media file: "{filename}"'
    config = types.GenerateContentConfig(
        system_instruction=CLASSIFY_INSTRUCTION,
        temperature=0.0
    )
    for attempt in range(3):
        try:
            time.sleep(4.5)
            response = client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt, config=config
            )
            result = response.text.strip().lstrip("/")
            if result and result.count("/") >= 2:
                return result
            logger.warning(f"Gemini classify invalid for {filename}: {repr(result)}")
        except Exception as e:
            logger.warning(f"Classify retry {attempt + 1} for {filename}: {e}")
            time.sleep(2 ** attempt)
    return None


def ask_gemini_compliant(relative_path):
    prompt = f'Is this path Plex-compliant? "{relative_path}"'
    config = types.GenerateContentConfig(
        system_instruction=CHECK_COMPLIANT_INSTRUCTION,
        temperature=0.0
    )
    try:
        time.sleep(4.5)
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt, config=config
        )
        return response.text.strip() == "COMPLIANT"
    except Exception as e:
        logger.warning(f"Compliance check failed for {relative_path}: {e}")
        return False


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def apply_open_permissions(path):
    try:
        os.chmod(path, 0o777 if os.path.isdir(path) else 0o666)
    except Exception:
        pass


def safe_rename(src, dest):
    if src == dest:
        return True
    if os.path.exists(dest):
        logger.warning(f"Cannot rename — destination already exists: {dest}")
        return False
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(src, dest)
        apply_open_permissions(dest)
        return True
    except Exception as e:
        logger.error(f"Rename failed {src} -> {dest}: {e}")
        return False


def cleanup_empty_dirs(path):
    for root, dirs, files in os.walk(path, topdown=False):
        if root == path:
            continue
        try:
            if not os.listdir(root):
                if DRY_RUN:
                    logger.info(f"Would remove empty dir: {root}")
                else:
                    os.rmdir(root)
                    logger.info(f"Removed empty dir: {root}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Core cleanup logic
# ---------------------------------------------------------------------------

def process_video_file(video_path, category):
    relative_path = os.path.relpath(video_path, BASE_MEDIA_DIR)
    filename = os.path.basename(video_path)

    if ask_gemini_compliant(relative_path):
        logger.info(f"COMPLIANT, skipping: {relative_path}")
        return

    logger.info(f"NON-COMPLIANT: {relative_path}")

    ideal = ask_gemini_classify(filename)
    if not ideal:
        logger.warning(f"Could not classify: {filename} — skipping")
        return

    ideal_category = ideal.split("/")[0]
    if ideal_category != category:
        logger.warning(
            f"Category mismatch: file is in '{category}' but Gemini says '{ideal_category}'. "
            f"Skipping — review manually."
        )
        return

    ideal_abs = os.path.join(BASE_MEDIA_DIR, ideal)

    if video_path == ideal_abs:
        logger.info(f"Already at correct path: {relative_path}")
        return

    logger.info(f"  FROM: {relative_path}")
    logger.info(f"  TO:   {ideal}")

    if DRY_RUN:
        logger.info(f"  (no action taken)")
        return

    ok = safe_rename(video_path, ideal_abs)
    if not ok:
        logger.error(f"  Failed to move: {relative_path}")


def process_category(category):
    category_path = os.path.join(BASE_MEDIA_DIR, category)

    if not os.path.isdir(category_path):
        logger.warning(f"Category folder not found, skipping: {category_path}")
        return

    logger.info(f"{'=' * 50}")
    logger.info(f"Category: {category}")
    logger.info(f"{'=' * 50}")

    video_files = []
    for root, dirs, files in os.walk(category_path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in sorted(files):
            if f.startswith('.'):
                continue
            if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                video_files.append(os.path.join(root, f))

    logger.info(f"Found {len(video_files)} video file(s)")

    for video_path in video_files:
        process_video_file(video_path, category)

    cleanup_empty_dirs(category_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_cleanup():
    categories = [args.category] if args.category else CATEGORIES

    if args.category and args.category not in CATEGORIES:
        logger.error(f"Unknown category '{args.category}'. Valid: {CATEGORIES}")
        sys.exit(1)

    logger.info(f"Base media dir: {BASE_MEDIA_DIR}")
    logger.info(f"Categories:     {categories}")

    for category in categories:
        process_category(category)

    logger.info("--- Cleanup Complete ---")
    if DRY_RUN:
        logger.info("This was a DRY RUN — nothing was changed.")
        logger.info("Rerun with --execute to apply changes.")
    logger.info(f"Full log: {LOG_FILE}")


if __name__ == "__main__":
    run_cleanup()
