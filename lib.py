import os
import re
import json
import time
import logging
import urllib.request
import urllib.parse
import urllib.error
from google import genai
from google.genai import types

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("bea-tidy")

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.m4v'}
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_RATE_SLEEP = 4.5  # stay well under free-tier RPM limit

CATEGORIES = ["Movies", "TV", "Anime Movies", "Anime Series"]

CLASSIFY_INSTRUCTION = """You are a media file classifier that outputs Plex-compatible folder paths.

Given a filename, identify the title, year (if present), season, and episode (if present), then return exactly two lines:
  Line 1: the Plex-standard relative path
  Line 2: your confidence level — one of: HIGH, MEDIUM, LOW

MOVIES format:
  Movies/Title (Year)/Title (Year).ext

TV format:
  TV/Show Title/Season XX/Show Title - SXXEXX.ext

Anime Movies format:
  Anime Movies/Title (Year)/Title (Year).ext

Anime Series format:
  Anime Series/Show Title/Season XX/Show Title - SXXEXX.ext

Rules:
- Line 1: ONLY the relative path, no explanation, no markdown
- Line 2: ONLY one word — HIGH, MEDIUM, or LOW
  - HIGH: title, category, year/episode all clearly identified from the filename
  - MEDIUM: title identified but year guessed from knowledge, or category is ambiguous (e.g. could be TV or Anime)
  - LOW: filename is very ambiguous, garbled, or you are guessing the title
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
  Output:
    Movies/A Knight's Tale (2001)/A Knight's Tale (2001).mp4
    HIGH

  Input:  Euphoria.US.S03E06.1080p.HEVC.x265-MeGusta.mkv
  Output:
    TV/Euphoria/Season 03/Euphoria - S03E06.mkv
    HIGH

  Input:  movie.2019.mkv
  Output:
    Movies/Movie (2019)/Movie (2019).mkv
    LOW

  Input:  Kimetsu.no.Yaiba.S04E10.1080p.mkv
  Output:
    Anime Series/Kimetsu no Yaiba/Season 04/Kimetsu no Yaiba - S04E10.mkv
    HIGH

  Input:  Breaking.Bad.S02E05E06.1080p.mkv
  Output:
    TV/Breaking Bad/Season 02/Breaking Bad - S02E05.mkv
    HIGH

  Input:  jersey.shore.s01e01.dvdrip.xvid-nodlabs.avi
  Output:
    TV/Jersey Shore/Season 01/Jersey Shore - S01E01.avi
    HIGH

  Input:  I.Robot.2004.1080p.BluRay.x265.mkv
  Output:
    Movies/I, Robot (2004)/I, Robot (2004).mkv
    HIGH"""


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


def make_client():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise EnvironmentError("GEMINI_API_KEY is not set.")
    return genai.Client(api_key=key)


def ask_gemini_classify(client, filename):
    """
    Returns (path, confidence) where confidence is 'HIGH', 'MEDIUM', or 'LOW'.
    Returns (None, None) if classification fails after retries.
    """
    prompt = f'Classify this media file: "{filename}"'
    config = types.GenerateContentConfig(
        system_instruction=CLASSIFY_INSTRUCTION,
        temperature=0.0
    )
    for attempt in range(3):
        try:
            time.sleep(GEMINI_RATE_SLEEP)
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=config
            )
            lines      = [l.strip() for l in response.text.strip().splitlines() if l.strip()]
            path       = lines[0].lstrip("/") if lines else ""
            confidence = lines[1].upper() if len(lines) > 1 else "HIGH"
            if confidence not in ("HIGH", "MEDIUM", "LOW"):
                confidence = "HIGH"
            logger.info(f"Gemini classify: {repr(path)} [{confidence}]")
            if path and path.count("/") >= 2:
                return path, confidence
            logger.warning(f"Gemini classify response invalid — raw: {repr(response.text)}")
        except Exception as e:
            logger.warning(f"Classify retry {attempt + 1} for {filename}: {e}")
            time.sleep(2 ** attempt)
    return None, None


def ask_gemini_match_folder(client, title, existing_folders):
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
            time.sleep(GEMINI_RATE_SLEEP)
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=config
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


def is_sample_file(client, filename):
    prompt = f'Is this a real media file or should it be skipped? "{filename}"'
    config = types.GenerateContentConfig(
        system_instruction=SAMPLE_CHECK_INSTRUCTION,
        temperature=0.0
    )
    try:
        time.sleep(GEMINI_RATE_SLEEP)
        response = client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt, config=config
        )
        result = response.text.strip()
        if result == "SKIP":
            logger.info(f"Gemini flagged as sample/extra, skipping: {filename}")
            return True
        return False
    except Exception as e:
        logger.warning(f"Sample check failed for {filename}: {e} — treating as real")
        return False


TMDB_BASE = "https://api.themoviedb.org/3"


