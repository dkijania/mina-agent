"""Read-only Buildkite REST client behind the ci_* MCP tools.

Every request is a GET with the token from BUILDKITE_API_TOKEN (shell or
harness/.envrc); nothing here can create, retry or cancel a build, so a
read-only token is enough and a write-scoped one gains nothing. The org and
default pipeline come from manifest.toml [buildkite].

A job is reported once, as its latest attempt: Buildkite keeps the original
of a retried job with retried=true, and counting both would show a flake
that passed on retry as a failure.

Logs are fetched from the end with an HTTP Range request. A job log can be
very large (a cancelled integration test has produced 1 GiB), and the cause
of a failure is almost always near the end.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.buildkite.com/v2"
TOKEN_VAR = "BUILDKITE_API_TOKEN"
TIMEOUT_S = 60

# Job states that mean the step did not succeed. waiting_failed and broken
# are not listed: they mark steps that never ran because something upstream
# failed, and the upstream job is the one worth reading.
FAILED_STATES = ("failed", "timed_out", "canceled", "expired")
MAX_LOG_KIB = 4096
MAX_MATCHES = 200

# Buildkite timestamp markers (ESC _bk;t=<ms> BEL), ANSI CSI sequences.
_BK_MARK = re.compile(r"\x1b_bk;t=\d+\x07")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_CR_EOL = re.compile(r"\r+\n")                # agents write \r\r\n line ends


def _token() -> str:
    tok = os.environ.get(TOKEN_VAR)
    if not tok:
        raise RuntimeError(f"{TOKEN_VAR} is not set; export it or put it in harness/.envrc "
                           "(a read-only Buildkite API token is enough)")
    return tok


def _get(url: str, headers: dict | None = None) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, method="GET",
                                 headers={"Authorization": f"Bearer {_token()}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Buildkite GET {url} -> HTTP {ex.code}: {detail}") from ex
    except urllib.error.URLError as ex:
        raise RuntimeError(f"Buildkite GET {url} failed: {ex.reason}") from ex


def _pipeline_url(org: str, pipeline: str) -> str:
    return f"{API}/organizations/{org}/pipelines/{pipeline}"


def latest_attempts(jobs: list) -> list:
    """Script jobs, latest attempt only."""
    return [j for j in jobs if j.get("type") == "script" and not j.get("retried")]


def job_summary(j: dict) -> dict:
    return {"job": j.get("id"), "step_key": j.get("step_key"), "name": j.get("name"),
            "state": j.get("state"), "exit_status": j.get("exit_status"),
            "soft_failed": bool(j.get("soft_failed")), "retries": j.get("retries_count") or 0,
            "url": j.get("web_url")}


def failed_jobs(build: dict) -> list:
    return [job_summary(j) for j in latest_attempts(build.get("jobs") or [])
            if j.get("state") in FAILED_STATES]


def state_counts(build: dict) -> dict:
    out: dict[str, int] = {}
    for j in latest_attempts(build.get("jobs") or []):
        out[j.get("state") or "unknown"] = out.get(j.get("state") or "unknown", 0) + 1
    return out


def builds(org: str, pipeline: str, branch: str = "", limit: int = 7) -> list:
    """Most recent builds first, each with its failed jobs (latest attempt)."""
    q = {"per_page": str(max(1, min(limit, 30)))}
    if branch:
        q["branch"] = branch
    body, _ = _get(f"{_pipeline_url(org, pipeline)}/builds?{urllib.parse.urlencode(q)}")
    out = []
    for b in json.loads(body):
        out.append({"number": b.get("number"), "state": b.get("state"), "branch": b.get("branch"),
                    "commit": b.get("commit"), "source": b.get("source"),
                    "created_at": b.get("created_at"), "url": b.get("web_url"),
                    "failed": [{"step_key": f["step_key"], "name": f["name"], "state": f["state"],
                                "soft_failed": f["soft_failed"]} for f in failed_jobs(b)]})
    return out


def build(org: str, pipeline: str, number: int) -> dict:
    body, _ = _get(f"{_pipeline_url(org, pipeline)}/builds/{int(number)}")
    b = json.loads(body)
    return {"number": b.get("number"), "state": b.get("state"), "branch": b.get("branch"),
            "commit": b.get("commit"), "message": (b.get("message") or "").splitlines()[:1],
            "source": b.get("source"), "created_at": b.get("created_at"),
            "finished_at": b.get("finished_at"), "url": b.get("web_url"),
            "job_states": state_counts(b), "failed": failed_jobs(b)}


def clean_log(text: str) -> str:
    text = _BK_MARK.sub("", text)
    text = _ANSI.sub("", text)
    return _CR_EOL.sub("\n", text).replace("\r", "\n")


def parse_content_range(value: str | None, received: int) -> tuple[int, int]:
    """(first byte offset, total size) of a ranged response. Without a
    Content-Range header the server sent the whole log."""
    m = _RANGE.match(value or "")
    if not m:
        return 0, received
    return int(m.group(1)), int(m.group(3))


def job_log(org: str, pipeline: str, number: int, job: str, tail_kib: int = 64, grep: str = "") -> dict:
    tail_kib = max(1, min(tail_kib, MAX_LOG_KIB))
    pattern = re.compile(grep) if grep else None
    url = f"{_pipeline_url(org, pipeline)}/builds/{int(number)}/jobs/{urllib.parse.quote(job)}/log.txt"
    body, headers = _get(url, {"Range": f"bytes=-{tail_kib * 1024}"})
    start, size = parse_content_range(headers.get("content-range"), len(body))
    lines = clean_log(body.decode("utf-8", errors="replace")).split("\n")
    if start > 0:
        lines = lines[1:]                      # first line is cut mid-way
    out = {"size_bytes": size, "from_byte": start, "truncated": start > 0}
    if pattern is None:
        out["text"] = "\n".join(lines)
        return out
    hits = [f"{i}: {ln}" for i, ln in enumerate(lines, 1) if pattern.search(ln)]
    out["matches"] = hits[:MAX_MATCHES]
    out["match_count"] = len(hits)
    return out
