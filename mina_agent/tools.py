#!/usr/bin/env python3
"""Tool implementations for the mina-harness MCP server and hooks.

server.py wraps each public function listed in TOOLS as an MCP tool; the
hook commands and discuss call them directly. See server.py for the rules
(all shell-outs via env.py, one dune lock, graph re-derived on change).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib

from . import env as envmod
from . import graph as derivemod
from . import paths
from .diagnostics import RAW_TAIL_BYTES, parse_dune_errors, parse_test_output, split_diags, tail  # noqa: F401
from .model import Diagnostic, DuneRun, ProfileEntry, Session, Severity, to_json

MANIFEST = str(paths.MANIFEST)
DUNE_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# environment + graph
# --------------------------------------------------------------------------

class Graph:
    """derived.json, loaded lazily and kept current against the dune metadata
    mtimes. Nothing is derived at import: the first get() loads the on-disk
    cache when its stamp matches and derives only when it does not."""

    STALE_CHECK_INTERVAL = 2.0   # seconds; bound the whole-tree walk under call storms

    def __init__(self, env):
        self.env = env
        self.data = None
        self.stamp = None
        self.error = None
        self._checked_at = 0.0

    def refresh(self, force=False):
        s = derivemod.stamp(self.env.repo)
        if not force and s == self.stamp and self.data is not None:
            return False
        try:
            self.data = (derivemod.derive_and_write if force else derivemod.load_or_derive)(self.env)
            self.error = None
        except (Exception, SystemExit) as ex:  # describe-dune failures raise SystemExit
            self.error = f"derive failed: {ex}"
        self.stamp = s
        return True

    def get(self):
        # Re-stamp (a full source-tree walk) at most once per interval, so a
        # burst of graph queries in one command does not re-walk the tree per
        # call. A mid-session edit is still picked up within the interval.
        now = time.time()
        if now - self._checked_at >= self.STALE_CHECK_INTERVAL:
            self._checked_at = now
            self.refresh()
        if self.error or self.data is None:
            raise RuntimeError(self.error or "graph not derived")
        return self.data


ENV = envmod.detect()
GRAPH = Graph(ENV)
with open(MANIFEST, "rb") as _fh:
    MANIFEST_DATA = tomllib.load(_fh)


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def rel(path):
    """Normalize to a repo-relative path; refuse anything outside the repo."""
    p = path if os.path.isabs(path) else os.path.join(ENV.repo, path)
    p = os.path.normpath(p)
    root = ENV.repo.rstrip(os.sep) + os.sep
    if not (p + os.sep).startswith(root):
        raise ValueError(f"{path} is outside the repo")
    return os.path.relpath(p, ENV.repo)


def enclosing_dune_dir(relpath):
    """Nearest ancestor directory (inclusive) containing a dune file."""
    d = relpath if os.path.isdir(os.path.join(ENV.repo, relpath)) else os.path.dirname(relpath)
    while True:
        if os.path.exists(os.path.join(ENV.repo, d, "dune")):
            return d or "."
        if d in ("", "."):
            return None
        d = os.path.dirname(d)


def unit_of(relpath):
    """(kind, key, record) of the unit whose dir encloses relpath, or None."""
    g = GRAPH.get()
    d = relpath if os.path.isdir(os.path.join(ENV.repo, relpath)) else os.path.dirname(relpath)
    while True:
        units = g["by_dir"].get(d)
        if units:
            # a dir with several (tests (names ...)): pick the one named after the file
            base = os.path.splitext(os.path.basename(relpath))[0]
            for u in units:
                if u["kind"] in ("test", "exe") and u["name"] == base:
                    table = {"test": "tests", "exe": "executables"}[u["kind"]]
                    return u["kind"], u["key"], g[table][u["key"]]
            # otherwise prefer a library; a dir with lib + tests is described by its lib
            for kind in ("lib", "test", "exe"):
                for u in units:
                    if u["kind"] == kind:
                        table = {"lib": "libraries", "test": "tests", "exe": "executables"}[kind]
                        return kind, u["key"], g[table][u["key"]]
        if d in ("", "."):
            return None
        d = os.path.dirname(d)


# --------------------------------------------------------------------------
# dune output parsing
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# running dune
# --------------------------------------------------------------------------

def run_dune(argv, timeout_s, overrides=None) -> DuneRun:
    """Run argv through the env adapter under the dune lock. `overrides` are
    environment variables in force for this run only (the build profile);
    they are applied inside the lock, so two tool calls asking for different
    profiles cannot see each other's."""
    if not ENV.usable:
        raise RuntimeError("no usable toolchain: " + "; ".join(ENV.reasons))
    from . import profile as P
    argv = P.dune_argv(ENV.repo, argv)   # --instrument-with landmarks while a profiling session is active
    t0 = time.time()
    with DUNE_LOCK, ENV.overridden(overrides or {}):
        try:
            r = ENV.run(argv, capture=True, timeout=timeout_s, lock=True)
            code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
            timed_out = False
        except subprocess.TimeoutExpired as ex:
            code, timed_out = -1, True
            out = ex.stdout or b""
            if isinstance(out, bytes):  # bytes even with text=True
                out = out.decode(errors="replace")
    return DuneRun(code, out, round(time.time() - t0, 1), timed_out)


def _dune_result(r: DuneRun, **fields) -> dict:
    """The common shape of a dune-backed tool result, diagnostics serialized."""
    errs, warns = split_diags(r.out)
    return {"ok": r.ok, **fields, "elapsed_s": r.elapsed_s, "timed_out": r.timed_out,
            "errors": to_json(errs), "warnings": to_json(warns), "raw_tail": tail(r.out)}


# --------------------------------------------------------------------------
# manifest + tests
# --------------------------------------------------------------------------

def manifest_tests():
    return {t["name"]: t for t in MANIFEST_DATA["tests"]}


def inline_test_entry(lib):
    g = GRAPH.get()
    rec = g["libraries"].get(lib)
    if not rec or not rec["has_inline_tests"]:
        return None
    tpl = MANIFEST_DATA["inline_tests"]
    return {"name": tpl["name_prefix"] + lib,
            "command": [a.replace("{dir}", rec["dir"]) for a in tpl["command_template"]],
            "cost": tpl["cost"], "modes": tpl["modes"], "libraries": [lib]}


