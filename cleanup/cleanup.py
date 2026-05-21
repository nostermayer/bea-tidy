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

import csv
import os
import shutil
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


# ---------------------------------------------------------------------------
# CSV report — collects one row per video file, written at end of run
# ---------------------------------------------------------------------------

class Report:
    COLUMNS = ["action", "current_path", "proposed_path", "confidence", "notes"]

    def __init__(self):
        self.rows = []
        self.counts = {}

    def add(self, action, current_path, proposed_path="", confidence="", notes=""):
        self.rows.append({
            "action":        action,
            "current_path":  current_path,
            "proposed_path": proposed_path,
            "confidence":    confidence,
            "notes":         notes,
        })
        self.counts[action] = self.counts.get(action, 0) + 1

    def write(self, csv_path):
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.COLUMNS)
            writer.writeheader()
            writer.writerows(self.rows)
        logger.info(f"Report written: {csv_path}")

    def log_summary(self):
        logger.info("--- Summary ---")
        for action, count in sorted(self.counts.items()):
            logger.info(f"  {action}: {count}")
        logger.info(f"  TOTAL: {len(self.rows)}")


# ---------------------------------------------------------------------------
# Gemini helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

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


def move_sidecars(src_dir, dest_dir, video_stem, dry_run, mode_tag):
    """Move sidecar files (same stem, non-video) and Subs/ dir when the directory changes."""
    if src_dir == dest_dir:
        return

    stem_lower = video_stem.lower()
    for f in sorted(os.listdir(src_dir)):
        if f.startswith('.'):
            continue
        if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
            continue
        if os.path.isdir(os.path.join(src_dir, f)):
            continue
        if os.path.splitext(f)[0].lower().startswith(stem_lower):
            src_path  = os.path.join(src_dir, f)
            dest_path = os.path.join(dest_dir, f)
            if dry_run:
                logger.info(f"{mode_tag}   SIDECAR: {f}")
            else:
                try:
                    os.makedirs(dest_dir, exist_ok=True)
                    os.rename(src_path, dest_path)
                    logger.info(f"  Moved sidecar: {f}")
                except Exception as e:
                    logger.warning(f"  Failed to move sidecar {f}: {e}")

    subs_src  = os.path.join(src_dir, "Subs")
    subs_dest = os.path.join(dest_dir, "Subs")
    if os.path.isdir(subs_src):
        if dry_run:
            logger.info(f"{mode_tag}   SUBS DIR: Subs/")
        else:
            try:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copytree(subs_src, subs_dest, dirs_exist_ok=True)
                shutil.rmtree(subs_src)
                logger.info(f"  Moved Subs/ dir")
            except Exception as e:
                logger.warning(f"  Failed to move Subs/ dir: {e}")


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


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_video_file(client, video_path, base_media_dir, category, dry_run, mode_tag, report):
    relative_path = os.path.relpath(video_path, base_media_dir)
    filename      = os.path.basename(video_path)
    video_stem    = os.path.splitext(filename)[0]

    if ask_gemini_compliant(client, relative_path):
        logger.info(f"{mode_tag} COMPLIANT: {relative_path}")
        report.add("COMPLIANT", relative_path)
        return

    logger.info(f"{mode_tag} NON-COMPLIANT: {relative_path}")

    ideal, confidence = ask_gemini_classify(client, filename)
    if not ideal:
        logger.warning(f"{mode_tag} Could not classify: {filename} — skipping")
        report.add("SKIP_NO_CLASSIFY", relative_path, notes="Gemini returned no usable path")
        return

    ideal, confidence = tmdb_enrich(ideal, confidence)

    if confidence == "LOW":
        logger.warning(f"{mode_tag} LOW confidence for: {filename} — skipping (suggested: {ideal})")
        report.add("SKIP_LOW_CONFIDENCE", relative_path, proposed_path=ideal,
                   confidence=confidence, notes="Review manually")
        return

    ideal_category = ideal.split("/")[0]
    if ideal_category != category:
        logger.warning(
            f"{mode_tag} Category mismatch: file is in '{category}' but Gemini says "
            f"'{ideal_category}'. Skipping — review manually."
        )
        report.add("SKIP_CATEGORY_MISMATCH", relative_path, proposed_path=ideal,
                   confidence=confidence,
                   notes=f"Currently in '{category}', Gemini says '{ideal_category}'")
        return

    ideal_abs = os.path.join(base_media_dir, ideal)
    if video_path == ideal_abs:
        logger.info(f"{mode_tag} Already at correct path: {relative_path}")
        report.add("COMPLIANT", relative_path, confidence=confidence)
        return

    if os.path.exists(ideal_abs):
        logger.warning(f"{mode_tag} Destination already exists, skipping: {ideal}")
        report.add("SKIP_DEST_EXISTS", relative_path, proposed_path=ideal,
                   confidence=confidence, notes="Destination path already occupied")
        return

    src_dir  = os.path.dirname(video_path)
    dest_dir = os.path.dirname(ideal_abs)

    logger.info(f"{mode_tag}   FROM: {relative_path}")
    logger.info(f"{mode_tag}   TO:   {ideal}")
    move_sidecars(src_dir, dest_dir, video_stem, dry_run, mode_tag)
    report.add("RENAME", relative_path, proposed_path=ideal, confidence=confidence)

    if not dry_run:
        ok = safe_rename(video_path, ideal_abs, dry_run=False)
        if not ok:
            logger.error(f"{mode_tag}   Failed to move: {relative_path}")
            report.rows[-1]["action"] = "RENAME_FAILED"
            report.counts["RENAME"] -= 1
            report.counts["RENAME_FAILED"] = report.counts.get("RENAME_FAILED", 0) + 1


