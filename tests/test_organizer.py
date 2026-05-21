import time
import pytest

import organizer


# ---------------------------------------------------------------------------
# Tracker — load / save / prune
# ---------------------------------------------------------------------------

class TestLoadTracker:
    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(organizer, "TRACKER_FILE", str(tmp_path / "tracker.txt"))
        assert organizer.load_tracker() == {}

    def test_roundtrip(self, tmp_path, monkeypatch):
        tracker = str(tmp_path / "tracker.txt")
        monkeypatch.setattr(organizer, "TRACKER_FILE", tracker)
        records = {"movie.mkv": (time.time(), "ok")}
        organizer.save_tracker(records)
        loaded = organizer.load_tracker()
        assert "movie.mkv" in loaded
        assert loaded["movie.mkv"][1] == "ok"

    def test_expired_ok_entries_pruned(self, tmp_path, monkeypatch):
        tracker = str(tmp_path / "tracker.txt")
        monkeypatch.setattr(organizer, "TRACKER_FILE", tracker)
        old = time.time() - organizer.PROCESSED_TTL - 1
        with open(tracker, "w") as f:
            f.write(f"old.mkv|{old}|ok\n")
        assert "old.mkv" not in organizer.load_tracker()

    def test_expired_failed_entries_pruned(self, tmp_path, monkeypatch):
        tracker = str(tmp_path / "tracker.txt")
        monkeypatch.setattr(organizer, "TRACKER_FILE", tracker)
        old = time.time() - organizer.FAILED_RETRY - 1
        with open(tracker, "w") as f:
            f.write(f"failed.mkv|{old}|failed\n")
        assert "failed.mkv" not in organizer.load_tracker()

    def test_valid_failed_entry_kept(self, tmp_path, monkeypatch):
        tracker = str(tmp_path / "tracker.txt")
        monkeypatch.setattr(organizer, "TRACKER_FILE", tracker)
        recent = time.time() - 60
        with open(tracker, "w") as f:
            f.write(f"recent.mkv|{recent}|failed\n")
        assert "recent.mkv" in organizer.load_tracker()

    def test_malformed_lines_skipped(self, tmp_path, monkeypatch):
        tracker = str(tmp_path / "tracker.txt")
        monkeypatch.setattr(organizer, "TRACKER_FILE", tracker)
        with open(tracker, "w") as f:
            f.write("garbage line\n")
            f.write(f"good.mkv|{time.time()}|ok\n")
        records = organizer.load_tracker()
        assert "good.mkv" in records

    def test_blank_lines_ignored(self, tmp_path, monkeypatch):
        tracker = str(tmp_path / "tracker.txt")
        monkeypatch.setattr(organizer, "TRACKER_FILE", tracker)
        with open(tracker, "w") as f:
            f.write(f"\n\ngood.mkv|{time.time()}|ok\n\n")
        assert len(organizer.load_tracker()) == 1


# ---------------------------------------------------------------------------
# is_already_processed
# ---------------------------------------------------------------------------

class TestIsAlreadyProcessed:
    def test_not_in_records(self):
        assert organizer.is_already_processed({}, "new.mkv") is False

    def test_ok_within_ttl(self):
        records = {"movie.mkv": (time.time(), "ok")}
        assert organizer.is_already_processed(records, "movie.mkv") is True

    def test_ok_past_ttl(self):
        records = {"movie.mkv": (time.time() - organizer.PROCESSED_TTL - 1, "ok")}
        assert organizer.is_already_processed(records, "movie.mkv") is False

    def test_failed_within_retry_window(self):
        records = {"movie.mkv": (time.time() - 60, "failed")}
        assert organizer.is_already_processed(records, "movie.mkv") is True

    def test_failed_past_retry_window(self):
        records = {"movie.mkv": (time.time() - organizer.FAILED_RETRY - 1, "failed")}
        assert organizer.is_already_processed(records, "movie.mkv") is False


# ---------------------------------------------------------------------------
# mark_processed
# ---------------------------------------------------------------------------