def resolve_test(name):
    t = manifest_tests().get(name)
    if t:
        return t
    prefix = MANIFEST_DATA["inline_tests"]["name_prefix"]
    if name.startswith(prefix):
        t = inline_test_entry(name[len(prefix):])
        if t:
            return t
        raise ValueError(f"{name[len(prefix):]} is not a library with (inline_tests)")
    raise ValueError(f"unknown test {name!r}; see manifest.toml [[tests]] or use inline:<library>")


# --------------------------------------------------------------------------



def env_status() -> dict:
    """Detected toolchain mode (opam/nix/none), dune and OCaml versions, warnings."""
    d = ENV.to_dict()
    try:
        GRAPH.get()
        d["derived_graph"] = {"ok": True, "error": None}
    except RuntimeError as ex:
        d["derived_graph"] = {"ok": False, "error": str(ex)}
    return d


def build_profiles() -> dict:
    """Mina's build profiles and the variables that select one, from
    manifest.toml [profiles]."""
    return MANIFEST_DATA["profiles"]


def _profile_overrides(profile: str) -> dict:
    return envmod.profile_env(profile, MANIFEST_DATA["profiles"])


def build(target: str, timeout_s: int = 600, profile: str = "") -> dict:
    """Run `dune build <target>`. target is a repo-relative path or alias like
    src/lib/hex or @src/lib/hex/check or src/app/cli/src/mina.exe.
    profile is a Mina build profile (dev, devnet, lightnet, mainnet); it sets
    DUNE_PROFILE and MINA_PROFILE for this run and makes dune rebuild what the
    profile reaches. Empty inherits the shell's, which is dev.
    Returns structured OCaml errors parsed from dune output."""
    t = target if target.startswith("@") else rel(target)
    over = _profile_overrides(profile)
    return _dune_result(run_dune(["dune", "build", t], timeout_s, over), target=t, profile=profile or None)


def check(path: str, timeout_s: int = 600) -> dict:
    """Cheapest type-check of the library containing path: `dune build @<dir>/check`
    (compiles interfaces/objects without linking). Returns structured errors."""
    p = rel(path)
    d = enclosing_dune_dir(p)
    if d is None:
        raise ValueError(f"{p} is not inside any dune directory")
    alias = f"@{d}/check"
    u = unit_of(p)
    return _dune_result(run_dune(["dune", "build", alias], timeout_s), path=p, alias=alias,
                        library=u[1] if u and u[0] == "lib" else None)


def test(name: str, timeout_s: int = 900, profile: str = "") -> dict:
    """Run a named test from manifest.toml, or inline:<library> for a library's
    ppx_inline_test blocks. Refuses tests whose modes exclude the current mode.
    profile is a Mina build profile (dev, devnet, lightnet, mainnet), which the
    profile_dependent test needs: it sets DUNE_PROFILE and MINA_PROFILE for this
    run. Empty inherits the shell's, which is dev."""
    t = resolve_test(name)
    if ENV.mode not in t["modes"]:
        return {"ok": False, "name": name, "refused": True,
                "reason": f"test {name} runs in modes {t['modes']}, current mode is {ENV.mode}",
                "command": t["command"]}
    over = _profile_overrides(profile)
    argv = list(t["command"])
    # dune caches passing runtest aliases; --force makes the test actually run
    # (and print its summary) even when nothing changed.
    if argv[:2] == ["dune", "build"] and "--force" not in argv:
        argv.insert(2, "--force")
    return _test_result(run_dune(argv, timeout_s, over), name=name, command=argv, cost=t["cost"],
                        profile=profile or None)


def test_one(file: str, test_name: str = "", timeout_s: int = 900, profile: str = "") -> dict:
    """Run one ppx_inline_test block via scripts/testone.sh <file> [test_name].
    test_name matches the string after `let%test "..."`; empty runs all blocks in the file.
    profile is a Mina build profile (dev, devnet, lightnet, mainnet); the script
    reads DUNE_PROFILE and passes it to dune. Empty inherits the shell's."""
    f = rel(file)
    over = _profile_overrides(profile)
    argv = ["bash", "scripts/testone.sh", f] + ([test_name] if test_name else [])
    return _test_result(run_dune(argv, timeout_s, over), file=f, test_name=test_name or None,
                        command=argv, profile=profile or None)


def _test_result(r: DuneRun, **fields) -> dict:
    errs, warns = split_diags(r.out)
    failures, summary = parse_test_output(r.out)
    return {"ok": r.ok, **fields, "elapsed_s": r.elapsed_s, "timed_out": r.timed_out,
            "summary_line": summary, "failures": to_json(failures),
            "build_errors": to_json(errs), "build_warnings": to_json(warns), "raw_tail": tail(r.out)}


def tests_for(path: str) -> dict:
    """Candidate tests for a source path, cheapest and most relevant first:
    the file's own library (inline tests, manifest tests), then tests of
    libraries that directly depend on it. Each has name/command/cost/reason."""
    g = GRAPH.get()
    p = rel(path)
    u = unit_of(p)
    if u is None:
        raise ValueError(f"{p} is not inside any described dune unit")
    kind, key, rec = u
    out, seen = [], set()

    def add(entry, reason):
        if entry and entry["name"] not in seen:
            seen.add(entry["name"])
            out.append({**entry, "reason": reason,
                        "runnable_in_current_mode": ENV.mode in entry["modes"]})

    if kind != "lib":
        add({"name": f"{kind}:{key}", "command": ["dune", "build", f"@{rec['dir']}/runtest"],
             "cost": "unmeasured", "modes": ["opam", "nix"], "libraries": []},
            f"path is inside {kind} unit {key}")
        return {"path": p, "unit": {"kind": kind, "key": key}, "candidates": out}

    lib = key
    mt = manifest_tests()
    core = MANIFEST_DATA["core"].get(lib)
    if core:
        add(mt.get(core["cheap_test"]), f"core library cheap_test for {lib}")
    add(inline_test_entry(lib), "inline tests of the same library")
    for t in sorted((t for t in mt.values() if lib in t["libraries"]),
                    key=lambda t: {"fast": 0, "slow": 1}.get(t["cost"], 2)):
        add(t, f"manifest test covering {lib}")
    for dep in g["dependents"].get(lib, []):
        if dep.startswith("test:"):
            tkey = dep[5:]
            trec = g["tests"][tkey]
            add({"name": dep, "command": ["dune", "build", f"@{trec['dir']}/runtest"],
                 "cost": "unmeasured", "modes": ["opam", "nix"], "libraries": [lib]},
                f"test unit depending directly on {lib}")
        elif not dep.startswith("exe:"):
            add(inline_test_entry(dep), f"inline tests of dependent library {dep}")
            for t in mt.values():
                if dep in t["libraries"]:
                    add(t, f"manifest test covering dependent {dep}")
        if len(out) >= 20:
            break
    return {"path": p, "unit": {"kind": "lib", "key": lib, "dir": rec["dir"]},
            "candidates": out[:20]}


