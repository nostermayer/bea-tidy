import csv
import io
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

import cleanup


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class TestReport:
    def test_add_increments_count(self):
        r = cleanup.Report()
        r.add("COMPLIANT", "Movies/Foo/Foo.mkv")
        r.add("COMPLIANT", "Movies/Bar/Bar.mkv")
        r.add("RENAME", "Movies/bad.mkv", proposed_path="Movies/Good (2020)/Good (2020).mkv")
        assert r.counts["COMPLIANT"] == 2
        assert r.counts["RENAME"] == 1

    def test_add_stores_all_fields(self):
        r = cleanup.Report()
        r.add("RENAME", "cur", proposed_path="prop", confidence="HIGH", notes="note")
        row = r.rows[0]
        assert row["action"] == "RENAME"
        assert row["current_path"] == "cur"
        assert row["proposed_path"] == "prop"
        assert row["confidence"] == "HIGH"
        assert row["notes"] == "note"

    def test_write_produces_valid_csv(self, tmp_path):
        r = cleanup.Report()
        r.add("COMPLIANT", "Movies/Foo (2020)/Foo (2020).mkv")
        r.add("RENAME", "Movies/bad.mkv", proposed_path="Movies/Good (2020)/Good (2020).mkv", confidence="HIGH")
        out = tmp_path / "report.csv"
        r.write(str(out))
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        assert rows[0]["action"] == "COMPLIANT"
        assert rows[1]["action"] == "RENAME"
        assert rows[1]["proposed_path"] == "Movies/Good (2020)/Good (2020).mkv"

    def test_write_utf8_paths(self, tmp_path):
        r = cleanup.Report()
        r.add("RENAME", "Movies/Ñoño (2020)/Ñoño (2020).mkv", proposed_path="Movies/Nono (2020)/Nono (2020).mkv")
        out = tmp_path / "report.csv"
        r.write(str(out))
        with open(out, encoding="utf-8") as f:
            content = f.read()
        assert "Ñoño" in content

    def test_total_count(self):
        r = cleanup.Report()
        for i in range(5):
            r.add("COMPLIANT", f"Movies/Movie{i}/Movie{i}.mkv")
        assert len(r.rows) == 5


# ---------------------------------------------------------------------------
# safe_rename
# ---------------------------------------------------------------------------

class TestSafeRename:
    def test_dry_run_returns_true_without_moving(self, tmp_path):
        src  = tmp_path / "src.mkv"
        dest = tmp_path / "dest.mkv"
        src.write_bytes(b"data")
        assert cleanup.safe_rename(str(src), str(dest), dry_run=True) is True
        assert src.exists()
        assert not dest.exists()

    def test_same_path_returns_true(self, tmp_path):
        f = tmp_path / "file.mkv"
        f.write_bytes(b"x")
        assert cleanup.safe_rename(str(f), str(f), dry_run=False) is True

    def test_dest_exists_returns_false(self, tmp_path):
        src  = tmp_path / "src.mkv"
        dest = tmp_path / "dest.mkv"
        src.write_bytes(b"a")
        dest.write_bytes(b"b")
        assert cleanup.safe_rename(str(src), str(dest), dry_run=False) is False
        assert src.exists()

    def test_execute_moves_file(self, tmp_path):
        src  = tmp_path / "old.mkv"
        dest = tmp_path / "subdir" / "new.mkv"
        src.write_bytes(b"content")
        result = cleanup.safe_rename(str(src), str(dest), dry_run=False)
        assert result is True
        assert dest.exists()
        assert not src.exists()

    def test_execute_creates_parent_dirs(self, tmp_path):
        src  = tmp_path / "file.mkv"
        dest = tmp_path / "a" / "b" / "c" / "file.mkv"
        src.write_bytes(b"x")
        cleanup.safe_rename(str(src), str(dest), dry_run=False)
        assert dest.exists()


# ---------------------------------------------------------------------------
# move_sidecars
# ---------------------------------------------------------------------------

