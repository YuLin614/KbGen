from __future__ import annotations

from kbgen.report import generate_html, _sparkline, _svg_line_chart


def test_sparkline_empty():
    assert _sparkline([]) == ""


def test_sparkline_single():
    result = _sparkline([1000])
    assert len(result) == 1
    assert result in "▁▂▃▄▅▆▇█"


def test_sparkline_ascending():
    result = _sparkline([100, 200, 300, 400])
    assert len(result) == 4
    bars = "▁▂▃▄▅▆▇█"
    assert bars.index(result[-1]) >= bars.index(result[0])


def test_svg_line_chart_empty():
    svg = _svg_line_chart([], [], width=400, height=100)
    assert "<svg" in svg
    assert "</svg>" in svg


def test_svg_line_chart_two_series():
    snap_vals = [1000, 900, 800]
    no_snap_vals = [1500, 1400, 1600]
    svg = _svg_line_chart(snap_vals, no_snap_vals, width=400, height=100)
    assert "<polyline" in svg
    assert 'stroke="' in svg


def test_generate_html_minimal():
    html = generate_html(sessions=[], quality=None, project_name="test")
    assert "<!DOCTYPE html>" in html
    assert "kbGen Dashboard" in html
    assert "test" in html


def test_generate_html_with_sessions():
    sessions = [
        {"ts": "2026-05-01T10:00:00", "snapshot": True, "input_tokens": 1000,
         "output_tokens": 200, "cache_read_tokens": 5000, "cache_write_tokens": 0,
         "duration_s": 60.0, "requests": 3, "snapshot_quality_score": 80},
        {"ts": "2026-05-02T10:00:00", "snapshot": False, "input_tokens": 1500,
         "output_tokens": 300, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "duration_s": 90.0, "requests": 4, "snapshot_quality_score": None},
    ]
    quality = {
        "available": True, "coverage_pct": 82.3, "staleness_pct": 5.0,
        "quality_score": 78.9, "grade": "B", "total_files": 100,
        "covered_files": 82, "stale_files": 3, "generated_at": "2026-05-01T00:00:00"
    }
    html = generate_html(sessions=sessions, quality=quality, project_name="myapp")
    assert "myapp" in html
    assert "82.3" in html
    assert "2026-05-01" in html
    assert "<table" in html
    assert "<polyline" in html
