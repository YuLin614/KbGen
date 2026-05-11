from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kbgen.quality import compute_quality, _extract_snapshot_paths, _grade


def test_grade_boundaries():
    assert _grade(90) == "A"
    assert _grade(85) == "A"
    assert _grade(84) == "B"
    assert _grade(70) == "B"
    assert _grade(69) == "C"
    assert _grade(50) == "C"
    assert _grade(49) == "D"
    assert _grade(0)  == "D"


def test_extract_snapshot_paths_empty():
    snapshot = {"m": {}}
    assert _extract_snapshot_paths(snapshot) == set()


def test_extract_snapshot_paths_uses_p_field():
    snapshot = {
        "m": {
            "api": {"p": ["api/routes.py", "api/handlers.py"]},
            "auth": {"p": ["auth/middleware.py"]},
        }
    }
    result = _extract_snapshot_paths(snapshot)
    assert result == {"api/routes.py", "api/handlers.py", "auth/middleware.py"}


def test_extract_snapshot_paths_missing_p_field():
    snapshot = {
        "m": {
            "api": {"r": ["routing"], "s": "routes"},  # no "p" key
        }
    }
    assert _extract_snapshot_paths(snapshot) == set()


def test_compute_quality_no_snapshot(tmp_path):
    result = compute_quality(tmp_path)
    assert result["available"] is False
    assert result["coverage_pct"] is None
    assert result["staleness_pct"] is None
    assert result["quality_score"] is None
    assert result["grade"] is None


def test_compute_quality_with_snapshot(tmp_path):
    # Create source files
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "routes.py").write_text("def get(): pass")
    (tmp_path / "api" / "handlers.py").write_text("def handle(): pass")
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "middleware.py").write_text("def check(): pass")

    # Create snapshot referencing 2 of 3 files
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    snapshot = {
        "m": {
            "api": {"p": ["api/routes.py", "api/handlers.py"]},
        }
    }
    (ai_dir / "snapshot.kb").write_text(json.dumps(snapshot))
    (ai_dir / "meta.json").write_text(json.dumps({
        "generated_at": "2026-01-01T00:00:00",
        "tool_version": "kbgen@0.1",
        "confidence": "high",
    }))

    # Mock git to return no changed files
    with patch("kbgen.quality._git_changed_since", return_value=set()):
        result = compute_quality(tmp_path)

    assert result["available"] is True
    assert result["total_files"] == 3
    assert result["covered_files"] == 2
    assert abs(result["coverage_pct"] - 66.7) < 1.0
    assert result["staleness_pct"] == 0.0
    assert result["stale_files"] == 0
    # score = 66.7 * 0.6 + 100 * 0.4 ≈ 40.0 + 40.0 = 80.0 (within tolerance)
    assert abs(result["quality_score"] - 80.0) < 1.0
    assert result["grade"] == "B"


def test_compute_quality_staleness(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "routes.py").write_text("def get(): pass")
    (tmp_path / "api" / "handlers.py").write_text("def handle(): pass")

    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    snapshot = {"m": {"api": {"p": ["api/routes.py", "api/handlers.py"]}}}
    (ai_dir / "snapshot.kb").write_text(json.dumps(snapshot))
    (ai_dir / "meta.json").write_text(json.dumps({"generated_at": "2026-01-01T00:00:00"}))

    # 1 of 2 snapshot files changed
    with patch("kbgen.quality._git_changed_since", return_value={"api/routes.py"}):
        result = compute_quality(tmp_path)

    assert result["stale_files"] == 1
    assert abs(result["staleness_pct"] - 50.0) < 1.0


def test_compute_quality_mtime_fallback(tmp_path):
    """When git is unavailable, falls back to mtime comparison."""
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "routes.py").write_text("def get(): pass")

    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    snapshot = {"m": {"api": {"p": ["api/routes.py"]}}}
    (ai_dir / "snapshot.kb").write_text(json.dumps(snapshot))
    (ai_dir / "meta.json").write_text(json.dumps({"generated_at": "2000-01-01T00:00:00"}))

    # git returns None (unavailable), mtime fallback should detect change
    # routes.py was created now, so mtime > year-2000 cutoff
    with patch("kbgen.quality._git_changed_since", return_value=None):
        result = compute_quality(tmp_path)

    assert result["available"] is True
    assert result["stale_files"] == 1
    assert result["staleness_pct"] == 100.0


def test_compute_quality_corrupt_snapshot(tmp_path):
    """Corrupt snapshot.kb returns available=False, no crash."""
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    (ai_dir / "snapshot.kb").write_text("not valid json {{{")

    result = compute_quality(tmp_path)
    assert result["available"] is False
    assert result["quality_score"] is None


def test_compute_quality_missing_generated_at(tmp_path):
    """Missing generated_at in meta.json → staleness stays 0, no crash."""
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "routes.py").write_text("def get(): pass")
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    snapshot = {"m": {"api": {"p": ["api/routes.py"]}}}
    (ai_dir / "snapshot.kb").write_text(json.dumps(snapshot))
    (ai_dir / "meta.json").write_text(json.dumps({"tool_version": "kbgen@0.1"}))

    with patch("kbgen.quality._git_changed_since", return_value=set()):
        result = compute_quality(tmp_path)

    assert result["available"] is True
    assert result["staleness_pct"] == 0.0
    assert result["stale_files"] == 0


def test_bar_full_and_empty():
    from kbgen.quality import _bar
    assert _bar(100) == "██████████"
    assert _bar(0) == "░░░░░░░░░░"
    assert len(_bar(50)) == 10


def test_format_quality_terminal_no_snapshot():
    from kbgen.quality import format_quality_terminal
    q = {"available": False}
    out = format_quality_terminal(q)
    assert "n/a" in out


def test_format_quality_terminal_with_data():
    from kbgen.quality import format_quality_terminal
    q = {
        "available": True, "quality_score": 80.0, "grade": "B",
        "coverage_pct": 66.7, "staleness_pct": 0.0,
        "covered_files": 2, "total_files": 3, "stale_files": 0,
    }
    out = format_quality_terminal(q)
    assert "B" in out
    assert "80" in out
    assert "66.7" in out