class TestMoveSidecars:
    def test_same_dir_is_noop(self, tmp_path):
        d = tmp_path / "dir"
        d.mkdir()
        srt = d / "show.s01e01.srt"
        srt.write_text("sub")
        cleanup.move_sidecars(str(d), str(d), "show.s01e01", dry_run=False, mode_tag="[TEST]")
        assert srt.exists()

    def test_moves_matching_sidecar(self, tmp_path):
        src_dir  = tmp_path / "src"
        dest_dir = tmp_path / "dest"
        src_dir.mkdir()
        dest_dir.mkdir()
        srt = src_dir / "badly.named.s01e01.srt"
        srt.write_text("sub")
        cleanup.move_sidecars(str(src_dir), str(dest_dir), "badly.named.s01e01",
                              dry_run=False, mode_tag="[TEST]")
        assert (dest_dir / "badly.named.s01e01.srt").exists()
        assert not srt.exists()

    def test_skips_video_files(self, tmp_path):
        src_dir  = tmp_path / "src"
        dest_dir = tmp_path / "dest"
        src_dir.mkdir()
        dest_dir.mkdir()
        vid = src_dir / "other.episode.mkv"
        vid.write_bytes(b"video")
        cleanup.move_sidecars(str(src_dir), str(dest_dir), "other.episode",
                              dry_run=False, mode_tag="[TEST]")
        assert vid.exists()
        assert not (dest_dir / "other.episode.mkv").exists()

    def test_skips_non_matching_files(self, tmp_path):
        src_dir  = tmp_path / "src"
        dest_dir = tmp_path / "dest"
        src_dir.mkdir()
        dest_dir.mkdir()
        unrelated = src_dir / "readme.txt"
        unrelated.write_text("hi")
        cleanup.move_sidecars(str(src_dir), str(dest_dir), "movie.2020",
                              dry_run=False, mode_tag="[TEST]")
        assert unrelated.exists()

    def test_moves_subs_directory(self, tmp_path):
        src_dir  = tmp_path / "src"
        dest_dir = tmp_path / "dest"
        subs     = src_dir / "Subs"
        src_dir.mkdir()
        dest_dir.mkdir()
        subs.mkdir()
        (subs / "en.srt").write_text("sub")
        cleanup.move_sidecars(str(src_dir), str(dest_dir), "movie",
                              dry_run=False, mode_tag="[TEST]")
        assert (dest_dir / "Subs" / "en.srt").exists()
        assert not subs.exists()

    def test_dry_run_does_not_move(self, tmp_path):
        src_dir  = tmp_path / "src"
        dest_dir = tmp_path / "dest"
        src_dir.mkdir()
        dest_dir.mkdir()
        srt = src_dir / "movie.srt"
        srt.write_text("sub")
        cleanup.move_sidecars(str(src_dir), str(dest_dir), "movie",
                              dry_run=True, mode_tag="[DRY RUN]")
        assert srt.exists()
        assert not (dest_dir / "movie.srt").exists()

    def test_double_extension_sidecar(self, tmp_path):
        src_dir  = tmp_path / "src"
        dest_dir = tmp_path / "dest"
        src_dir.mkdir()
        dest_dir.mkdir()
        nfo = src_dir / "movie.mp4.nfo"
        nfo.write_text("<nfo/>")
        cleanup.move_sidecars(str(src_dir), str(dest_dir), "movie",
                              dry_run=False, mode_tag="[TEST]")
        assert (dest_dir / "movie.mp4.nfo").exists()


# ---------------------------------------------------------------------------
# cleanup_empty_dirs
# ---------------------------------------------------------------------------

