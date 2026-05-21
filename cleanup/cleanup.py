#!/usr/bin/env python3
"""
cleanup.py — One-shot Plex library cleanup.

Walks existing library categories, classifies every video it finds using
Gemini, and renames files/folders to Plex standard naming conventions.

Usage:
  python3 cleanup.py                      # dry run (no changes made)
  python3 cleanup.py --execute            # actually rename/move files
  python3 cleanup.py --category Movies    # limit to one category
  python3 cleanup.py --path /mnt/tank/media/Movies/Godfather  # target a specific path
  python3 cleanup.py --execute --category TV
"""

import os
import sys
import time
import argparse
import logging
from logging.handlers import RotatingFileHandler

from lib import (
    VIDEO_EXTENSIONS, CATEGORIES, GEMINI_MODEL, GEMINI_RATE_SLEEP,
    CLASSIFY_INSTRUCTION, make_client, ask_gemini_classify, apply_open_permissions,
    tmdb_enrich,
)
from google.genai import types


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


def ask_gemini_compliant(client, relative_path):
    prompt = f'Is this path Plex-compliant? "{relative_path}"'
    config = types.GenerateContentConfig(
        system_instruction=CHECK_COMPLIANT_INSTRUCTION,
        temperature=0.0
    )
    for attempt in range(3):
        try:
            time.sleep(GEMINI_RATE_SLEEP)
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=config
            )
            return response.text.strip() == "COMPLIANT"
        except Exception as e:
            logger.warning(f"Compliance check retry {attempt + 1} for {relative_path}: {e}")
            time.sleep(2 ** attempt)
    return False


def safe_rename(src, dest, dry_run):
    if src == dest:
        return True
    if os.path.exists(dest):
        logger.warning(f"Cannot rename — destination already exists: {dest}")
        return False
    if dry_run:
        return True
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(src, dest)
        apply_open_permissions(dest)
        return True
    except Exception as e:
        logger.error(f"Rename failed {src} -> {dest}: {e}")
        return False


def cleanup_empty_dirs(path, dry_run):
    for root, dirs, files in os.walk(path, topdown=False):
        if root == path:
            continue
        try:
            if not os.listdir(root):
                if dry_run:
                    logger.info(f"Would remove empty dir: {root}")
                else:
                    os.rmdir(root)
                    logger.info(f"Removed empty dir: {root}")
        except Exception:
            pass


def process_video_file(client, video_path, base_media_dir, category, dry_run, mode_tag):
    relative_path = os.path.relpath(video_path, base_media_dir)
    filename      = os.path.basename(video_path)

    if ask_gemini_compliant(client, relative_path):
        logger.info(f"{mode_tag} COMPLIANT, skipping: {relative_path}")
        return

    logger.info(f"{mode_tag} NON-COMPLIANT: {relative_path}")

    ideal, confidence = ask_gemini_classify(client, filename)
    if not ideal:
        logger.warning(f"{mode_tag} Could not classify: {filename} — skipping")
        return
    ideal, confidence = tmdb_enrich(ideal, confidence)
    if confidence == "LOW":
        logger.warning(f"{mode_tag} LOW confidence classification for: {filename} — review suggested path: {ideal}")

    ideal_category = ideal.split("/")[0]
    if ideal_category != category:
        logger.warning(
            f"{mode_tag} Category mismatch: file is in '{category}' but Gemini says "
            f"'{ideal_category}'. Skipping — review manually."
        )
        return

    ideal_abs = os.path.join(base_media_dir, ideal)
    if video_path == ideal_abs:
        logger.info(f"{mode_tag} Already at correct path: {relative_path}")
        return

    logger.info(f"{mode_tag}   FROM: {relative_path}")
    logger.info(f"{mode_tag}   TO:   {ideal}")

    if not dry_run:
        ok = safe_rename(video_path, ideal_abs, dry_run=False)
        if not ok:
            logger.error(f"{mode_tag}   Failed to move: {relative_path}")


def process_category(client, category, category_path, base_media_dir, dry_run, mode_tag):
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
        process_video_file(client, video_path, base_media_dir, category, dry_run, mode_tag)

    cleanup_empty_dirs(category_path, dry_run)


def run_cleanup(args):
    base_media_dir = os.getenv("BASE_MEDIA_DIR", "/mnt/tank/media")
    dry_run  = not args.execute
    mode_tag = "[DRY RUN]" if dry_run else "[EXECUTE]"

    try:
        client = make_client()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    if args.path:
        # Targeted path mode
        target = os.path.abspath(args.path)
        if not target.startswith(os.path.abspath(base_media_dir)):
            logger.error(f"--path must be within BASE_MEDIA_DIR ({base_media_dir})")
            sys.exit(1)
        # Infer category from path
        rel = os.path.relpath(target, base_media_dir)
        category = rel.split(os.sep)[0]
        if category not in CATEGORIES:
            logger.error(f"Could not determine category from path. Expected one of: {CATEGORIES}")
            sys.exit(1)
        logger.info(f"Targeted path: {target} (category: {category})")
        video_files = []
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in sorted(files):
                if not f.startswith('.') and os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                    video_files.append(os.path.join(root, f))
        for video_path in video_files:
            process_video_file(client, video_path, base_media_dir, category, dry_run, mode_tag)
        cleanup_empty_dirs(target, dry_run)
    else:
        categories = [args.category] if args.category else CATEGORIES
        if args.category and args.category not in CATEGORIES:
            logger.error(f"Unknown category '{args.category}'. Valid: {CATEGORIES}")
            sys.exit(1)
        logger.info(f"Base media dir: {base_media_dir}")
        logger.info(f"Categories:     {categories}")
        for category in categories:
            process_category(
                client, category,
                os.path.join(base_media_dir, category),
                base_media_dir, dry_run, mode_tag,
            )

    logger.info("--- Cleanup Complete ---")
    if dry_run:
        logger.info("This was a DRY RUN — nothing was changed.")
        logger.info("Rerun with --execute to apply changes.")


def main():
    parser = argparse.ArgumentParser(description="Plex library cleanup using Gemini AI")
    parser.add_argument("--execute", action="store_true",
                        help="Actually rename/move files. Without this, runs as dry run.")
    parser.add_argument("--category", type=str, default=None,
                        help="Limit to one category e.g. Movies")
    parser.add_argument("--path", type=str, default=None,
                        help="Target a specific folder path for cleanup")
    args = parser.parse_args()

    dry_run = not args.execute

    if dry_run:
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

    run_cleanup(args)


# ---------------------------------------------------------------------------
# Logging — module-level so it's available to all functions
# ---------------------------------------------------------------------------

base_media_dir = os.getenv("BASE_MEDIA_DIR", "/mnt/tank/media")
log_file = os.path.join(base_media_dir, "cleanup.log")

logger = logging.getLogger("bea-tidy")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=3)
_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(_handler)
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(_console)


if __name__ == "__main__":
    main()