def deps_of(library: str) -> dict:
    """Direct dependencies of a library (private dune name or public name)."""
    g = GRAPH.get()
    name = g["public_names"].get(library, library)
    rec = g["libraries"].get(name)
    if not rec:
        raise ValueError(f"unknown library {library!r}")
    return {"library": name, "public_name": rec["public_name"], "dir": rec["dir"],
            "deps": rec["deps"], "external_deps": rec["external_deps"], "ppx": rec["ppx"],
            "has_inline_tests": rec["has_inline_tests"]}


def dependents_of(library: str) -> dict:
    """Libraries, executables, and test units that directly depend on a library."""
    g = GRAPH.get()
    name = g["public_names"].get(library, library)
    if name not in g["libraries"]:
        raise ValueError(f"unknown library {library!r}")
    deps = g["dependents"].get(name, [])
    return {"library": name,
            "libraries": [d for d in deps if ":" not in d],
            "executables": [d[4:] for d in deps if d.startswith("exe:")],
            "tests": [d[5:] for d in deps if d.startswith("test:")]}


def library_of(path: str) -> dict:
    """Which dune unit (library / executable / test) a source path belongs to."""
    p = rel(path)
    u = unit_of(p)
    if u is None:
        d = enclosing_dune_dir(p)
        raise ValueError(f"{p}: no described unit; nearest dune dir is {d}")
    kind, key, rec = u
    return {"path": p, "kind": kind, "key": key, "dir": rec["dir"],
            "name": rec.get("name", key), "public_name": rec.get("public_name")}




# --------------------------------------------------------------------------
# module index: module name -> file + owning library
# --------------------------------------------------------------------------

def _modname(stem):
    return stem[:1].upper() + stem[1:]


def _lib_files(rec, by_dir):
    """{Module: {ml, mli}} for one library. Dune names a module after its file
    stem; (modules ...) narrows the set and (include_subdirs ...) widens it to
    subdirectories that are not dune units of their own."""
    d = rec["dir"]
    full = os.path.join(ENV.repo, d)
    try:
        with open(os.path.join(full, "dune")) as fh:
            recurse = "include_subdirs" in fh.read()
    except OSError:
        recurse = False
    found = {}
    if recurse:
        for root, dirs, files in os.walk(full):
            rel_root = os.path.relpath(root, ENV.repo)
            dirs[:] = [x for x in dirs if os.path.join(rel_root, x) not in by_dir
                       and x not in derivemod.SKIP_DIRS]
            for f in files:
                found.setdefault(f, os.path.join(rel_root, f))
    else:
        try:
            found = {f: os.path.join(d, f) for f in os.listdir(full)}
        except OSError:
            return {}
    mods = rec.get("modules")
    out = {}
    for f, p in found.items():
        stem, ext = os.path.splitext(f)
        if ext not in (".ml", ".mli") or not stem[:1].isalpha() or "." in stem:
            continue
        if mods and (stem in mods.get("exclude", [])
                     or not (mods.get("with_standard") or stem in mods.get("include", []))):
            continue
        out.setdefault(_modname(stem), {"ml": None, "mli": None})[ext[1:]] = p
    return out


def _module_index(g):
    return {key: {"top": _modname(key), "files": _lib_files(rec, g["by_dir"])}
            for key, rec in g["libraries"].items()}


def find_module(name: str) -> dict:
    """Where a module lives: its canonical .ml/.mli and the dune library that
    owns it, for a module name with no position attached (from conversation,
    a build error, or a dune file). Accepts a bare module (Zkapp_account), a
    dotted path (Mina_base.Zkapp_account.Stable), or a dune public name
    (archive.cli). Library hits come first — a library's top module is its
    wrapper, i.e. the file named after it when one exists — then same-named
    submodule files in other libraries, each with its library, so an ambiguous
    name (26 intf.ml) is resolvable. Components beyond the file come back as
    `remaining`; resolve those inside the file with the LSP (documentSymbol).
    The library returned is the key for deps_of / dependents_of / tests_for /
    check_dependents."""
    g = GRAPH.get()
    q = name.strip()
    if not q:
        raise ValueError("empty module name")
    if q in g["public_names"]:  # a dune public name names one library directly
        key = g["public_names"][q]
        idx = {key: {"top": _modname(key), "files": _lib_files(g["libraries"][key], g["by_dir"])}}
        comps = [idx[key]["top"]]
    else:
        idx = _module_index(g)
        comps = q.split(".")
    head, rest = comps[0], comps[1:]
    hits = []
    for key, ent in idx.items():
        rec = g["libraries"][key]
        base = {"library": key, "public_name": rec["public_name"], "dir": rec["dir"]}
        if ent["top"] == head:
            main = ent["files"].get(head)
            h = {"role": "library", "module": head, **base,
                 "ml": main["ml"] if main else None, "mli": main["mli"] if main else None}
            if not main:  # dune generates the wrapper; the files are its submodules
                subs = sorted(ent["files"])
                h.update(auto_wrapper=True, submodules=subs[:40], submodule_count=len(subs))
            hits.append(h)
        elif head in ent["files"]:
            f = ent["files"][head]
            hits.append({"role": "submodule", "module": f"{ent['top']}.{head}", **base,
                         "ml": f["ml"], "mli": f["mli"]})
    hits.sort(key=lambda h: (h["role"] != "library", h["dir"]))
    remaining = rest
    if rest and hits and hits[0]["role"] == "library":  # Top.Sub -> the file sub.ml
        lib, sub = hits[0], rest[0]
        f = idx[lib["library"]]["files"].get(sub)
        if f:
            hits = [{"role": "submodule", "module": f"{head}.{sub}", "library": lib["library"],
                     "public_name": lib["public_name"], "dir": lib["dir"], "ml": f["ml"], "mli": f["mli"]}]
            remaining = rest[1:]
        else:
            lib["note"] = (f"no file {sub.lower()}.ml in {lib['dir']}; {sub} is declared inside "
                           f"{lib['ml'] or 'the library'}: use documentSymbol or workspaceSymbol")
            hits = [lib]
    out = {"query": q, "hit_count": len(hits), "hits": hits[:30], "remaining": remaining}
    if not hits:
        out["note"] = ("no library or file is named after this module; a value, type, or module "
                       "declared inside a file is found by the LSP workspaceSymbol")
    elif remaining:
        out["note"] = f"{'.'.join(remaining)} is inside the returned file; use documentSymbol on it"
    return out