class TestCleanupEmptyDirs:
    def test_removes_empty_subdir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        cleanup.cleanup_empty_dirs(str(tmp_path), dry_run=False)
        assert not empty.exists()

    def test_leaves_non_empty_subdir(self, tmp_path):
        full = tmp_path / "full"
        full.mkdir()
        (full / "file.mkv").write_bytes(b"x")
        cleanup.cleanup_empty_dirs(str(tmp_path), dry_run=False)
        assert full.exists()

    def test_does_not_remove_root(self, tmp_path):
        cleanup.cleanup_empty_dirs(str(tmp_path), dry_run=False)
        assert tmp_path.exists()

    def test_dry_run_leaves_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        cleanup.cleanup_empty_dirs(str(tmp_path), dry_run=True)
        assert empty.exists()

    def test_removes_nested_empty_dirs(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        cleanup.cleanup_empty_dirs(str(tmp_path), dry_run=False)
        assert not (tmp_path / "a").exists()


# ---------------------------------------------------------------------------
# run_from_csv
# ---------------------------------------------------------------------------

class TestRunFromCsv:
    def _write_csv(self, path, rows):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=cleanup.Report.COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def test_applies_rename_row(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BASE_MEDIA_DIR", str(tmp_path))

        src = tmp_path / "Movies" / "bad" / "bad.mkv"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"video")

        csv_path = tmp_path / "cleanup_plan.csv"
        self._write_csv(str(csv_path), [{
            "action": "RENAME",
            "current_path": "Movies/bad/bad.mkv",
            "proposed_path": "Movies/Good (2020)/Good (2020).mkv",
            "confidence": "HIGH",
            "notes": "",
        }])

        args = MagicMock()
        args.execute = True
        args.from_csv = "__default__"

        cleanup.run_from_csv(args)

        assert (tmp_path / "Movies" / "Good (2020)" / "Good (2020).mkv").exists()
        assert not src.exists()

    def test_skips_missing_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BASE_MEDIA_DIR", str(tmp_path))

        csv_path = tmp_path / "cleanup_plan.csv"
        self._write_csv(str(csv_path), [{
            "action": "RENAME",
            "current_path": "Movies/ghost/ghost.mkv",
            "proposed_path": "Movies/Ghost (2020)/Ghost (2020).mkv",
            "confidence": "HIGH",
            "notes": "",
        }])

        args = MagicMock()
        args.execute = True
        args.from_csv = "__default__"

        cleanup.run_from_csv(args)
        # No exception — just logged and skipped
        assert not (tmp_path / "Movies" / "Ghost (2020)" / "Ghost (2020).mkv").exists()

    def test_skips_existing_destination(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BASE_MEDIA_DIR", str(tmp_path))

        src  = tmp_path / "Movies" / "bad" / "bad.mkv"
        dest = tmp_path / "Movies" / "Good (2020)" / "Good (2020).mkv"
        src.parent.mkdir(parents=True)
        dest.parent.mkdir(parents=True)
        src.write_bytes(b"src")
        dest.write_bytes(b"existing")

        csv_path = tmp_path / "cleanup_plan.csv"
        self._write_csv(str(csv_path), [{
            "action": "RENAME",
            "current_path": "Movies/bad/bad.mkv",
            "proposed_path": "Movies/Good (2020)/Good (2020).mkv",
            "confidence": "HIGH",
            "notes": "",
        }])

        args = MagicMock()
        args.execute = True
        args.from_csv = "__default__"

        cleanup.run_from_csv(args)
        assert dest.read_bytes() == b"existing"
        assert src.exists()

    def test_ignores_non_rename_rows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BASE_MEDIA_DIR", str(tmp_path))

        csv_path = tmp_path / "cleanup_plan.csv"
        self._write_csv(str(csv_path), [
            {"action": "COMPLIANT", "current_path": "Movies/Good/Good.mkv",
             "proposed_path": "", "confidence": "", "notes": ""},
            {"action": "SKIP_LOW_CONFIDENCE", "current_path": "Movies/Idk/Idk.mkv",
             "proposed_path": "Movies/Maybe (2020)/Maybe (2020).mkv",
             "confidence": "LOW", "notes": "Review manually"},
        ])

        args = MagicMock()
        args.execute = True
        args.from_csv = "__default__"

        cleanup.run_from_csv(args)
        assert not (tmp_path / "Movies" / "Maybe (2020)").exists()

    def test_dry_run_does_not_move(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BASE_MEDIA_DIR", str(tmp_path))

        src = tmp_path / "Movies" / "bad" / "bad.mkv"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"video")

        csv_path = tmp_path / "cleanup_plan.csv"
        self._write_csv(str(csv_path), [{
            "action": "RENAME",
            "current_path": "Movies/bad/bad.mkv",
            "proposed_path": "Movies/Good (2020)/Good (2020).mkv",
            "confidence": "HIGH",
            "notes": "",
        }])

        args = MagicMock()
        args.execute = False
        args.from_csv = "__default__"

        cleanup.run_from_csv(args)
        assert src.exists()
        assert not (tmp_path / "Movies" / "Good (2020)").exists()

    def test_missing_csv_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BASE_MEDIA_DIR", str(tmp_path))
        args = MagicMock()
        args.execute = True
        args.from_csv = "__default__"
        with pytest.raises(SystemExit):
            cleanup.run_from_csv(args)

    def test_custom_csv_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BASE_MEDIA_DIR", str(tmp_path))

        src = tmp_path / "Movies" / "bad" / "bad.mkv"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"video")

        custom_csv = tmp_path / "my_plan.csv"
        self._write_csv(str(custom_csv), [{
            "action": "RENAME",
            "current_path": "Movies/bad/bad.mkv",
            "proposed_path": "Movies/Good (2020)/Good (2020).mkv",
            "confidence": "HIGH",
            "notes": "",
        }])

        args = MagicMock()
        args.execute = True
        args.from_csv = str(custom_csv)

        cleanup.run_from_csv(args)
        assert (tmp_path / "Movies" / "Good (2020)" / "Good (2020).mkv").exists()

    def test_moves_sidecars_alongside_video(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BASE_MEDIA_DIR", str(tmp_path))

        src_dir = tmp_path / "Movies" / "bad"
        src_dir.mkdir(parents=True)
        (src_dir / "bad.mkv").write_bytes(b"video")
        (src_dir / "bad.en.srt").write_text("sub")

        csv_path = tmp_path / "cleanup_plan.csv"
        self._write_csv(str(csv_path), [{
            "action": "RENAME",
            "current_path": "Movies/bad/bad.mkv",
            "proposed_path": "Movies/Good (2020)/Good (2020).mkv",
            "confidence": "HIGH",
            "notes": "",
        }])

        args = MagicMock()
        args.execute = True
        args.from_csv = "__default__"

        cleanup.run_from_csv(args)
        dest_dir = tmp_path / "Movies" / "Good (2020)"
        assert (dest_dir / "Good (2020).mkv").exists()
        assert (dest_dir / "bad.en.srt").exists()
