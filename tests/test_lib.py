import pytest
import urllib.error
from unittest.mock import patch, MagicMock

from lib import (
    _parse_plex_path,
    _rebuild_plex_path,
    check_category,
    tmdb_enrich,
    ask_gemini_classify,
    is_sample_file,
)


# ---------------------------------------------------------------------------
# _parse_plex_path
# ---------------------------------------------------------------------------

class TestParsePlexPath:
    @pytest.mark.parametrize("path,expected", [
        (
            "Movies/A Knight's Tale (2001)/A Knight's Tale (2001).mp4",
            ("Movies", "A Knight's Tale", "2001"),
        ),
        (
            "TV/Breaking Bad/Season 02/Breaking Bad - S02E05.mkv",
            ("TV", "Breaking Bad", None),
        ),
        (
            "Anime Series/Kimetsu no Yaiba/Season 04/Kimetsu no Yaiba - S04E10.mkv",
            ("Anime Series", "Kimetsu no Yaiba", None),
        ),
        (
            "Anime Movies/Spirited Away (2001)/Spirited Away (2001).mkv",
            ("Anime Movies", "Spirited Away", "2001"),
        ),
    ])
    def test_parse(self, path, expected):
        assert _parse_plex_path(path) == expected

    def test_short_path_no_crash(self):
        cat, title, year = _parse_plex_path("Movies")
        assert cat == "Movies"
        assert title == ""
        assert year is None


# ---------------------------------------------------------------------------
# _rebuild_plex_path
# ---------------------------------------------------------------------------

class TestRebuildPlexPath:
    def test_movie_title_and_year(self):
        result = _rebuild_plex_path(
            "Movies/A Knights Tale (2001)/A Knights Tale (2001).mp4",
            "A Knight's Tale", "2001",
        )
        assert result == "Movies/A Knight's Tale (2001)/A Knight's Tale (2001).mp4"

    def test_movie_title_correction(self):
        result = _rebuild_plex_path(
            "Movies/I Robot (2004)/I Robot (2004).mkv",
            "I, Robot", "2004",
        )
        assert result == "Movies/I, Robot (2004)/I, Robot (2004).mkv"

    def test_movie_no_year(self):
        result = _rebuild_plex_path(
            "Movies/Unknown/Unknown.mkv",
            "Unknown", None,
        )
        assert result == "Movies/Unknown/Unknown.mkv"

    def test_tv_title_correction(self):
        result = _rebuild_plex_path(
            "TV/breaking bad/Season 02/breaking bad - S02E05.mkv",
            "Breaking Bad", "2008",
        )
        assert result == "TV/Breaking Bad/Season 02/Breaking Bad - S02E05.mkv"

    def test_anime_series_rename(self):
        result = _rebuild_plex_path(
            "Anime Series/Demon Slayer/Season 04/Demon Slayer - S04E10.mkv",
            "Kimetsu no Yaiba", "2019",
        )
        assert result == "Anime Series/Kimetsu no Yaiba/Season 04/Kimetsu no Yaiba - S04E10.mkv"


# ---------------------------------------------------------------------------
# check_category
# ---------------------------------------------------------------------------

class TestCheckCategory:
    def test_episode_in_movie_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="bea-tidy"):
            check_category("show.s01e01.mkv", "Movies/Some Show/Some Show.mkv")
        assert "mismatch" in caplog.text.lower()

    def test_no_episode_in_tv_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="bea-tidy"):
            check_category("some.movie.2024.mkv", "TV/Some Movie/Season 01/Some Movie - S01E01.mkv")
        assert "mismatch" in caplog.text.lower()

    def test_correct_movie_no_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="bea-tidy"):
            check_category("knight.tale.2001.mkv", "Movies/A Knight's Tale (2001)/A Knight's Tale (2001).mkv")
        assert "mismatch" not in caplog.text.lower()

    def test_correct_tv_no_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="bea-tidy"):
            check_category("breaking.bad.s02e05.mkv", "TV/Breaking Bad/Season 02/Breaking Bad - S02E05.mkv")
        assert "mismatch" not in caplog.text.lower()

    def test_unknown_category_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="bea-tidy"):
            check_category("file.mkv", "UnknownCategory/Something/file.mkv")
        assert "unknown category" in caplog.text.lower()


# ---------------------------------------------------------------------------
# tmdb_enrich
# ---------------------------------------------------------------------------