def check_dependents(library: str, timeout_s: int = 900) -> dict:
    """Type-check every library that directly depends on `library`, in one
    dune call (`dune build @a/check @b/check ...`). Use after changing an
    interface, to catch breakage in consumers without linking anything."""
    g = GRAPH.get()
    name = g["public_names"].get(library, library)
    if name not in g["libraries"]:
        raise ValueError(f"unknown library {library!r}")
    deps = [d for d in g["dependents"].get(name, []) if ":" not in d]
    aliases = [f"@{g['libraries'][d]['dir']}/check" for d in deps]
    if not aliases:
        return {"ok": True, "library": name, "checked": [], "elapsed_s": 0,
                "errors": [], "raw_tail": ""}
    return _dune_result(run_dune(["dune", "build"] + aliases, timeout_s), library=name, checked=deps)


# --------------------------------------------------------------------------
# merlin (read-side queries against the last compiled state)
# --------------------------------------------------------------------------

def _merlin_run(extra, path, timeout_s=60):
    """Run `ocamlmerlin single` with extra args on the file's current contents,
    fed on stdin so unsaved edits are honoured. Returns (relpath, value, clock_ms)."""
    p = rel(path)
    full = os.path.join(ENV.repo, p)
    if not os.path.isfile(full):
        raise ValueError(f"{p} is not a file")
    with open(full, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    argv = ["ocamlmerlin", "single"] + extra + ["-filename", p]
    with DUNE_LOCK:  # merlin asks `dune ocaml-merlin` for config
        r = ENV.run(argv, capture=True, timeout=timeout_s, input=src)
    try:
        j = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"merlin produced no JSON: {tail(r.stdout + r.stderr)}")
    if j.get("class") != "return":
        raise RuntimeError(f"merlin {j.get('class')}: {j.get('value')}")
    return p, j["value"], (j.get("timing") or {}).get("clock")


def _merlin(command, path, line, col, timeout_s=60):
    """A positioned merlin query (line/col 1-based; merlin wants 0-based columns)."""
    return _merlin_run([command, "-position", f"{line}:{col - 1}"], path, timeout_s)


_NOT_BUILT_HINT = ("no result; if the library has not been compiled since clone or "
                   "since its dependencies changed, run check on this path first")


def type_at(file: str, line: int, col: int) -> dict:
    """Type of the expression at file:line:col (1-based, as in build/check errors),
    plus the enclosing expressions' types. Reflects the last compiled state of
    other modules, so run check after edits to interfaces before trusting it."""
    p, val, ms = _merlin("type-enclosing", file, line, col)
    enclosing = [{"start": [v["start"]["line"], v["start"]["col"] + 1],
                  "end": [v["end"]["line"], v["end"]["col"] + 1],
                  "type": v["type"]} for v in val
                 if "repeat to confirm" not in v["type"]][:3]
    return {"file": p, "line": line, "col": col, "elapsed_ms": ms,
            "type": enclosing[0]["type"] if enclosing else None,
            "enclosing": enclosing,
            "note": None if enclosing else _NOT_BUILT_HINT}


def definition(file: str, line: int, col: int) -> dict:
    """Where the identifier at file:line:col is defined (1-based). Returns the
    defining file and position, or a note if merlin cannot resolve it."""
    p, val, ms = _merlin("locate", file, line, col)
    if isinstance(val, str):
        return {"file": p, "line": line, "col": col, "elapsed_ms": ms,
                "definition": None, "note": val + "; " + _NOT_BUILT_HINT}
    dfile = val.get("file")
    if dfile and os.path.isabs(dfile):
        try:
            dfile = rel(dfile)
        except ValueError:
            pass  # outside the repo (opam library source), keep absolute
    return {"file": p, "line": line, "col": col, "elapsed_ms": ms,
            "definition": {"file": dfile, "line": val["pos"]["line"],
                           "col": val["pos"]["col"] + 1}}


def errors(file: str, timeout_s: int = 60) -> dict:
    """All syntax/type errors and warnings in file, from merlin against the last
    compiled state (~sub-second, no rebuild, reads the file as currently edited).
    This is the fast inner-loop check: it catches local mistakes (typos, wrong
    arguments, non-exhaustive matches) without paying dune's per-edit recompile;
    run check / check_dependents afterwards to confirm cross-module effects, which
    merlin cannot see until a rebuild. If the file's library has not been compiled,
    merlin reports phantom Unbound errors; this detects that and returns stale=true
    with a hint to run check first, rather than a flood of false diagnostics."""
    p, val, ms = _merlin_run(["errors"], file, timeout_s)
    diags: list[Diagnostic] = []
    for e in val:
        st = e.get("start") or {"line": 0, "col": -1}
        en = e.get("end") or st
        msg = " ".join((e.get("message") or "").split())
        warning = e.get("type", "typer") == "warning" or msg.startswith("Warning")
        diags.append(Diagnostic(file=p, line=st["line"], col_start=st["col"] + 1, col_end=en["col"] + 1,
                                severity=Severity.WARNING if warning else Severity.ERROR, message=msg))
    errs = [d for d in diags if d.severity is Severity.ERROR]
    warns = [d for d in diags if d.severity is Severity.WARNING]
    # A single Unbound is a real mistake (a name that doesn't exist yet); a pile of
    # them means the library's .cmi files aren't built, so every reference dangles.
    unbound = [d for d in errs if d.message.startswith("Unbound")]
    stale = len(unbound) >= 3 and len(unbound) >= 0.5 * len(errs)
    out = {"file": p, "elapsed_ms": ms, "ok": not errs, "stale": stale,
           "error_count": len(errs), "warning_count": len(warns),
           "errors": to_json(errs[:50]), "warnings": to_json(warns[:50])}
    if stale:
        out["note"] = ("most errors are Unbound, which usually means " + _NOT_BUILT_HINT
                       + "; then errors reflects only real problems")
    return out


