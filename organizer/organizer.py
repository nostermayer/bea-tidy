import os
import shutil
import time
import logging
from logging.handlers import RotatingFileHandler

from lib import (
    VIDEO_EXTENSIONS, make_client,
    ask_gemini_classify, ask_gemini_match_folder, is_sample_file,
    apply_open_permissions, check_category, tmdb_enrich,
)
from notify import send_run_summary, send_file_added

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

BASE_MEDIA_DIR = os.getenv("BASE_MEDIA_DIR", "/mnt/tank/media")
LOG_FILE       = os.path.join(BASE_MEDIA_DIR, "organizer.log")
TRACKER_FILE   = os.path.join(BASE_MEDIA_DIR, ".processed_history.txt")
PROCESSED_TTL  = 1209600  # 14 days in seconds
FAILED_RETRY   = 7200     # 2 hours — matches cron interval
REVIEW_FILE    = os.path.join(BASE_MEDIA_DIR, "_review_needed.txt")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("bea-tidy")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=1 * 1024 * 1024, backupCount=3)
handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
logger.addHandler(handler)
console = logging.StreamHandler()
console.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
logger.addHandler(console)

try:
    client = make_client()
except EnvironmentError as e:
    logger.error(str(e))
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Tracker — keyed by individual filename (not folder name for multi-episode)
# Format: <filename>|<timestamp>|<status>
# Loaded once per run and passed through to avoid repeated disk reads.
# ---------------------------------------------------------------------------