def process_category(client, category, category_path, base_media_dir, dry_run, mode_tag, report):
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
        process_video_file(client, video_path, base_media_dir, category, dry_run, mode_tag, report)

    cleanup_empty_dirs(category_path, dry_run)


# ---------------------------------------------------------------------------
# CSV-driven apply — no Gemini calls
# ---------------------------------------------------------------------------

def run_from_csv(args):
    base_media_dir = os.getenv("BASE_MEDIA_DIR", "/mnt/tank/media")
    dry_run  = not args.execute
    mode_tag = "[DRY RUN]" if dry_run else "[EXECUTE]"
    report   = Report()

    csv_path = args.from_csv if args.from_csv != "__default__" \
        else os.path.join(base_media_dir, "cleanup_plan.csv")

    if not os.path.exists(csv_path):
        logger.error(f"CSV not found: {csv_path}")
        sys.exit(1)

    logger.info(f"Applying plan from: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    rename_rows = [r for r in rows if r["action"] == "RENAME"]
    skipped_rows = [r for r in rows if r["action"].startswith("SKIP_")]
    logger.info(f"Found {len(rename_rows)} RENAME(s), {len(skipped_rows)} previously skipped")

    affected_dirs = set()

    for row in rename_rows:
        current_rel  = row["current_path"]
        proposed_rel = row["proposed_path"]

        if not current_rel or not proposed_rel:
            logger.warning(f"Incomplete row, skipping: {row}")
            continue

        src_abs  = os.path.join(base_media_dir, current_rel)
        dest_abs = os.path.join(base_media_dir, proposed_rel)

        if not os.path.exists(src_abs):
            logger.warning(f"{mode_tag} Source not found, skipping: {current_rel}")
            report.add("SKIP_NOT_FOUND", current_rel, proposed_path=proposed_rel,
                       notes="Source file missing — already moved?")
            continue

        if os.path.exists(dest_abs):
            logger.warning(f"{mode_tag} Destination exists, skipping: {proposed_rel}")
            report.add("SKIP_DEST_EXISTS", current_rel, proposed_path=proposed_rel,
                       notes="Destination path already occupied")
            continue

        src_dir    = os.path.dirname(src_abs)
        dest_dir   = os.path.dirname(dest_abs)
        video_stem = os.path.splitext(os.path.basename(src_abs))[0]

        logger.info(f"{mode_tag}   FROM: {current_rel}")
        logger.info(f"{mode_tag}   TO:   {proposed_rel}")
        move_sidecars(src_dir, dest_dir, video_stem, dry_run, mode_tag)
        report.add("RENAME", current_rel, proposed_path=proposed_rel,
                   confidence=row.get("confidence", ""))
        affected_dirs.add(src_dir)

        if not dry_run:
            ok = safe_rename(src_abs, dest_abs, dry_run=False)
            if not ok:
                report.rows[-1]["action"] = "RENAME_FAILED"
                report.counts["RENAME"] -= 1
                report.counts["RENAME_FAILED"] = report.counts.get("RENAME_FAILED", 0) + 1

    # Clean up empty dirs left behind
    for d in affected_dirs:
        cleanup_empty_dirs(d, dry_run)

    out_csv = os.path.join(base_media_dir, "cleanup_applied.csv")
    report.write(out_csv)
    report.log_summary()
    logger.info("--- Apply Complete ---")
    if dry_run:
        logger.info("This was a DRY RUN — nothing was changed.")
        logger.info("Rerun with --execute to apply changes.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_cleanup(args):
    base_media_dir = os.getenv("BASE_MEDIA_DIR", "/mnt/tank/media")
    dry_run  = not args.execute
    mode_tag = "[DRY RUN]" if dry_run else "[EXECUTE]"
    report   = Report()

    try:
        client = make_client()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    if args.path:
        target = os.path.abspath(args.path)
        if not target.startswith(os.path.abspath(base_media_dir)):
            logger.error(f"--path must be within BASE_MEDIA_DIR ({base_media_dir})")
            sys.exit(1)
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
            process_video_file(client, video_path, base_media_dir, category, dry_run, mode_tag, report)
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
                base_media_dir, dry_run, mode_tag, report,
            )

    csv_path = os.path.join(base_media_dir, "cleanup_plan.csv")
    report.write(csv_path)
    report.log_summary()

    logger.info("--- Cleanup Complete ---")
    if dry_run:
        logger.info("This was a DRY RUN — nothing was changed.")
        logger.info(f"Review the plan: {csv_path}")
        logger.info("Rerun with --execute to apply changes.")


def main():
    parser = argparse.ArgumentParser(description="Plex library cleanup using Gemini AI")
    parser.add_argument("--execute", action="store_true",
                        help="Actually rename/move files. Without this, runs as dry run.")
    parser.add_argument("--category", type=str, default=None,
                        help="Limit to one category e.g. Movies")
    parser.add_argument("--path", type=str, default=None,
                        help="Target a specific folder path for cleanup")
    parser.add_argument("--from-csv", nargs="?", const="__default__", default=None,
                        metavar="PATH",
                        help="Apply renames from a previous dry-run CSV instead of re-querying Gemini. "
                             "Optionally specify a CSV path (default: cleanup_plan.csv in BASE_MEDIA_DIR).")
    args = parser.parse_args()

    dry_run = not args.execute

    if args.from_csv is not None:
        if dry_run:
            print("\n" + "=" * 60)
            print("  DRY RUN MODE (from CSV)")
            print("  No files will be renamed, moved, or deleted.")
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
        run_from_csv(args)
        return

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