def usages(file: str, line: int, col: int, timeout_s: int = 120, refresh: bool = True) -> dict:
    """Every use of the binding at file:line:col (1-based) across the repo:
    its library plus everything that depends on it, executables and tests
    included. Read from the typed trees the compiler wrote (.cmt in _build),
    so each result is the typechecker's own resolution: no text matching,
    ppx-generated references excluded. The position may be the definition or
    any use of it (merlin resolves it to the definition first). Values,
    constructors, record fields, and types; not modules (use dependents_of
    for who depends on a library). The typed trees are refreshed first
    (`dune build @<dir>/check` over the whole scope, incremental, so stale
    trees from an older checkout or a native-only build cannot hide or
    misplace references; refresh=false skips that); `unbuilt` lists units
    that still have no tree."""
    from . import usages as U
    g = GRAPH.get()
    refreshed = {}

    def attempt(def_file, dl, dc):
        u = unit_of(def_file)
        if u is None:
            raise ValueError(f"{def_file} is not inside any described dune unit")
        kind, key, _ = u
        units = U.cone(g, kind, key)
        if refresh:
            r = run_dune(["dune", "build", *U.check_aliases(g, units)], max(timeout_s, 600))
            refreshed.update({"refresh_s": r.elapsed_s, "refresh_ok": r.ok,
                              **({} if r.ok else {"refresh_errors": to_json(split_diags(r.out)[0][:10])})})
        cmts, unbuilt = U.cmt_files(ENV.repo, g, units)
        return U.run(ENV, def_file, dl, dc, cmts, timeout_s), key, units, unbuilt

    t0 = time.time()
    # The position is taken as a declaration first (a miss costs one .cmt
    # read). Only when nothing is declared there is it a use of the binding,
    # which merlin resolves to the definition. Asking merlin first is wrong at
    # a declaration: for `type t = M.t = A | B` it jumps to the manifest M.t.
    def_file, dl, dc = rel(file), line, col
    res, key, units, unbuilt = attempt(def_file, dl, dc)
    if res.get("error", "").startswith("no declaration"):
        _, val, _ = _merlin("locate", def_file, dl, dc)
        if isinstance(val, dict):
            f = val.get("file") or def_file
            try:
                def_file = rel(f) if os.path.isabs(f) else f
            except ValueError:
                return {"ok": False, "file": rel(file), "line": line, "col": col,
                        "note": f"defined outside the repo ({f}); bindings of external libraries are not searched"}
            dl, dc = val["pos"]["line"], val["pos"]["col"] + 1
            res, key, units, unbuilt = attempt(def_file, dl, dc)
    elapsed = round(time.time() - t0, 1)
    if "error" in res:
        return {"ok": False, "file": def_file, "line": dl, "col": dc, "elapsed_s": elapsed,
                "unbuilt": unbuilt, "note": res["error"], **refreshed}
    by_lib = {}
    for h in res["usages"]:
        hu = unit_of(h["file"])
        h["library"] = hu[1] if hu else None
        by_lib[h["library"]] = by_lib.get(h["library"], 0) + 1
    n = len(res["usages"])
    out = {"ok": True,
           "binding": {**res["target"], "file": def_file, "line": dl, "col": dc, "library": key},
           "usage_count": n, "usages": res["usages"][:500], "truncated": n > 500,
           "by_library": dict(sorted(by_lib.items(), key=lambda kv: (-kv[1], str(kv[0])))),
           "scope": {"units": len(units), "files_read": res["files_read"],
                     "unreadable": len(res["unreadable"])},
           "unbuilt": unbuilt, "unresolved_types": res["unresolved_types"], **refreshed,
           "unresolved_files": res.get("unresolved_files", []), "elapsed_s": elapsed}
    notes = []
    if unbuilt:
        notes.append(f"{len(unbuilt)} unit(s) in scope have never been compiled, so their "
                     "references are not visible; build them (check) to complete the answer")
    if res["unresolved_types"]:
        notes.append(f"{res['unresolved_types']} type reference(s) with a local module head could not "
                     "be resolved (functor parameters, signature-local types, or an environment that "
                     "failed to rebuild); a use through a local module alias could be among them, "
                     "see unresolved_files")
    if notes:
        out["note"] = "; ".join(notes)
    return out


# --------------------------------------------------------------------------
# profiling (active only inside `mina-agent profile`)
# --------------------------------------------------------------------------

def _session() -> Session:
    from . import profile as P
    s = P.load(ENV.repo)
    if s is None:
        raise RuntimeError("no active profiling session; start one with mina-agent profile --focus <library>")
    return s


def _profile_entry(s: Session, profile: str) -> ProfileEntry:
    if not s.profiles:
        raise ValueError("no profiles recorded yet; run profile_run first")
    if profile in ("", "latest"):
        return s.profiles[-1]
    for p in s.profiles:
        if p.profile == profile or p.profile.startswith(profile):
            return p
    raise ValueError(f"unknown profile {profile!r}; recorded: " + ", ".join(p.profile for p in s.profiles))


def profile_add_library(library: str) -> dict:
    """Extend the active profiling session's instrumentation to another
    library (a dune name, public name, or a source path inside it), so the
    next profile_run attributes time and allocation inside it too. Use it
    when the hot path crosses out of the instrumented set: windows placed
    in an uninstrumented library are inert. The library's dune file gets the
    same temporary stanza as the focus and is restored with it at session
    end; refuses if that file has uncommitted changes."""
    from . import profile as P
    g = GRAPH.get()
    lib = P.resolve_focus(g, library)
    before = _session()
    s = P.extend(ENV.repo, g, [lib])
    added = [l for l in s.libraries if l not in before.libraries]
    return {"library": lib, "added": added, "already_instrumented": not added,
            "instrumented_libraries": list(s.libraries), "injected_dune_files": sorted(s.injected),
            "skipped": [list(x) for x in s.skipped],
            "note": "rebuild happens on the next profile_run (dune recompiles the library instrumented); "
                    "profiles recorded before this call do not cover it"}


def profile_link_impl(impl: str, workload: str = "") -> dict:
    """Link a chosen implementation of a dune virtual library into a
    workload's link unit for the rest of the session, e.g.
    profile_link_impl("disk_cache.lmdb"): without it every test runner
    silently gets the virtual library's default implementation, so a
    measurement never exercises the production backend. impl is a library
    (dune or public name) carrying `implements`; workload defaults to
    inline:<focus>. Refuses if the unit already links another
    implementation of that virtual library. The dune edit is tracked and
    restored with the session; the next profile_run relinks."""
    from . import profile as P
    g = GRAPH.get()
    s = _session()
    key = g["public_names"].get(impl, impl)
    workload = workload or f"inline:{s.focus}"
    _, info = P.link_impl(ENV.repo, g, key, workload)
    return info