def load_tracker():
    """Load tracker from disk, pruning expired entries in the process."""
    records = {}
    now = time.time()
    if not os.path.exists(TRACKER_FILE):
        return records
    with open(TRACKER_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            try:
                name      = parts[0]
                timestamp = float(parts[1])
                status    = parts[2] if len(parts) > 2 else "ok"
            except (IndexError, ValueError):
                continue
            age = now - timestamp
            # Drop expired entries so the file doesn't grow unboundedly
            if status == "ok" and age >= PROCESSED_TTL:
                continue
            if status == "failed" and age >= FAILED_RETRY:
                continue
            records[name] = (timestamp, status)
    return records


def flag_for_review(filename, suggested_path):
    """Append a low-confidence classification to the review file for manual check."""
    with open(REVIEW_FILE, "a") as f:
        f.write(f"{filename} -> {suggested_path}\n")
    logger.warning(f"LOW confidence — flagged for review: {filename} -> {suggested_path}")


def save_tracker(records):
    """Atomically write tracker to disk."""
    tmp = TRACKER_FILE + ".tmp"
    with open(tmp, "w") as f:
        for name, (ts, st) in records.items():
            f.write(f"{name}|{ts}|{st}\n")
    os.replace(tmp, TRACKER_FILE)


def is_already_processed(records, name):
    if name not in records:
        return False
    timestamp, status = records[name]
    age = time.time() - timestamp
    if status == "failed":
        return age < FAILED_RETRY
    return age < PROCESSED_TTL


def mark_processed(records, name, status="ok"):
    records[name] = (time.time(), status)


# ---------------------------------------------------------------------------
# File readiness — skip 0-byte and in-progress Syncthing transfers
# ---------------------------------------------------------------------------

def is_file_ready(path):
    """Return False for 0-byte files or active Syncthing temp files."""
    try:
        if os.path.getsize(path) == 0:
            logger.info(f"Skipping 0-byte file (partial transfer): {path}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Could not stat file {path}: {e}")
        return False


def has_syncthing_tmp(folder_path):
    """Return True if any .syncthing.*.tmp file exists in folder tree."""
    for root, _, files in os.walk(folder_path):
        for f in files:
            if f.startswith(".syncthing.") and f.endswith(".tmp"):
                return True
    return False


# ---------------------------------------------------------------------------
# Copy integrity check
# ---------------------------------------------------------------------------

def verify_copy(src, dest):
    try:
        if os.path.getsize(src) != os.path.getsize(dest):
            logger.warning(f"Size mismatch after copy: {src} vs {dest}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Could not verify copy for {dest}: {e}")
        return False


def safe_copy(src, dest):
    shutil.copy2(src, dest)
    apply_open_permissions(dest)
    if not verify_copy(src, dest):
        logger.error(f"Integrity check failed, removing bad copy: {dest}")
        try:
            os.remove(dest)
        except Exception:
            pass
        return False
    return True


# ---------------------------------------------------------------------------
# Path resolution — fuzzy-match against existing library folders
# ---------------------------------------------------------------------------

def rename_folder_if_needed(current_path, ideal_name):
    parent       = os.path.dirname(current_path)
    current_name = os.path.basename(current_path)
    if current_name == ideal_name:
        return current_path
    new_path = os.path.join(parent, ideal_name)
    if os.path.exists(new_path):
        logger.warning(
            f"Cannot rename '{current_name}' to '{ideal_name}' — target exists, using existing."
        )
        return new_path
    try:
        os.rename(current_path, new_path)
        logger.info(f"Renamed folder: '{current_name}' -> '{ideal_name}'")
        return new_path
    except Exception as e:
        logger.error(f"Failed to rename '{current_name}' -> '{ideal_name}': {e}")
        return current_path


def resolve_path(ideal):
    parts = ideal.split("/")
    if len(parts) < 3:
        return ideal

    category  = parts[0]
    title_dir = parts[1]
    rest      = parts[2:]

    category_path = os.path.join(BASE_MEDIA_DIR, category)
    if not os.path.isdir(category_path):
        return ideal

    existing_folders = [
        d for d in os.listdir(category_path)
        if os.path.isdir(os.path.join(category_path, d))
    ]
    if not existing_folders:
        return ideal

    matched = ask_gemini_match_folder(client, title_dir, existing_folders)
    if matched:
        matched_path = os.path.join(category_path, matched)
        final_path   = rename_folder_if_needed(matched_path, title_dir)
        final_name   = os.path.basename(final_path)
        new_path     = "/".join([category, final_name] + rest)
        if new_path != ideal:
            logger.info(f"Path resolved: '{ideal}' -> '{new_path}'")
        return new_path

    return ideal


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def has_any_videos(folder_path):
    """Return True if the folder contains at least one ready video file (ignores tracker)."""
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.startswith('.'):
                continue
            if os.path.splitext(f)[1].lower() not in VIDEO_EXTENSIONS:
                continue
            if is_file_ready(os.path.join(root, f)):
                return True
    return False


def find_all_videos(folder_path, records):
    """Return (abs_path, filename) for all real, ready video files in folder."""
    videos = []
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in sorted(files):
            if f.startswith('.'):
                continue
            if os.path.splitext(f)[1].lower() not in VIDEO_EXTENSIONS:
                continue
            abs_path = os.path.join(root, f)
            if not is_file_ready(abs_path):
                continue
            if is_already_processed(records, f):
                continue
            if is_sample_file(client, f):
                continue
            videos.append((abs_path, f))
    return videos


def find_sidecars(folder_path, video_stem):
    sidecars = []
    stem_lower = video_stem.lower()
    for f in os.listdir(folder_path):
        if f.startswith('.'):
            continue
        if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
            continue
        if os.path.isdir(os.path.join(folder_path, f)):
            continue
        if os.path.splitext(f)[0].lower().startswith(stem_lower):
            sidecars.append(f)
    return sidecars


def copy_single_video_with_sidecars(src_folder, video_abs, video_filename, ideal_path):
    dest_abs    = os.path.join(BASE_MEDIA_DIR, ideal_path)
    dest_dir    = os.path.dirname(dest_abs)
    plex_filename = os.path.basename(ideal_path)
    video_stem  = os.path.splitext(video_filename)[0]

    os.makedirs(dest_dir, exist_ok=True)
    apply_open_permissions(dest_dir)

    if not os.path.exists(dest_abs):
        ok = safe_copy(video_abs, dest_abs)
        if not ok:
            logger.error(f"Integrity check failed: {video_filename}")
            return False
        logger.info(f"  Copied video: {video_filename} -> {plex_filename}")
    else:
        logger.info(f"  Already exists, skipping: {plex_filename}")

    for sidecar in find_sidecars(src_folder, video_stem):
        src  = os.path.join(src_folder, sidecar)
        dest = os.path.join(dest_dir, sidecar)
        if not os.path.exists(dest):
            safe_copy(src, dest)
            logger.info(f"  Copied sidecar: {sidecar}")

    subs_src = os.path.join(src_folder, "Subs")
    if os.path.isdir(subs_src):
        subs_dest = os.path.join(dest_dir, "Subs")
        if not os.path.exists(subs_dest):
            try:
                shutil.copytree(subs_src, subs_dest)
                logger.info("  Copied Subs/ subdir")
            except Exception as e:
                logger.warning(f"  Failed to copy Subs/: {e}")

    return True


def process_folder_copy(src_folder, dest_dir, primary_video_file, plex_filename):
    """Copy all files from src_folder to dest_dir. Returns True if all copies succeeded."""
    os.makedirs(dest_dir, exist_ok=True)
    apply_open_permissions(dest_dir)
    all_ok = True

    for item in os.listdir(src_folder):
        if item.startswith('.'):
            continue
        src = os.path.join(src_folder, item)
        if os.path.isdir(src):
            logger.info(f"  Copying subdir: {item}/")
            ok = process_folder_copy(src, os.path.join(dest_dir, item), primary_video_file, plex_filename)
            if not ok:
                all_ok = False
        elif os.path.isfile(src):
            dest_name = plex_filename if item == primary_video_file else item
            dest      = os.path.join(dest_dir, dest_name)
            if not os.path.exists(dest):
                ok = safe_copy(src, dest)
                if ok:
                    logger.info(f"  Copied: {item} -> {dest_name}")
                else:
                    logger.error(f"  Integrity check failed: {item}")
                    all_ok = False
            else:
                logger.info(f"  Already exists, skipping: {dest_name}")

    return all_ok


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------

def run_audit():
    sync_folder = os.getenv("SYNC_DIR", os.path.join(BASE_MEDIA_DIR, "Sync"))
    if not os.path.exists(sync_folder):
        logger.warning(f"Sync folder not found: {sync_folder}")
        return

    logger.info("--- Starting Audit ---")
    records = load_tracker()

    processed = skipped = failed = 0
    failures = []

    for item in sorted(os.listdir(sync_folder)):
        if item.startswith('.'):
            continue

        item_path = os.path.join(sync_folder, item)
        ext       = os.path.splitext(item)[1].lower()

        # --- Bare video file ---
        if os.path.isfile(item_path) and ext in VIDEO_EXTENSIONS:
            if is_already_processed(records, item):
                logger.info(f"SKIP (already processed): {item}")
                skipped += 1
                continue
            if not is_file_ready(item_path):
                skipped += 1
                continue
            if is_sample_file(client, item):
                skipped += 1
                continue
            logger.info(f"Classifying file: {item}")
            ideal, confidence = ask_gemini_classify(client, item)
            if not ideal:
                logger.warning(f"No usable Plex path for: {item} — marking failed")
                mark_processed(records, item, status="failed")
                failures.append(item)
                failed += 1
                continue
            ideal, confidence = tmdb_enrich(ideal, confidence)
            if confidence == "LOW":
                flag_for_review(item, ideal)
                skipped += 1
                continue
            check_category(item, ideal)
            ideal = resolve_path(ideal)
            dest  = os.path.join(BASE_MEDIA_DIR, ideal)
            if not os.path.exists(dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                ok = safe_copy(item_path, dest)
                if ok:
                    logger.info(f"Copied: {item} -> {ideal}")
                    mark_processed(records, item, status="ok")
                    send_file_added(ideal)
                    processed += 1
                else:
                    logger.error(f"Integrity check failed for: {item} — will retry next run")
                    failures.append(item)
                    failed += 1
            else:
                logger.info(f"Destination already exists, skipping: {dest}")
                mark_processed(records, item, status="ok")
                skipped += 1

        # --- Folder ---
        elif os.path.isdir(item_path):
            if has_syncthing_tmp(item_path):
                logger.info(f"SKIP (Syncthing still transferring): {item}")
                skipped += 1
                continue

            videos = find_all_videos(item_path, records)
            if not videos:
                if has_any_videos(item_path):
                    logger.info(f"SKIP (all files already processed): {item}")
                else:
                    logger.info(f"No video found in folder (may still be transferring): {item}")
                skipped += 1
                continue

            # Multi-episode folder — each video tracked individually
            if len(videos) > 1:
                logger.info(f"Multi-episode folder: {item} ({len(videos)} videos)")
                folder_ok = True
                newly_copied = []
                newly_added_ideals = []
                for video_abs, video_filename in videos:
                    logger.info(f"  Classifying: {video_filename}")
                    ideal, confidence = ask_gemini_classify(client, video_filename)
                    if not ideal:
                        logger.warning(f"  No usable Plex path for: {video_filename} — marking failed")
                        mark_processed(records, video_filename, status="failed")
                        failures.append(video_filename)
                        failed += 1
                        folder_ok = False
                        continue
                    ideal, confidence = tmdb_enrich(ideal, confidence)
                    if confidence == "LOW":
                        flag_for_review(video_filename, ideal)
                        skipped += 1
                        continue
                    check_category(video_filename, ideal)
                    ideal = resolve_path(ideal)
                    ok = copy_single_video_with_sidecars(
                        src_folder=os.path.dirname(video_abs),
                        video_abs=video_abs,
                        video_filename=video_filename,
                        ideal_path=ideal,
                    )
                    if ok:
                        mark_processed(records, video_filename, status="ok")
                        newly_copied.append(os.path.join(BASE_MEDIA_DIR, ideal))
                        newly_added_ideals.append(ideal)
                        processed += 1
                    else:
                        mark_processed(records, video_filename, status="failed")
                        failures.append(video_filename)
                        failed += 1
                        folder_ok = False

                # Rollback: if any file in the folder failed, remove successfully copied ones
                if not folder_ok and newly_copied:
                    logger.warning(f"Partial failure in {item} — rolling back {len(newly_copied)} copied file(s)")
                    for copied_path in newly_copied:
                        try:
                            os.remove(copied_path)
                            logger.info(f"  Rolled back: {copied_path}")
                        except Exception as e:
                            logger.error(f"  Rollback failed for {copied_path}: {e}")
                elif folder_ok:
                    for ideal in newly_added_ideals:
                        send_file_added(ideal)

            # Single-episode folder — tracked by video filename
            else:
                video_abs, video_filename = videos[0]
                logger.info(f"Classifying folder: {item} (anchor: {video_filename})")
                ideal, confidence = ask_gemini_classify(client, video_filename)
                if not ideal:
                    logger.warning(f"No usable Plex path for folder: {item} — marking failed")
                    mark_processed(records, video_filename, status="failed")
                    failures.append(item)
                    failed += 1
                    continue
                ideal, confidence = tmdb_enrich(ideal, confidence)
                if confidence == "LOW":
                    flag_for_review(video_filename, ideal)
                    skipped += 1
                    continue
                check_category(video_filename, ideal)
                ideal     = resolve_path(ideal)
                dest_dir  = os.path.join(BASE_MEDIA_DIR, os.path.dirname(ideal))
                plex_name = os.path.basename(ideal)
                ok        = process_folder_copy(item_path, dest_dir, video_filename, plex_name)
                if ok:
                    logger.info(f"Copied folder: {item} -> {ideal}")
                    mark_processed(records, video_filename, status="ok")
                    send_file_added(ideal)
                    processed += 1
                else:
                    logger.error(f"Partial copy failure for folder: {item} — marking failed")
                    mark_processed(records, video_filename, status="failed")
                    failures.append(item)
                    failed += 1

    save_tracker(records)

    summary = f"Processed: {processed} | Skipped: {skipped} | Failed: {failed}"
    logger.info(f"--- Audit Complete | {summary} ---")
    if processed > 0 or failed > 0:
        send_run_summary(processed, skipped, failed, failures=failures)


if __name__ == "__main__":
    run_audit()
