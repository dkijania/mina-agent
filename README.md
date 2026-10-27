# mina-agent

A structural harness for working on the Mina monorepo with Claude Code. It
makes correct behaviour a property of the tools rather than of prose: builds,
type-checks, and tests go through one MCP server that runs dune in the right
switch; the dependency graph is derived from the dune files, never written by
hand; build configuration and the Rust proof-systems boundary cannot be
edited; the raw toolchain (dune, opam, nix, cargo, make, pip, ocaml*) and the
opam-switch scripts cannot be run. Headless phases
and interactive sessions both start from that same set of walls and facts,
and only when invoked through `mina-agent`: nothing is written into the
repo's Claude settings, so plain `claude` sessions are unaffected.

    uv tool install ./harness            # --editable while developing the harness itself
    mina-agent admin setup && mina-agent admin init && mina-agent doctor
    mina-agent --help

Either toolchain reaches dune: the repo's opam switch, or the flake devShell
entered first (`nix develop .#with-lsp`, or direnv). `mina-agent status` says
which one it found; NIX.md covers the nix side.

The tool lives in uv's tool directory, not in the checkout, so it survives
checking out branches that predate `harness/`. Reinstall after changing the
harness source.

Every command answers `--help`. Listings of tools, tests, and phases are
derived, not documented: `mina-agent show tools|tests|phases`.

The one thing `--help` cannot tell you: the edit deny rules cover Claude's
file tools and the shell commands Claude Code recognises, not a script the
model writes that opens a file itself. Closing that needs the sandbox, which
this harness does not configure.

Known limitation: the harness serializes the dune calls it makes, but not
a `dune build -w` you run yourself; on the pinned dune (3.3.1) two dune
processes on one `_build` can corrupt each other's temporaries, and the
native fix (`dune rpc build` into the watcher) is experimental there. Stop
the watcher while the agent builds, until the dune pin moves.

Adding a workflow: see `PHASES.md`. Development: `uv run pytest` runs the pure tests under `tests/` (no
toolchain needed); `uv run pyright` type-checks the package. Shared record
types live in `mina_agent/model.py`; modules pass those, and `to_json()`
serializes at the MCP and log edges.

Secrets: fix-bug reads GitHub issues with `gh`, authenticated however this
machine already is (`gh auth login`); where that is not possible, put
`GH_TOKEN` in `harness/.envrc` (gitignored, see `.envrc.example`) and every
session sources it. A phase declares tools it needs with `needs:` and
variables with `env:` in its front matter and refuses to start without
them; `optional:` names one it can run without but would measure less with,
and asks first rather than letting the gap reach the report as a result.
Commits made in any harness session are the user's own: no Co-Authored-By
or Generated-with lines.

Reporting a harness bug: in any interactive session, say what went wrong
and the harness-bug-report skill drafts an issue for o1-labs/mina-agent,
bundles the evidence (environment, doctor, recent run logs, lint log,
profiling session) into a zip under the temp dir, and files it with `gh`
once you agree on the text. Without `gh` it saves the draft and tells you
where both files are. GitHub cannot take the zip through the API; drag it
onto the issue in the browser.

Verifying a PR's performance claims: `mina-agent run verify-perf --pr <number>`
opens a TUI session (this phase is interactive by default; `--headless` runs
it unattended) that reads the PR, locates the workload it says it measured (and stops if it
does not say), then measures base and head with no instrumentation:
/usr/bin/time for wall clock and peak RSS, the runtime's GC counters for
bytes allocated, and samply for the share of samples under a named
function. It reports whether the claimed numbers roughly recover.

Triaging a nightly: `mina-agent run triage-nightly --branch <develop|compatible>`
reads the newest finished build of the mainline nightly pipeline, sorts its
failed jobs into hard and soft, new, persistent and intermittent against the
previous builds, and finds each cause in the job logs and in the commits
between the last green build and this one. It reads Buildkite through three
read-only MCP tools (`ci_builds`, `ci_build`, `ci_job_log`), which need
`BUILDKITE_API_TOKEN` (read scope only); git is limited to log, show, diff and
fetch. Nothing is retried, edited or posted: the report is the output.

Writing code: `mina-agent develop` is discuss's opposite. Edits are accepted
without asking, tests may be run, and the shell is an allowlist enforced by
a hook (git, gh, `mina-agent lint/list/status/doctor`, read-only utilities;
the list is `[develop]` in `manifest.toml`, grow it as needed). The raw
toolchain and build configuration stay walled; building and testing go
through the harness tools.

Benchmarking: in a develop (or discuss) session the `benchmarking` skill
picks the instrument for the question: `perf_measure` on the working tree
or `perf_compare` between commits for samply sample shares, exact
allocation, peak RSS and wall clock, or a landmarks profiling session for
per-function attribution. Results are recorded under `state/perf/`.

`--continue` on discuss and develop resumes that mode's last session (the
harness records its own sessions from the SessionStart hook, so a plain
`claude` conversation in the same directory is never picked by mistake).
