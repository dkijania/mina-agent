"""The read-only Buildkite client: job selection, log cleaning, ranged tails,
and the triage_nightly phase that uses it. No network: _get is replaced."""
import json

import pytest

from mina_agent import buildkite as bk
from mina_agent import phases


def _job(state, key, retried=False, soft=False, type_="script", jid=None):
    return {"type": type_, "state": state, "step_key": key, "name": key, "retried": retried,
            "soft_failed": soft, "id": jid or f"id-{key}-{retried}", "exit_status": 1,
            "web_url": f"https://bk/{key}"}


BUILD = {"number": 1741, "state": "failed", "branch": "compatible", "commit": "a801fbde",
         "message": "Scheduled build\nsecond line", "source": "schedule", "web_url": "https://bk/1741",
         "jobs": [_job("passed", "ok"),
                  _job("failed", "flaky", retried=True), _job("passed", "flaky"),
                  _job("failed", "hard"),
                  _job("failed", "perf", soft=True),
                  _job("canceled", "big-log"),
                  _job("waiting_failed", None, type_="waiter"),
                  _job("broken", "downstream")]}


@pytest.fixture
def fake_get(monkeypatch):
    calls = []

    def install(body, headers=None):
        def _get(url, h=None):
            calls.append((url, h))
            return (body if isinstance(body, bytes) else json.dumps(body).encode()), (headers or {})
        monkeypatch.setattr(bk, "_get", _get)
        return calls
    return install


def test_failed_jobs_use_the_latest_attempt_only():
    keys = [(f["step_key"], f["soft_failed"]) for f in bk.failed_jobs(BUILD)]
    assert keys == [("hard", False), ("perf", True), ("big-log", False)]


def test_state_counts_skip_waiters_and_retried_originals():
    assert bk.state_counts(BUILD) == {"passed": 2, "failed": 2, "canceled": 1, "broken": 1}


def test_build_summary(fake_get):
    calls = fake_get(BUILD)
    b = bk.build("org", "pipe", 1741)
    assert calls[0][0].endswith("/organizations/org/pipelines/pipe/builds/1741")
    assert b["message"] == ["Scheduled build"]
    assert [f["job"] for f in b["failed"]] == ["id-hard-False", "id-perf-False", "id-big-log-False"]


def test_builds_lists_failures_per_build(fake_get):
    calls = fake_get([BUILD])
    out = bk.builds("org", "pipe", branch="compatible", limit=99)
    assert "branch=compatible" in calls[0][0] and "per_page=30" in calls[0][0]
    assert [f["step_key"] for f in out[0]["failed"]] == ["hard", "perf", "big-log"]


def test_clean_log_strips_markers_and_colour():
    raw = "\x1b_bk;t=1793063130877\x07\x1b[31mError: exit 1\x1b[0m\r\r\n\x1b_bk;t=1\x07done\r"
    assert bk.clean_log(raw) == "Error: exit 1\ndone\n"


def test_parse_content_range():
    assert bk.parse_content_range("bytes 33698-34197/34198", 500) == (33698, 34198)
    assert bk.parse_content_range(None, 500) == (0, 500)


def test_job_log_tail_drops_the_cut_first_line(fake_get):
    calls = fake_get(b"partial line\nFAIL: schema mismatch\nexit 1",
                     {"content-range": "bytes 100-140/141"})
    out = bk.job_log("org", "pipe", 1741, "job-1", tail_kib=2)
    assert calls[0][1] == {"Range": "bytes=-2048"} and calls[0][0].endswith("/builds/1741/jobs/job-1/log.txt")
    assert out["truncated"] and out["size_bytes"] == 141
    assert out["text"] == "FAIL: schema mismatch\nexit 1"


def test_job_log_whole_log_and_grep(fake_get):
    fake_get(b"a\nFAIL one\nb\nFAIL two\n")
    out = bk.job_log("org", "pipe", 1, "j", tail_kib=10**9, grep="^FAIL")
    assert not out["truncated"]
    assert out["matches"] == ["2: FAIL one", "4: FAIL two"] and out["match_count"] == 2


def test_missing_token_is_reported(monkeypatch):
    monkeypatch.delenv(bk.TOKEN_VAR, raising=False)
    with pytest.raises(RuntimeError, match=bk.TOKEN_VAR):
        bk._token()


def test_triage_nightly_phase_is_read_only():
    p = next(p for p in phases.all_phases() if p.name == "triage_nightly")
    assert p.args == ("branch",) and p.env == ("BUILDKITE_API_TOKEN",) and not p.interactive
    assert {"Edit", "Write", "Bash(gh *)", "Bash(git push *)"} <= set(p.disallowed_tools)
    assert not [t for t in p.allowed_tools
                if t.startswith("mcp__mina-harness__") and not t.startswith("mcp__mina-harness__ci_")]
    assert "branch `develop`" not in phases.render(p, {"branch": "develop"})
    assert "nightly build of `develop`" in phases.render(p, {"branch": "develop"})