class TestMarkProcessed:
    def test_adds_entry(self):
        records = {}
        organizer.mark_processed(records, "movie.mkv")
        assert "movie.mkv" in records
        assert records["movie.mkv"][1] == "ok"

    def test_failed_status(self):
        records = {}
        organizer.mark_processed(records, "movie.mkv", status="failed")
        assert records["movie.mkv"][1] == "failed"

    def test_overwrites_existing(self):
        records = {"movie.mkv": (0.0, "failed")}
        organizer.mark_processed(records, "movie.mkv", status="ok")
        assert records["movie.mkv"][1] == "ok"

    def test_timestamp_is_recent(self):
        records = {}
        before = time.time()
        organizer.mark_processed(records, "movie.mkv")
        assert records["movie.mkv"][0] >= before


# ---------------------------------------------------------------------------
# is_file_ready
# ---------------------------------------------------------------------------

class TestIsFileReady:
    def test_zero_byte_not_ready(self, tmp_path):
        f = tmp_path / "empty.mkv"
        f.write_bytes(b"")
        assert organizer.is_file_ready(str(f)) is False

    def test_normal_file_ready(self, tmp_path):
        f = tmp_path / "movie.mkv"
        f.write_bytes(b"fake video content")
        assert organizer.is_file_ready(str(f)) is True

    def test_missing_file_not_ready(self, tmp_path):
        assert organizer.is_file_ready(str(tmp_path / "nope.mkv")) is False


# ---------------------------------------------------------------------------
# has_syncthing_tmp
# ---------------------------------------------------------------------------

class TestHasSyncthingTmp:
    def test_clean_folder(self, tmp_path):
        (tmp_path / "movie.mkv").write_bytes(b"data")
        assert organizer.has_syncthing_tmp(str(tmp_path)) is False

    def test_tmp_in_root(self, tmp_path):
        (tmp_path / ".syncthing.movie.mkv.tmp").write_bytes(b"")
        assert organizer.has_syncthing_tmp(str(tmp_path)) is True

    def test_tmp_in_subdir(self, tmp_path):
        sub = tmp_path / "Season 01"
        sub.mkdir()
        (sub / ".syncthing.ep1.mkv.tmp").write_bytes(b"")
        assert organizer.has_syncthing_tmp(str(tmp_path)) is True

    def test_regular_hidden_file_ignored(self, tmp_path):
        (tmp_path / ".DS_Store").write_bytes(b"")
        assert organizer.has_syncthing_tmp(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# verify_copy
# ---------------------------------------------------------------------------

class TestVerifyCopy:
    def test_matching_sizes(self, tmp_path):
        src  = tmp_path / "src.mkv"
        dest = tmp_path / "dest.mkv"
        src.write_bytes(b"fake video data")
        dest.write_bytes(b"fake video data")
        assert organizer.verify_copy(str(src), str(dest)) is True

    def test_mismatched_sizes(self, tmp_path):
        src  = tmp_path / "src.mkv"
        dest = tmp_path / "dest.mkv"
        src.write_bytes(b"fake video data")
        dest.write_bytes(b"truncated")
        assert organizer.verify_copy(str(src), str(dest)) is False

    def test_missing_dest(self, tmp_path):
        src = tmp_path / "src.mkv"
        src.write_bytes(b"data")
        assert organizer.verify_copy(str(src), str(tmp_path / "nope.mkv")) is False


# ---------------------------------------------------------------------------
# flag_for_review
# ---------------------------------------------------------------------------

class TestFlagForReview:
    def test_writes_entry(self, tmp_path, monkeypatch):
        review = str(tmp_path / "review.txt")
        monkeypatch.setattr(organizer, "REVIEW_FILE", review)
        organizer.flag_for_review("ambiguous.mkv", "Movies/Ambiguous (2020)/Ambiguous (2020).mkv")
        content = open(review).read()
        assert "ambiguous.mkv" in content
        assert "Ambiguous (2020)" in content

    def test_appends_multiple(self, tmp_path, monkeypatch):
        review = str(tmp_path / "review.txt")
        monkeypatch.setattr(organizer, "REVIEW_FILE", review)
        organizer.flag_for_review("file1.mkv", "Movies/File1 (2020)/File1 (2020).mkv")
        organizer.flag_for_review("file2.mkv", "TV/File2/Season 01/File2 - S01E01.mkv")
        lines = [l for l in open(review).readlines() if l.strip()]
        assert len(lines) == 2