def profile_status() -> dict:
    """The active profiling session: focus, instrumented libraries, profiles
    recorded so far. Errors when no session is active."""
    s = _session()
    d = {k: v for k, v in to_json(s).items() if k not in ("injected", "injected_sha")}  # original bytes are not for the model
    return d | {"injected_dune_files": sorted(s.injected),
                "linked_implementations": [f"{l.impl} ({l.virtual}) into {l.workload}" for l in s.linked]}


def profile_run(workload: str, only_test: str = "", timeout_s: int = 900) -> dict:
    """Build the workload with the in-scope libraries instrumented and run it
    under the landmarks profiler. workload: inline:<library> (its inline
    tests; only_test narrows to file[:name]), test:<dir>/<name> (a test unit),
    exe:<path.exe>, or a manifest test name. Returns the profile id, the
    focus libraries' share of time, and the top functions by self time; use
    profile_top / profile_callers to dig, profile_diff to compare runs. The
    workload's complete output is written next to the profile (`log`); test
    failures are parsed into `failures`, so a failing run can be read in full."""
    from . import landmarks as L, profile as P
    s = _session()
    results = []
    for w in P.resolve_workload(GRAPH.get(), manifest_tests(), workload, dune_version=ENV.dune_version):
        built = run_dune(["dune", "build", w.target], timeout_s)
        if not built.ok:
            return {"ok": False, "stage": "build", "workload": workload, "target": w.target,
                    "errors": to_json(split_diags(built.out)[0]), "raw_tail": tail(built.out)}
        path = P.next_profile_path(ENV.repo, workload)
        argv = ["env", f"OCAML_LANDMARKS=format=json,output={path},allocation,time",
                os.path.join(ENV.repo, "_build", "default", w.exe), *w.args]
        if only_test and w.args:
            argv += ["-only-test", only_test]
        t0 = time.time()
        r = ENV.run(argv, capture=True, timeout=timeout_s, cwd=os.path.join(ENV.repo, "_build", "default", w.cwd))
        run_s = round(time.time() - t0, 1)
        text = (r.stdout or "") + (r.stderr or "")
        log = path.with_suffix(".log")
        log.write_text(text, encoding="utf-8", errors="replace")
        failures, summary = parse_test_output(text)
        if not path.exists():
            return {"ok": False, "stage": "run", "workload": workload, "exe": w.exe, "exit_code": r.returncode,
                    "elapsed_s": run_s, "note": "the run produced no profile (crashed before exit?)",
                    "log": str(log), "summary_line": summary, "failures": to_json(failures), "raw_tail": tail(text)}
        prof = L.load(path)
        focus = [f for f in prof.functions.values() if f.under(s.dirs) and f.calls > 0]
        share = round(sum(f.self_ms for f in focus) / prof.total_ms * 100, 1) if prof.total_ms else 0.0
        entry = ProfileEntry(profile=path.stem, path=str(path), workload=workload, only_test=only_test or None,
                             exe=w.exe, exit_code=r.returncode, run_s=run_s, build_s=built.elapsed_s,
                             total_ms=prof.total_ms, units=prof.units, functions=len(prof.functions),
                             focus_functions_hit=len(focus), focus_self_share_pct=share, log=str(log))
        P.record_profile(ENV.repo, entry)
        results.append(to_json(entry) | {"summary_line": summary, "failures": to_json(failures),
                                         "top_focus": L.top(prof, "self_ms", 10, s.dirs), "raw_tail": tail(text)})
    return {"ok": all(e["exit_code"] == 0 for e in results), "runs": results,
            "note": None if results and results[-1]["focus_functions_hit"] else
            "no focus-library function ran under this workload; pick one that exercises the focus"}


def profile_top(profile: str = "latest", by: str = "self_ms", k: int = 15, scope: str = "focus") -> dict:
    """Ranked functions of a recorded profile. by: self_ms, total_ms, calls,
    self_alloc_mb, alloc_mb. scope: focus (instrumented libraries only) or all.
    Self figures exclude callees; total includes them."""
    from . import landmarks as L
    s = _session()
    p = _profile_entry(s, profile)
    if by not in L.RANK_KEYS:
        raise ValueError("by must be one of " + ", ".join(L.RANK_KEYS))
    prof = L.load(p.path)
    return {"profile": p.profile, "workload": p.workload, "total_ms": prof.total_ms, "units": prof.units,
            "by": by, "rows": L.top(prof, by, k, s.dirs if scope == "focus" else None)}


def profile_callers(function: str, profile: str = "latest") -> dict:
    """Who calls a function and what it calls, with time under each edge, in
    a recorded profile. function matches a substring of "name @ file:line"."""
    from . import landmarks as L
    s = _session()
    p = _profile_entry(s, profile)
    prof = L.load(p.path)
    keys = [k for k in prof.functions if function in k]
    if not keys:
        raise ValueError(f"no function matching {function!r} in {p.profile}")
    if len(keys) > 1 and function not in keys:
        return {"profile": p.profile, "ambiguous": keys[:20]}
    key = function if function in keys else keys[0]
    f = prof.functions[key]
    # callees: every function that lists `key` among its callers, with that edge
    callees = sorted(({"callee": k, "calls": e.calls, "total_ms": e.total_ms}
                      for k, g in prof.functions.items() for e in g.callers if e.caller == key),
                     key=lambda c: -c["total_ms"])
    return {"profile": p.profile, "function": key, **{kk: v for kk, v in to_json(f).items() if kk != "callers"},
            "callers": to_json(f.callers), "callees": callees[:20]}


def profile_diff(before: str, after: str = "latest", k: int = 20) -> dict:
    """Per-function change between two profiles of the same workload: self
    time and self allocation deltas, largest first, plus the total delta."""
    from . import landmarks as L
    s = _session()
    a, b = _profile_entry(s, before), _profile_entry(s, after)
    return {"before": a.profile, "after": b.profile, "workload": a.workload,
            "same_workload": (a.workload, a.only_test) == (b.workload, b.only_test),
            **L.diff(L.load(a.path), L.load(b.path), k)}