def _tmdb_request(endpoint, params):
    """GET a TMDB endpoint. Returns parsed JSON dict or None on any failure."""
    api_key = os.getenv("TMDB_API_KEY", "").strip()
    if not api_key:
        return None
    params = dict(params, api_key=api_key)
    url = f"{TMDB_BASE}{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        logger.warning(f"TMDB request failed: {e}")
        return None


def _parse_plex_path(ideal):
    """
    Extract (category, title, year) from a Plex path.
      Movies/A Knight's Tale (2001)/...  -> ("Movies", "A Knight's Tale", "2001")
      TV/Breaking Bad/Season 02/...      -> ("TV", "Breaking Bad", None)
    """
    parts      = ideal.split("/")
    category   = parts[0] if parts else ""
    title_dir  = parts[1] if len(parts) > 1 else ""
    year_match = re.search(r'\((\d{4})\)$', title_dir)
    if year_match:
        return category, title_dir[:year_match.start()].strip(), year_match.group(1)
    return category, title_dir, None


def _rebuild_plex_path(ideal, canonical_title, canonical_year):
    """Swap Gemini's title/year in a Plex path with TMDB's canonical values."""
    parts    = ideal.split("/")
    category = parts[0]
    rest     = parts[2:]
    is_movie = category in ("Movies", "Anime Movies")

    if is_movie:
        year_str  = f" ({canonical_year})" if canonical_year else ""
        title_dir = f"{canonical_title}{year_str}"
        ext       = os.path.splitext(rest[0])[1] if rest else ""
        return "/".join([category, title_dir, f"{canonical_title}{year_str}{ext}"])
    else:
        if len(rest) >= 2:
            ep_match = re.match(r'^.+? - (S\d+E\d+.*)$', rest[1])
            episode  = f"{canonical_title} - {ep_match.group(1)}" if ep_match else rest[1]
            return "/".join([category, canonical_title, rest[0], episode])
        return "/".join([category, canonical_title] + rest)


def tmdb_enrich(ideal, confidence):
    """
    Verify and enrich a Gemini-classified path using TMDB.
    - Silently skips if TMDB_API_KEY is not set.
    - On a match: returns path rebuilt with canonical title/year, confidence -> HIGH.
    - On no match: returns inputs unchanged.
    TMDB enrichment runs before the LOW-confidence check so a TMDB-confirmed
    result is never incorrectly flagged for manual review.
    """
    if not os.getenv("TMDB_API_KEY", "").strip():
        return ideal, confidence

    category, title, year = _parse_plex_path(ideal)
    if not title or category not in CATEGORIES:
        return ideal, confidence

    is_movie = category in ("Movies", "Anime Movies")
    endpoint = "/search/movie" if is_movie else "/search/tv"
    params   = {"query": title, "include_adult": "false"}
    if year:
        params["year" if is_movie else "first_air_date_year"] = year

    data    = _tmdb_request(endpoint, params)
    results = (data or {}).get("results", [])

    # Retry without year — filename year might be wrong or use airdate vs release year
    if not results and year:
        params.pop("year", None)
        params.pop("first_air_date_year", None)
        results = (_tmdb_request(endpoint, params) or {}).get("results", [])

    if not results:
        logger.info(f"TMDB: no match for '{title}' — keeping Gemini result")
        return ideal, confidence

    top = results[0]
    if is_movie:
        canonical_title = top.get("title") or title
        canonical_year  = (top.get("release_date") or "")[:4] or year
    else:
        canonical_title = top.get("name") or title
        canonical_year  = (top.get("first_air_date") or "")[:4] or year

    new_path = _rebuild_plex_path(ideal, canonical_title, canonical_year)
    if new_path != ideal:
        logger.info(f"TMDB enriched: '{ideal}' -> '{new_path}'")
    else:
        logger.info(f"TMDB confirmed: '{ideal}'")

    return new_path, "HIGH"


def check_category(filename, path):
    """
    Log a warning if the classified category doesn't match what we'd expect
    from the filename. Purely informational — does not block processing.
    """
    category = path.split("/")[0] if path else ""
    if category not in CATEGORIES:
        logger.warning(f"Unknown category '{category}' for: {filename}")
        return
    # Rough heuristics to flag obvious mismatches worth a human glance
    name_lower = filename.lower()
    is_episode = bool(__import__("re").search(r"s\d{2}e\d{2}", name_lower))
    if is_episode and category in ("Movies", "Anime Movies"):
        logger.warning(f"Category mismatch? '{filename}' has episode pattern but classified as '{category}'")
    if not is_episode and category in ("TV", "Anime Series"):
        logger.warning(f"Category mismatch? '{filename}' has no episode pattern but classified as '{category}'")


def apply_open_permissions(path):
    try:
        os.chmod(path, 0o777 if os.path.isdir(path) else 0o666)
    except Exception:
        pass