def _mock_urlopen(body: bytes):
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestTmdbEnrich:
    def test_no_api_key_passthrough(self, monkeypatch):
        monkeypatch.setenv("TMDB_API_KEY", "")
        path, conf = tmdb_enrich("Movies/Test (2020)/Test (2020).mkv", "HIGH")
        assert path == "Movies/Test (2020)/Test (2020).mkv"
        assert conf == "HIGH"

    def test_movie_match_fixes_title_and_upgrades_confidence(self, monkeypatch):
        monkeypatch.setenv("TMDB_API_KEY", "fake")
        body = b'{"results": [{"title": "I, Robot", "release_date": "2004-07-16"}]}'
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(body)):
            path, conf = tmdb_enrich("Movies/I Robot (2004)/I Robot (2004).mkv", "MEDIUM")
        assert "I, Robot" in path
        assert "(2004)" in path
        assert conf == "HIGH"

    def test_tv_match_fixes_title(self, monkeypatch):
        monkeypatch.setenv("TMDB_API_KEY", "fake")
        body = b'{"results": [{"name": "Breaking Bad", "first_air_date": "2008-01-20"}]}'
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(body)):
            path, conf = tmdb_enrich("TV/breaking bad/Season 02/breaking bad - S02E05.mkv", "MEDIUM")
        assert "Breaking Bad" in path
        assert conf == "HIGH"

    def test_no_results_unchanged(self, monkeypatch):
        monkeypatch.setenv("TMDB_API_KEY", "fake")
        body = b'{"results": []}'
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(body)):
            path, conf = tmdb_enrich("Movies/Obscure (2019)/Obscure (2019).mkv", "LOW")
        assert conf == "LOW"

    def test_network_failure_unchanged(self, monkeypatch):
        monkeypatch.setenv("TMDB_API_KEY", "fake")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            path, conf = tmdb_enrich("Movies/Test (2020)/Test (2020).mkv", "MEDIUM")
        assert conf == "MEDIUM"

    def test_already_correct_still_high(self, monkeypatch):
        monkeypatch.setenv("TMDB_API_KEY", "fake")
        body = b'{"results": [{"title": "A Knight\'s Tale", "release_date": "2001-05-11"}]}'
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(body)):
            path, conf = tmdb_enrich("Movies/A Knight's Tale (2001)/A Knight's Tale (2001).mp4", "HIGH")
        assert conf == "HIGH"
        assert "A Knight's Tale" in path


# ---------------------------------------------------------------------------
# ask_gemini_classify
# ---------------------------------------------------------------------------

class TestAskGeminiClassify:
    def test_valid_high_confidence(self, monkeypatch):
        monkeypatch.setattr("lib.GEMINI_RATE_SLEEP", 0)
        client = MagicMock()
        client.models.generate_content.return_value.text = (
            "Movies/A Knight's Tale (2001)/A Knight's Tale (2001).mp4\nHIGH"
        )
        path, conf = ask_gemini_classify(client, "A.Knights.Tale.2001.mkv")
        assert path == "Movies/A Knight's Tale (2001)/A Knight's Tale (2001).mp4"
        assert conf == "HIGH"

    def test_medium_confidence(self, monkeypatch):
        monkeypatch.setattr("lib.GEMINI_RATE_SLEEP", 0)
        client = MagicMock()
        client.models.generate_content.return_value.text = (
            "Movies/Some Film (2019)/Some Film (2019).mkv\nMEDIUM"
        )
        _, conf = ask_gemini_classify(client, "some.film.mkv")
        assert conf == "MEDIUM"

    def test_low_confidence(self, monkeypatch):
        monkeypatch.setattr("lib.GEMINI_RATE_SLEEP", 0)
        client = MagicMock()
        client.models.generate_content.return_value.text = (
            "Movies/Movie (2019)/Movie (2019).mkv\nLOW"
        )
        _, conf = ask_gemini_classify(client, "movie.2019.mkv")
        assert conf == "LOW"

    def test_invalid_path_returns_none(self, monkeypatch):
        monkeypatch.setattr("lib.GEMINI_RATE_SLEEP", 0)
        client = MagicMock()
        client.models.generate_content.return_value.text = "not a valid path"
        path, conf = ask_gemini_classify(client, "garbage.mkv")
        assert path is None
        assert conf is None

    def test_retries_three_times_on_exception(self, monkeypatch):
        monkeypatch.setattr("lib.GEMINI_RATE_SLEEP", 0)
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("API error")
        path, conf = ask_gemini_classify(client, "test.mkv")
        assert path is None
        assert client.models.generate_content.call_count == 3

    def test_unknown_confidence_defaults_to_high(self, monkeypatch):
        monkeypatch.setattr("lib.GEMINI_RATE_SLEEP", 0)
        client = MagicMock()
        client.models.generate_content.return_value.text = (
            "Movies/Test (2020)/Test (2020).mkv\nVERY_CONFIDENT"
        )
        _, conf = ask_gemini_classify(client, "test.2020.mkv")
        assert conf == "HIGH"

    def test_strips_leading_slash(self, monkeypatch):
        monkeypatch.setattr("lib.GEMINI_RATE_SLEEP", 0)
        client = MagicMock()
        client.models.generate_content.return_value.text = (
            "/Movies/Test (2020)/Test (2020).mkv\nHIGH"
        )
        path, _ = ask_gemini_classify(client, "test.mkv")
        assert not path.startswith("/")


# ---------------------------------------------------------------------------
# is_sample_file
# ---------------------------------------------------------------------------

class TestIsSampleFile:
    def test_skip_response(self, monkeypatch):
        monkeypatch.setattr("lib.GEMINI_RATE_SLEEP", 0)
        client = MagicMock()
        client.models.generate_content.return_value.text = "SKIP"
        assert is_sample_file(client, "movie.sample.mkv") is True

    def test_real_response(self, monkeypatch):
        monkeypatch.setattr("lib.GEMINI_RATE_SLEEP", 0)
        client = MagicMock()
        client.models.generate_content.return_value.text = "REAL"
        assert is_sample_file(client, "movie.s01e01.mkv") is False

    def test_api_failure_defaults_to_real(self, monkeypatch):
        monkeypatch.setattr("lib.GEMINI_RATE_SLEEP", 0)
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("API error")
        assert is_sample_file(client, "movie.mkv") is False