def perf_measure(workload: str, symbol: str = "", repeats: int = 3, extra_args: str = "", env: str = "",
                 timeout_s: int = 1800) -> dict:
    """Measure one workload on the working tree as it is, uncommitted edits
    included, with no instrumentation: median wall clock and peak RSS
    (/usr/bin/time), bytes allocated (the runtime's GC counters), and, with
    `symbol` and samply installed, that function's share of the OCaml
    threads' CPU: inclusive (withheld with a warning when stacks are
    incomplete, the norm on macOS arm64) and leaf (self time, always valid),
    plus stack completeness. extra_args are appended to the
    workload's argv; env is 'NAME=value ...' set for every run (how a test
    is told to use its mainnet-sized configuration). Measure before an
    edit, edit, measure again; each result is recorded under state/perf/.
    Refuses while a profiling session is active."""
    import shlex
    from . import perf
    r, rec = perf.measure_current(ENV, GRAPH.get(), manifest_tests(), workload, symbol=symbol or None, repeats=repeats,
                                  run_dune=run_dune, timeout_s=timeout_s, extra_args=shlex.split(extra_args),
                                  extra_env=perf.parse_env(env))
    d = to_json(r)
    return d | {"wall_median_s": r.wall_median_s, "allocated_gb": round(r.gc.allocated_bytes / 1e9, 3) if r.gc else None,
                "max_rss_mb": round(r.max_rss_bytes / 1e6, 1) if r.max_rss_bytes else None, "record": rec,
                "tools": perf.tools_available()}


def perf_compare(workload: str, base: str, head: str, symbol: str = "", repeats: int = 3, extra_args: str = "",
                 env: str = "", timeout_s: int = 1800) -> dict:
    """Measure one workload at two commits with no instrumentation, and
    report head relative to base: median wall clock and peak RSS
    (/usr/bin/time), bytes allocated (the runtime's GC counters), and, when
    `symbol` is given and samply is installed, that function's inclusive
    and leaf shares of the OCaml threads' CPU with stack completeness (see
    perf_measure). workload is inline:<library>,
    test:<dir>/<name>, exe:<path.exe> or a manifest test name; base/head are
    any git refs (use the merge-base of the PR's base branch for base).
    Checks the commits out in place, builds, measures, and restores the
    original branch; refuses on a dirty tree or an active profiling session.
    extra_args and env as in perf_measure. Slow: two builds plus
    (repeats + 2) runs each."""
    import shlex
    from . import perf
    c = perf.compare(ENV, GRAPH.get(), manifest_tests(), workload, base, head, symbol=symbol or None,
                     repeats=repeats, run_dune=run_dune, timeout_s=timeout_s, extra_args=shlex.split(extra_args),
                     extra_env=perf.parse_env(env))
    return to_json(c) | {"deltas": perf.deltas(c)}


def bug_report_bundle(runs: int = 2) -> dict:
    """Evidence for a bug report about this harness: environment (harness and
    Mina commits, toolchain), `mina-agent doctor` output, the last `runs`
    headless run logs with their summaries, the lint gate log, and the active
    profiling session. Written to a directory under the system temp dir plus
    a zip of it; Read the files in `directory` to quote from them."""
    from . import bugreport as B
    return B.bundle_json(B.bundle(ENV, runs=runs))


def bug_report_file(title: str, body: str, bundle: str = "") -> dict:
    """File an issue on github.com/o1-labs/mina-agent with gh, after the user
    has agreed on the exact title and body. `bundle` is the zip path from
    bug_report_bundle; GitHub cannot take it as an attachment, so the body
    says where it is. When gh is missing or unauthenticated the draft is
    saved as markdown instead and `filed` is false: give the user the draft
    path, the bundle path, and new_issue_url."""
    from . import bugreport as B
    return B.file_issue(title, body, bundle or None)


# --------------------------------------------------------------------------
# CI (Buildkite, read-only)
# --------------------------------------------------------------------------

def _bk(pipeline: str) -> tuple[str, str]:
    cfg = MANIFEST_DATA["buildkite"]
    return cfg["org"], pipeline or cfg["pipeline"]


def ci_builds(branch: str = "", pipeline: str = "", limit: int = 7) -> dict:
    """Recent Buildkite builds, newest first, each with its failed jobs
    (latest attempt; soft_failed marks a step allowed to fail). One call over
    several builds shows whether a failure is new, persistent or
    intermittent. pipeline defaults to manifest [buildkite] pipeline (the
    scheduled nightlies of the mainline branches); branch filters, e.g.
    develop or compatible."""
    from . import buildkite as bk
    org, pipe = _bk(pipeline)
    return {"pipeline": pipe, "builds": bk.builds(org, pipe, branch, limit)}


def ci_build(number: int, pipeline: str = "") -> dict:
    """One build: commit, state, job-state counts, and every failed job
    (latest attempt) with its job id, step_key, exit status and soft_failed.
    Pass a job id to ci_job_log to read why it failed."""
    from . import buildkite as bk
    org, pipe = _bk(pipeline)
    return {"pipeline": pipe} | bk.build(org, pipe, number)


def ci_job_log(number: int, job: str, pipeline: str = "", tail_kib: int = 64, grep: str = "") -> dict:
    """The end of one job's log (last tail_kib KiB, at most 4096), with
    Buildkite timestamps and ANSI colour removed. size_bytes is the whole
    log; truncated says the start was not fetched. With grep (a Python
    regex), only matching lines of that window are returned, numbered."""
    from . import buildkite as bk
    org, pipe = _bk(pipeline)
    return {"pipeline": pipe} | bk.job_log(org, pipe, number, job, tail_kib, grep)


TOOLS = ["env_status", "build", "check", "check_dependents", "test", "test_one",
         "tests_for", "deps_of", "dependents_of", "library_of", "find_module", "usages",
         "type_at", "definition", "errors",
         "profile_status", "profile_run", "profile_top", "profile_callers", "profile_diff", "profile_add_library",
         "profile_link_impl",
         "bug_report_bundle", "bug_report_file", "perf_measure", "perf_compare",
         "ci_builds", "ci_build", "ci_job_log"]



