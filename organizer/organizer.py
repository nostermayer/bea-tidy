import os
import re
import shutil
import time
import logging
from logging.handlers import RotatingFileHandler
from google import genai
from google.genai import types

# Configuration
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
BASE_MEDIA_DIR = "/mnt/tank/media"
LOG_FILE = os.path.join(BASE_MEDIA_DIR, "organizer.log")
TRACKER_FILE = os.path.join(BASE_MEDIA_DIR, ".processed_history.txt")
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.m4v'}
PROCESSED_TTL = 1209600  # 14 days in seconds
FAILED_RETRY  = 7200     # retry failed items after 2 hours (matches cron interval)

# Setup Logging
logger = logging.getLogger("Organizer")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=1 * 1024 * 1024, backupCount=3)
formatter = logging.Formatter('%(asctime)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

TARGET_FOLDERS = {"sync": os.path.join(BASE_MEDIA_DIR, "Sync")}

if not GEMINI_KEY:
    logger.error("GEMINI_API_KEY missing.")
    exit(1)

client = genai.Client(api_key=GEMINI_KEY)


# ---------------------------------------------------------------------------
# Tracker — keyed by individual filename (not folder name for multi-episode)
# Format: <filename>|<timestamp>|<status>
# ---------------------------------------------------------------------------

def _load_tracker():
    records = {}
    if not os.path.exists(TRACKER_FILE):
        return records
    with open(TRACKER_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            try:
                name = parts[0]
                timestamp = float(parts[1])
                status = parts[2] if len(parts) > 2 else "ok"
                records[name] = (timestamp, status)
            except (IndexError, ValueError):
                continue
    return records


def is_already_processed(name):
    records = _load_tracker()
    if name not in records:
        return False
    timestamp, status = records[name]
    age = time.time() - timestamp
    if age >= PROCESSED_TTL:
        return False
    if status == "failed":
        return age < FAILED_RETRY
    return True


def mark_as_processed(name, status="ok"):
    records = _load_tracker()
    records[name] = (time.time(), status)
    tmp = TRACKER_FILE + ".tmp"
    with open(tmp, "w") as f:
        for rec_name, (ts, st) in records.items():
            f.write(f"{rec_name}|{ts}|{st}\n")
    os.replace(tmp, TRACKER_FILE)


# ---------------------------------------------------------------------------
# File readiness — skip 0-byte files (partial Syncthing transfers)
# ---------------------------------------------------------------------------

def is_file_ready(path):
    """Return False if file is 0 bytes — likely a partial Syncthing transfer."""
    try:
        size = os.path.getsize(path)
        if size == 0:
            logger.info(f"Skipping 0-byte file (partial transfer): {path}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Could not stat file {path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Copy integrity check
# ---------------------------------------------------------------------------

def verify_copy(src, dest):
    try:
        src_size  = os.path.getsize(src)
        dest_size = os.path.getsize(dest)
        if src_size != dest_size:
            logger.warning(f"Size mismatch after copy: {src} ({src_size}B) vs {dest} ({dest_size}B)")
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
# Gemini calls
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
- Examples:
    Show.S01E01E02.mkv        -> TV/Show/Season 01/Show - S01E01.mkv
    Show.S02E03-E04.1080p.mkv -> TV/Show/Season 02/Show - S02E03.mkv

Specials:
- If the filename contains S00 or the word Special/OVA, treat as a special
- Examples:
    Show.S00E01.Special.mkv -> TV/Show/Season 00/Show - S00E01.mkv
    Show.OVA1.mkv           -> Anime Series/Show/Season 00/Show - S00E01.mkv

Examples:
  Input:  A.Knights.Tale.2001.1080p.BluRay.x265.mp4
  Output: Movies/A Knight's Tale (2001)/A Knight's Tale (2001).mp4

  Input:  Euphoria.US.S03E06.1080p.HEVC.x265-MeGusta.mkv
  Output: TV/Euphoria/Season 03/Euphoria - S03E06.mkv

  Input:  The.Drama.2026.1080p.WEBRip.x265.10bit.AAC5.1-LAMA.mp4
  Output: Movies/The Drama (2026)/The Drama (2026).mp4

  Input:  Kimetsu.no.Yaiba.S04E10.1080p.mkv
  Output: Anime Series/Kimetsu no Yaiba/Season 04/Kimetsu no Yaiba - S04E10.mkv

  Input:  Breaking.Bad.S02E05E06.1080p.mkv
  Output: TV/Breaking Bad/Season 02/Breaking Bad - S02E05.mkv

  Input:  jersey.shore.s01e01.dvdrip.xvid-nodlabs.avi
  Output: TV/Jersey Shore/Season 01/Jersey Shore - S01E01.avi

  Input:  Cowboy.Bebop.S00E01.Remix.mkv
  Output: Anime Series/Cowboy Bebop/Season 00/Cowboy Bebop - S00E01.mkv

  Input:  I.Robot.2004.1080p.BluRay.x265.mkv
  Output: Movies/I, Robot (2004)/I, Robot (2004).mkv"""


FOLDER_MATCH_INSTRUCTION = """You are a media library folder matcher.

You will be given:
1. A media title that needs to be placed in a library
2. A list of existing folder names in that library

Your job: decide if any existing folder is the same show/movie as the given title.
Account for year differences, alternate spellings, unicode vs ascii (e.g. Shogun vs Shōgun),
'The' prefix differences, and minor punctuation differences.

If a match exists, return ONLY that exact existing folder name, character for character.
If no match exists, return ONLY the word: NEW

No explanation. One line only."""


SAMPLE_CHECK_INSTRUCTION = """You are a media file classifier.

Given a filename, decide if it is a real media file worth keeping (a full episode, movie, or OVA)
or something that should be skipped (a sample, trailer, featurette, bonus clip, or extra).

Reply with ONLY one of:
  REAL
  SKIP

No explanation. One word only.

Examples:
  jersey.shore.s01e01.dvdrip.xvid-nodlabs.avi        -> REAL
  jersey.shore.s01e01.dvdrip.xvid-nodlabs.sample.avi -> SKIP
  A.Knights.Tale.2001.1080p.sample.mp4               -> SKIP
  A.Knights.Tale.2001.1080p.BluRay.x265.mp4          -> REAL
  movie-trailer.mp4                                   -> SKIP
  featurette-making-of.mkv                            -> SKIP
  Euphoria.S03E06.1080p.mkv                           -> REAL"""


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
            logger.info(f"Gemini classify returned: {repr(result)}")
            if result and result.count("/") >= 2:
                return result
            logger.warning(f"Gemini classify response invalid — raw: {repr(result)}")
        except Exception as e:
            logger.warning(f"Retry {attempt + 1} for {filename}: {e}")
            time.sleep(2 ** attempt)
    return None


def ask_gemini_match_folder(title, existing_folders):
    if not existing_folders:
        return None
    folder_list = "\n".join(f"- {f}" for f in existing_folders)
    prompt = f'Title to place: "{title}"\n\nExisting folders:\n{folder_list}'
    config = types.GenerateContentConfig(
        system_instruction=FOLDER_MATCH_INSTRUCTION,
        temperature=0.0
    )
    for attempt in range(3):
        try:
            time.sleep(4.5)
            response = client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt, config=config
            )
            result = response.text.strip()
            logger.info(f"Gemini folder match returned: {repr(result)}")
            if result == "NEW":
                return None
            if result in existing_folders:
                return result
            logger.warning(f"Gemini returned folder not in list: {repr(result)}")
        except Exception as e:
            logger.warning(f"Folder match retry {attempt + 1}: {e}")
            time.sleep(2 ** attempt)
    return None


def is_sample_file(filename):
    prompt = f'Is this a real media file or should it be skipped? "{filename}"'
    config = types.GenerateContentConfig(
        system_instruction=SAMPLE_CHECK_INSTRUCTION,
        temperature=0.0
    )
    try:
        time.sleep(4.5)
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt, config=config
        )
        result = response.text.strip()
        if result == "SKIP":
            logger.info(f"  Gemini flagged as sample/extra, skipping: {filename}")
            return True
        return False
    except Exception as e:
        logger.warning(f"  Sample check failed for {filename}: {e} — treating as real")
        return False


def rename_folder_if_needed(current_path, ideal_name):
    parent = os.path.dirname(current_path)
    current_name = os.path.basename(current_path)
    if current_name == ideal_name:
        return current_path
    new_path = os.path.join(parent, ideal_name)
    if os.path.exists(new_path):
        logger.warning(
            f"Cannot rename '{current_name}' to '{ideal_name}' — target already exists. "
            f"Using existing '{ideal_name}'."
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

    existing_folders = []
    if os.path.isdir(category_path):
        existing_folders = [
            d for d in os.listdir(category_path)
            if os.path.isdir(os.path.join(category_path, d))
        ]

    if not existing_folders:
        return ideal

    matched = ask_gemini_match_folder(title_dir, existing_folders)

    if matched:
        matched_path = os.path.join(category_path, matched)
        final_path = rename_folder_if_needed(matched_path, title_dir)
        final_name = os.path.basename(final_path)
        new_path = "/".join([category, final_name] + rest)
        if new_path != ideal:
            logger.info(f"Path resolved: '{ideal}' -> '{new_path}'")
        return new_path

    return ideal


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def find_all_videos(folder_path):
    """
    Return list of (abs_path, filename) for all real video files in folder.
    Skips: hidden files, 0-byte files, Gemini-flagged samples/extras.
    """
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
            if is_sample_file(f):
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


def apply_open_permissions(path):
    try:
        os.chmod(path, 0o777 if os.path.isdir(path) else 0o666)
    except Exception:
        pass


def copy_single_video_with_sidecars(src_folder, video_abs, video_filename, ideal_path):
    dest_abs = os.path.join(BASE_MEDIA_DIR, ideal_path)
    dest_dir = os.path.dirname(dest_abs)
    plex_filename = os.path.basename(ideal_path)
    video_stem = os.path.splitext(video_filename)[0]

    os.makedirs(dest_dir, exist_ok=True)
    apply_open_permissions(dest_dir)

    if not os.path.exists(dest_abs):
        ok = safe_copy(video_abs, dest_abs)
        if ok:
            logger.info(f"  Copied video: {video_filename} -> {plex_filename}")
        else:
            logger.error(f"  Integrity check failed: {video_filename}")
            return False
    else:
        logger.info(f"  Already exists, skipping: {plex_filename}")

    sidecars = find_sidecars(src_folder, video_stem)
    for sidecar in sidecars:
        src = os.path.join(src_folder, sidecar)
        dest = os.path.join(dest_dir, sidecar)
        if not os.path.exists(dest):
            safe_copy(src, dest)
            logger.info(f"  Copied sidecar: {sidecar}")

    subs_src = os.path.join(src_folder, "Subs")
    if os.path.isdir(subs_src):
        subs_dest = os.path.join(dest_dir, "Subs")
        if not os.path.exists(subs_dest):
            shutil.copytree(subs_src, subs_dest)
            logger.info(f"  Copied Subs/ subdir")

    return True


def process_folder_copy(src_folder, dest_dir, primary_video_file, plex_filename):
    os.makedirs(dest_dir, exist_ok=True)
    apply_open_permissions(dest_dir)

    for item in os.listdir(src_folder):
        if item.startswith('.'):
            continue
        src = os.path.join(src_folder, item)
        if os.path.isdir(src):
            logger.info(f"  Copying subdir: {item}/")
            process_folder_copy(src, os.path.join(dest_dir, item), primary_video_file, plex_filename)
        elif os.path.isfile(src):
            dest_name = plex_filename if item == primary_video_file else item
            dest = os.path.join(dest_dir, dest_name)
            if not os.path.exists(dest):
                ok = safe_copy(src, dest)
                if ok:
                    logger.info(f"  Copied: {item} -> {dest_name}")
                else:
                    logger.error(f"  Integrity check failed: {item}")
            else:
                logger.info(f"  Already exists, skipping: {dest_name}")


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------

def run_audit():
    sync_folder = TARGET_FOLDERS["sync"]
    if not os.path.exists(sync_folder):
        logger.warning(f"Sync folder not found: {sync_folder}")
        return

    logger.info("--- Starting Audit ---")

    for item in sorted(os.listdir(sync_folder)):
        if item.startswith('.'):
            continue

        item_path = os.path.join(sync_folder, item)
        ext = os.path.splitext(item)[1].lower()

        # --- Bare video file — tracked by filename ---
        if os.path.isfile(item_path) and ext in VIDEO_EXTENSIONS:
            if is_already_processed(item):
                logger.info(f"SKIP (already processed): {item}")
                continue
            if not is_file_ready(item_path):
                continue
            logger.info(f"Classifying file: {item}")
            ideal = ask_gemini_classify(item)
            if not ideal:
                logger.warning(f"No usable Plex path for: {item} — marking failed")
                mark_as_processed(item, status="failed")
                continue
            ideal = resolve_path(ideal)
            dest = os.path.join(BASE_MEDIA_DIR, ideal)
            if not os.path.exists(dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                ok = safe_copy(item_path, dest)
                if ok:
                    logger.info(f"Copied: {item} -> {ideal}")
                    mark_as_processed(item, status="ok")
                else:
                    logger.error(f"Integrity check failed for: {item} — will retry next run")
            else:
                logger.info(f"Destination already exists, skipping: {dest}")
                mark_as_processed(item, status="ok")

        # --- Folder ---
        elif os.path.isdir(item_path):
            videos = find_all_videos(item_path)

            if not videos:
                logger.info(f"No video found in folder (may still be transferring): {item}")
                continue

            # --- Multi-episode folder — each video tracked individually ---
            if len(videos) > 1:
                logger.info(f"Multi-episode folder: {item} ({len(videos)} videos)")
                for video_abs, video_filename in videos:
                    if is_already_processed(video_filename):
                        logger.info(f"  SKIP (already processed): {video_filename}")
                        continue
                    logger.info(f"  Classifying: {video_filename}")
                    ideal = ask_gemini_classify(video_filename)
                    if not ideal:
                        logger.warning(f"  No usable Plex path for: {video_filename} — marking failed")
                        mark_as_processed(video_filename, status="failed")
                        continue
                    ideal = resolve_path(ideal)
                    ok = copy_single_video_with_sidecars(
                        src_folder=os.path.dirname(video_abs),
                        video_abs=video_abs,
                        video_filename=video_filename,
                        ideal_path=ideal
                    )
                    mark_as_processed(video_filename, status="ok" if ok else "failed")

            # --- Single-episode folder — tracked by video filename ---
            else:
                video_abs, video_filename = videos[0]
                if is_already_processed(video_filename):
                    logger.info(f"SKIP (already processed): {video_filename}")
                    continue
                logger.info(f"Classifying folder: {item} (anchor: {video_filename})")
                ideal = ask_gemini_classify(video_filename)
                if not ideal:
                    logger.warning(f"No usable Plex path for folder: {item} — marking failed")
                    mark_as_processed(video_filename, status="failed")
                    continue
                ideal = resolve_path(ideal)
                dest_dir = os.path.join(BASE_MEDIA_DIR, os.path.dirname(ideal))
                plex_filename = os.path.basename(ideal)
                process_folder_copy(item_path, dest_dir, video_filename, plex_filename)
                logger.info(f"Copied folder: {item} -> {ideal}")
                mark_as_processed(video_filename, status="ok")

    logger.info("--- Audit Complete ---")


if __name__ == "__main__":
    run_audit()