def facts(develop: bool = False) -> list:
    """Plain factual statements about the environment and manifest, for the
    SessionStart hook and run.py's --append-system-prompt. Statements, never
    instructions (hooks docs: prompt-injection note). develop=True describes
    the development session's shell, which is an allowlist rather than the
    deny rules the other sessions run under."""
    env, m = ENV, MANIFEST_DATA
    out = [f"mina-harness environment: {env.summary()}."]
    out += [f"warning: {w}" for w in env.warnings]
    try:
        g = GRAPH.get()
        out.append(f"library graph derived from dune files: {len(g['libraries'])} libraries, "
                   f"{len(g['tests'])} test units, {len(g['executables'])} executables.")
    except RuntimeError as ex:
        out.append(f"library graph unavailable: {ex}")
    b = m["boundary"]
    mut = ("editable, mutable=true: the proof-systems submodule may be edited and cargo may run"
           if b.get("mutable") else "read-only, mutable=false")
    out.append(f"OCaml/Rust boundary ({mut}): libraries "
               + ", ".join(b["libraries"]) + f" in {b['stubs_dir']} wrap crates "
               + ", ".join(b["crates"]) + ". Protected paths: " + ", ".join(b["rust_paths"]) + ".")
    out.append("Core libraries: " + "; ".join(
        f"{k} ({v['dir']}, cheap test {v['cheap_test']})" for k, v in m["core"].items()) + ".")
    out.append("Manifest tests: " + "; ".join(
        f"{t['name']} [{t['cost']}, modes {','.join(t['modes']) or 'none'}]" for t in m["tests"])
        + f". Any library with (inline_tests) also has {m['inline_tests']['name_prefix']}<library>.")
    from . import lsp
    if lsp.plugin_dir(ENV.repo):
        out.append("ocamllsp is installed: Claude's built-in LSP tool (goToDefinition, findReferences, "
                   "hover, documentSymbol, workspaceSymbol) works on .ml/.mli files; type_at/definition "
                   "are the merlin fallback. goToDefinition resolves a reference at a position; "
                   "workspaceSymbol finds a declared value, type, or module by name (~2s, whole "
                   "workspace); find_module maps a module name with no position to its canonical "
                   "file and library. A library's top module is its file rather than a declaration, "
                   "so workspaceSymbol does not find it and returns same-named aliases or mocks "
                   "in other files instead.")
    out.append("MCP server mina-harness provides: " + ", ".join(TOOLS)
               + ". errors gives merlin's sub-second diagnostics for one file and is the fast "
               "inner-loop check after an edit; check decides whether an edit compiles and "
               "check_dependents whether its consumers still do (the slower, cross-module "
               "truth); find_module maps a module name to its file and owning library, the key "
               "for deps_of/tests_for; usages lists every use of a value, constructor, field, or "
               "type across the repo from the compiled typed trees (the LSP findReferences and "
               "merlin occurrences are file-local on this toolchain); type_at/definition describe "
               "code as last compiled; after every Edit of "
               "a .ml/.mli file a hook runs check automatically and returns its diagnostics. "
               "profile_* tools work only inside a mina-agent profile session, where the "
               "focus libraries are compiled with landmarks instrumentation.")
    pr = m["profiles"]
    out.append("build, test and test_one take profile=<" + "|".join(pr["names"]) + ">, which exports "
               + " and ".join(pr["vars"]) + " for that one run, as "
               "buildkite/scripts/profile-dependent-tests.sh does; omitted, the run inherits the "
               f"shell's, which is {pr['default']}. Expected values that vary by profile (the constraint "
               "counts, the *_snark_vk.json fixtures) are covered by the profile_dependent test, one "
               "run per profile. Switching profiles makes dune rebuild what the profile reaches.")
    if develop:
        out.append("This development session's shell is an allowlist (manifest.toml [develop]): only "
                   + ", ".join(MANIFEST_DATA["develop"]["bash_heads"])
                   + " and mina-agent " + "/".join(MANIFEST_DATA["develop"]["mina_agent_subcommands"])
                   + " run. Everything else is denied, including checked-in scripts under the repo and "
                   "any interpreter that could run one; a variable assignment in front of a denied "
                   "command does not change that. Raw dune/opam/nix/make is denied by permission rules "
                   "as well. build-config and Rust boundary files are deny-listed for edits.")
    else:
        out.append("Raw dune/opam/nix/cargo/make "
                   "commands are denied by permission rules; checked-in scripts run normally; "
                   "build-config and Rust boundary files are deny-listed for edits.")
    return out


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

SAMPLE_ERR = '''File "harness/scratch/scratch.ml", line 1, characters 14-26:
1 | let x : int = "not an int"
                  ^^^^^^^^^^^^
Error: This expression has type string but an expression was expected of type
         int
File "src/lib/foo/bar.ml", line 7, characters 2-5:
7 |   baz
      ^^^
Error (warning 32 [unused-value-declaration]): unused value baz.

File "src/lib/foo/dune", line 3, characters 12-20:
Error: Library "nope" not found.
'''
SAMPLE_TEST = '''File "src/lib/currency/currency.ml", line 1235, characters 6-61: fee sub_flagged (0.008 sec)
File "src/lib/currency/currency.ml", line 1240, characters 6-40: broken threw (Failure "x").
14 tests ran, 3 test_modules ran
'''


def selftest():
    errs = parse_dune_errors(SAMPLE_ERR)
    assert len(errs) == 3, errs
    assert errs[0].line == 1 and errs[0].col_start == 14 and errs[0].severity is Severity.ERROR
    assert "expected of type int" in errs[0].message, errs[0]
    assert errs[1].severity is Severity.ERROR and "unused value baz" in errs[1].message
    assert errs[2].file.endswith("dune") and errs[2].col_start == 12
    f, s = parse_test_output(SAMPLE_TEST)
    assert [(x.file, x.line, x.name) for x in f] == [("src/lib/currency/currency.ml", 1240, "broken")], f
    assert s == "14 tests ran, 3 test_modules ran"
    assert rel(os.path.join(ENV.repo, "src/lib/hex")) == "src/lib/hex"
    try:
        rel("/etc/passwd"); assert False
    except ValueError:
        pass
    g = GRAPH.get()
    assert "pickles" in g["libraries"]
    assert resolve_test("inline:currency")["command"] == ["dune", "build", "@src/lib/currency/runtest"]
    assert _profile_overrides("devnet") == {"DUNE_PROFILE": "devnet", "MINA_PROFILE": "devnet"}
    assert _profile_overrides("") == {}
    try:
        _profile_overrides("nope"); assert False
    except ValueError:
        pass
    fm = find_module("Mina_base.Zkapp_account.Stable")
    assert fm["hits"][0]["ml"] == "src/lib/mina_base/zkapp_account.ml" and fm["remaining"] == ["Stable"], fm
    assert find_module("Staged_ledger")["hits"][0]["role"] == "library"
    print(f"selftest ok: mode={ENV.mode} libraries={len(g['libraries'])}")

