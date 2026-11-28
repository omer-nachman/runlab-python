"""`runlab verdict`: one pull-request check that answers "did this change help?".

    runlab verdict --results "logs/*.txt" --post

1. Reads the spec (default `.runlab/verdict.yml`) from the pull request's **base** commit, so
   a pull request cannot loosen the test it is judged by. No spec there: a neutral check.
2. When the spec lists `paths` and the pull request changes none of them: a neutral check,
   nothing recorded.
3. Records the RESULT lines of each `--results` file (`--arm` renames their experiment).
4. Compares each row of `checks` (`RunLab.compare`) and holds its verdict to the row's
   `expect`.
5. Writes the table (difference, its CI, p, seeds, verdict) to the job summary and, with
   `--post`, as a check run named "runlab verdict" plus one pull-request comment it keeps
   up to date.

The spec, in YAML (needs PyYAML: `pip install "runlab-client[yaml]"`) or JSON:

    paths: ["train/**", "configs/**"]   # optional: other changes need no verdict
    project: my-model                   # optional: the default for every row
    checks:
      - a: "pr-{pr}-{sha7}"             # {pr} {sha} {sha7} {branch} are filled in
        b: main
        metric: val_acc
        expect: better                  # better | not_worse (needs margin) | different
        margin: 0.005                   # smallest difference that matters, metric units
        better: auto                    # auto | higher | lower
        alpha: 0.05
        missing: failure                # failure | neutral, when an arm has no results

Exit status: 0 every row met its expectation (or no verdict was needed), 4 a row failed,
2 the spec, a results file, RunLab or GitHub stopped the check.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import _NAME, _OPENER, RunLab, RunLabError, __version__

CHECK_NAME = "runlab verdict"
MARK = "<!-- runlab-verdict -->"            # first line of the pull-request comment
DEFAULT_SPEC = ".runlab/verdict.yml"
EXPECT = {                                  # expect -> the verdicts that meet it
    "better": ("a_better",),
    "not_worse": ("a_better", "equivalent", "non_inferior"),
    "different": ("a_better", "b_better"),
}
ROW_KEYS = ("project", "a", "b", "metric", "expect", "margin", "better", "alpha", "missing")
TOP_KEYS = ("paths", "project", "checks")
BATCH_BYTES = 60_000                        # RESULT text per record call
MAX_FILES = 3000                            # GitHub lists at most this many files of a PR
SUMMARY_CHARS = 65_000                      # a check run's summary holds 65,535
ICON = {"success": "✅", "failure": "❌", "neutral": "⚪"}
EXIT_OK, EXIT_FAILED, EXIT_ERROR = 0, 4, 2


class SpecError(ValueError):
    """The spec, a `--results` argument or a results file cannot be used as given."""


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub HTTP {status}: {message}")
        self.status = status


# ------------------------------------------------------------------------- spec

@dataclass(frozen=True)
class Check:
    project: str
    a: str
    b: str
    metric: str
    expect: str = "better"
    margin: float | None = None
    better: str = "auto"
    alpha: float = 0.05
    missing: str = "failure"


@dataclass
class Spec:
    checks: list[Check]
    paths: list[str] = field(default_factory=list)


def _load(text: str) -> Any:
    try:
        import yaml  # type: ignore[import-untyped, unused-ignore]
    except ImportError:
        try:
            return json.loads(text)
        except ValueError:
            raise SpecError('the spec is not JSON; for YAML install PyYAML: '
                            'pip install "runlab-client[yaml]"') from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"the spec is not valid YAML: {exc}") from None


def _number(row: int, key: str, v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise SpecError(f"checks[{row}].{key} must be a number, not {v!r}")
    return float(v)


def _choice(row: int, key: str, v: Any, allowed: tuple[str, ...]) -> str:
    if v not in allowed:
        raise SpecError(f"checks[{row}].{key} must be one of {' | '.join(allowed)}, not {v!r}")
    return str(v)


def parse_spec(text: str) -> Spec:
    """The spec in `text` (YAML, or JSON without PyYAML). SpecError names the first problem;
    an unknown key is one, so a typo never turns a check off silently."""
    doc = _load(text)
    if not isinstance(doc, dict):
        raise SpecError("the spec must be a mapping with a `checks` list")
    extra = sorted(set(doc) - set(TOP_KEYS))
    if extra:
        raise SpecError(f"unknown key {extra[0]!r} (keys: {', '.join(TOP_KEYS)})")
    paths = doc.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise SpecError("paths must be a list of globs")
    rows = doc.get("checks")
    if not isinstance(rows, list) or not rows:
        raise SpecError("checks must be a non-empty list")
    default = doc.get("project")
    checks = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SpecError(f"checks[{i}] must be a mapping")
        extra = sorted(set(row) - set(ROW_KEYS))
        if extra:
            raise SpecError(f"checks[{i}]: unknown key {extra[0]!r} (keys: {', '.join(ROW_KEYS)})")
        names: dict[str, str] = {}
        for k in ("project", "a", "b", "metric"):
            v = row.get(k, default if k == "project" else None)
            if not isinstance(v, str) or not v.strip():
                raise SpecError(f"checks[{i}] needs {k}")
            names[k] = v.strip()
        expect = _choice(i, "expect", row.get("expect", "better"), tuple(EXPECT))
        margin = None if row.get("margin") is None else _number(i, "margin", row["margin"])
        if margin is not None and margin < 0:
            raise SpecError(f"checks[{i}].margin must be >= 0")
        if expect == "not_worse" and not margin:
            raise SpecError(f"checks[{i}]: expect not_worse needs a margin > 0, the largest "
                            "loss that still counts as not worse")
        alpha = _number(i, "alpha", row.get("alpha", 0.05))
        if not 0 < alpha < 1:
            raise SpecError(f"checks[{i}].alpha must be between 0 and 1")
        checks.append(Check(
            project=names["project"], a=names["a"], b=names["b"], metric=names["metric"],
            expect=expect, margin=margin,
            better=_choice(i, "better", row.get("better", "auto"), ("auto", "higher", "lower")),
            alpha=alpha,
            missing=_choice(i, "missing", row.get("missing", "failure"), ("failure", "neutral"))))
    return Spec(checks, paths)


# ---------------------------------------------------------------------- context

@dataclass
class Context:
    """Where the check runs: from the GitHub Actions environment, empty outside it."""
    repo: str = ""                 # owner/name
    sha: str = ""                  # the commit the check is for (the PR's head)
    base: str = ""                 # the PR's base commit; '' outside a pull request
    pr: int | None = None
    branch: str = ""
    api: str = "https://api.github.com"
    token: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Context:
        env = dict(os.environ if env is None else env)
        event: dict[str, Any] = {}
        if env.get("GITHUB_EVENT_PATH"):
            try:
                with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
                    event = json.load(f)
            except (OSError, ValueError):
                event = {}
        pull = event.get("pull_request") if isinstance(event, dict) else None
        ctx = cls(repo=env.get("GITHUB_REPOSITORY", ""), sha=env.get("GITHUB_SHA", ""),
                  branch=env.get("GITHUB_HEAD_REF") or env.get("GITHUB_REF_NAME", ""),
                  api=(env.get("GITHUB_API_URL") or cls.api).rstrip("/"),
                  token=env.get("GITHUB_TOKEN", ""))
        if isinstance(pull, dict):
            head, base = pull.get("head") or {}, pull.get("base") or {}
            ctx.pr = int(pull["number"]) if str(pull.get("number", "")).isdigit() else None
            ctx.sha = str(head.get("sha") or ctx.sha)
            ctx.branch = str(head.get("ref") or ctx.branch)
            ctx.base = str(base.get("sha") or "")
        return ctx

    def fill(self, name: str, what: str) -> str:
        """`name` with {pr} {sha} {sha7} {branch} filled in; SpecError when one is unknown
        here or the result is not a valid RunLab name."""
        values = {"pr": "" if self.pr is None else str(self.pr), "sha": self.sha,
                  "sha7": self.sha[:7], "branch": re.sub(r"[^A-Za-z0-9_.-]+", "-", self.branch)}

        def sub(m: re.Match[str]) -> str:
            key = m.group(1)
            if key not in values:
                raise SpecError(f"{what} {name!r}: unknown placeholder {{{key}}} "
                                "(use {pr}, {sha}, {sha7} or {branch})")
            if not values[key]:
                raise SpecError(f"{what} {name!r}: {{{key}}} is unknown outside a pull request")
            return values[key]

        out = re.sub(r"\{([^{}]*)\}", sub, name)
        if not _NAME.match(out):
            raise SpecError(f"{what} {out[:60]!r}: letters, digits, _ . - (at most 100), "
                            "starting with a letter or digit")
        return out


def filled(spec: Spec, ctx: Context) -> list[Check]:
    return [Check(ctx.fill(c.project, "project"), ctx.fill(c.a, "a"), ctx.fill(c.b, "b"),
                  ctx.fill(c.metric, "metric"), c.expect, c.margin, c.better, c.alpha,
                  c.missing) for c in spec.checks]


# ----------------------------------------------------------------------- GitHub

def github(ctx: Context, method: str, path: str, body: Any = None,
           accept: str = "application/vnd.github+json") -> tuple[Any, dict[str, str]]:
    """One GitHub REST call to `ctx.api` (the only host the token is sent to); the decoded
    body (text for a raw Accept) and the response headers. GitHubError on HTTP errors."""
    headers = {"Accept": accept, "User-Agent": f"runlab-verdict/{__version__}",
               "X-GitHub-Api-Version": "2022-11-28"}
    if ctx.token:
        headers["Authorization"] = f"Bearer {ctx.token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(ctx.api + path, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=30) as r:  # noqa: S310
            raw, got = r.read(), dict(r.headers.items())
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read() or b"{}").get("message") or exc.reason
        except (ValueError, AttributeError):
            msg = exc.reason
        raise GitHubError(exc.code, str(msg)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise GitHubError(0, f"cannot reach {ctx.api}: {getattr(exc, 'reason', exc)}") from None
    if "raw" in accept:
        return raw.decode("utf-8"), got
    return (json.loads(raw) if raw else None), got


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-c", "core.fsmonitor=false", *args], capture_output=True,
                          text=True, check=False, stdin=subprocess.DEVNULL)


def spec_text(path: str, ctx: Context, spec_from: str = "base") -> str | None:
    """The spec's text at the base commit (or the checked-out file with `spec_from="head"`,
    and outside a pull request); None when there is no spec there."""
    if spec_from == "head" or not ctx.base:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None
    rel = path
    while rel.startswith("./"):
        rel = rel[2:]
    try:
        got = _git("cat-file", "blob", f"{ctx.base}:./{rel}")
        if got.returncode == 0:
            return got.stdout
        if _git("cat-file", "-e", f"{ctx.base}^{{commit}}").returncode == 0:
            return None                         # the base commit is here, the spec is not
    except OSError:
        pass                                    # no git: ask GitHub
    if not ctx.repo:
        raise GitHubError(0, f"cannot read {path} at the base commit {ctx.base[:7]}: fetch it "
                             "(actions/checkout with fetch-depth: 0) or set GITHUB_REPOSITORY")
    try:
        text, _ = github(ctx, "GET", f"/repos/{ctx.repo}/contents/{urllib.parse.quote(rel)}"
                         f"?ref={ctx.base}", accept="application/vnd.github.raw")
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    return str(text)


def _glob_re(pattern: str) -> re.Pattern[str]:
    """A path glob: `*` and `?` stay inside one folder, `**` crosses folders, a trailing `/`
    means everything under it."""
    p = pattern.lstrip("/")
    if p.endswith("/"):
        p += "**"
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif p.startswith("**", i):
            out, i = out + ".*", i + 2
        elif p[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif p[i] == "?":
            out, i = out + "[^/]", i + 1
        elif p[i] == "[":
            j = p.find("]", i + 2)
            if j < 0:
                out, i = out + re.escape("["), i + 1
            else:
                chars = p[i + 1:j]
                chars = "^" + chars[1:] if chars.startswith("!") else chars
                out, i = out + "[" + chars.replace("\\", "\\\\") + "]", j + 1
        else:
            out, i = out + re.escape(p[i]), i + 1
    return re.compile(out + r"\Z")


def touches(files: list[str], paths: list[str]) -> bool:
    pats = [_glob_re(p) for p in paths]
    return any(pat.match(f) for f in files for pat in pats)


def pr_files(ctx: Context) -> list[str] | None:
    """The files the pull request changes (renames count under both names); None when they
    cannot be listed (more than GitHub lists, or no access), so every check runs."""
    if ctx.pr is None or not ctx.repo:
        return None
    files: list[str] = []
    try:
        for page in range(1, MAX_FILES // 100 + 1):
            got, _ = github(ctx, "GET", f"/repos/{ctx.repo}/pulls/{ctx.pr}/files"
                            f"?per_page=100&page={page}")
            if not isinstance(got, list):
                return None
            for f in got:
                files += [str(f.get(k)) for k in ("filename", "previous_filename") if f.get(k)]
            if len(got) < 100:
                return files
    except GitHubError:
        return None
    return None                                  # GitHub stops listing at 3000 files


# ---------------------------------------------------------------------- results

@dataclass
class Source:
    patterns: list[str]
    project: str = ""
    arm: str = ""


def parse_source(arg: str) -> Source:
    """'logs/*.txt --project P --arm A' -> Source."""
    words, src = shlex.split(arg), Source([])
    i = 0
    while i < len(words):
        w = words[i]
        if w in ("--project", "--arm"):
            if i + 1 >= len(words):
                raise SpecError(f"--results {arg!r}: {w} needs a value")
            setattr(src, w[2:], words[i + 1])
            i += 2
        elif w.startswith("-"):
            raise SpecError(f"--results {arg!r}: unknown option {w} (--project, --arm)")
        else:
            src.patterns.append(w)
            i += 1
    if not src.patterns:
        raise SpecError(f"--results {arg!r}: give a file or glob")
    return src


_LINE = re.compile(r"^\s*RESULT:\s*(\S+)(.*)$")


def result_lines(src: Source, ctx: Context) -> list[str]:
    """The RESULT lines of `src`'s files (other lines are dropped), their experiment renamed
    to `src.arm` when given. SpecError when a pattern matches no file."""
    arm = ctx.fill(src.arm, "--arm") if src.arm else ""
    out: list[str] = []
    for pattern in src.patterns:
        names = sorted(glob.glob(pattern, recursive=True))
        if not names:
            raise SpecError(f"no file matches {pattern!r}")
        for name in names:
            if os.path.isdir(name):
                continue
            with open(name, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _LINE.match(line.rstrip("\r\n"))
                    if m:
                        out.append(f"RESULT: {arm or m.group(1)}{m.group(2)}")
    return out


def batches(lines: list[str], limit: int = BATCH_BYTES) -> list[list[str]]:
    out: list[list[str]] = [[]]
    size = 0
    for line in lines:
        n = len(line.encode("utf-8")) + 1
        if out[-1] and size + n > limit:
            out.append([])
            size = 0
        out[-1].append(line)
        size += n
    return [b for b in out if b]


# ---------------------------------------------------------------------- judging

@dataclass
class Row:
    check: Check
    conclusion: str                # success | failure | neutral
    why: str
    v: dict[str, Any] | None = None    # the compare response; None when no verdict


def judge(rl: RunLab, c: Check) -> Row:
    """Compare `c`'s arms and hold the verdict to `c.expect`. An arm or metric with no
    results (RunLab answers 400) is `c.missing`; other RunLab errors raise."""
    opts: dict[str, Any] = {"better": c.better, "alpha": c.alpha}
    if c.margin is not None:
        opts["margin"] = c.margin
    try:
        v = rl.compare(c.project, c.a, c.b, c.metric, **opts)
    except RunLabError as exc:
        if exc.status != 400:
            raise
        return Row(c, c.missing, f"no verdict: {exc.message}")
    code = str(v.get("verdict", ""))
    if code == "refused":
        why = str(v.get("why") or v.get("text") or "").strip().splitlines()
        return Row(c, "failure", f"refused by the {v.get('refused_by') or 'integrity'} check"
                   + (f": {why[0]}" if why else ""), v)
    if code in EXPECT[c.expect]:
        return Row(c, "success", f"expected {c.expect}: met", v)
    hint = {"no_detectable_difference": " (add seeds to detect a smaller effect)",
            "insufficient": " (too few seeds to test: add seeds)"}.get(code, "")
    return Row(c, "failure", f"expected {c.expect}, got {code}{hint}", v)


def conclusion(rows: list[Row]) -> str:
    got = {r.conclusion for r in rows}
    return "failure" if "failure" in got else "success" if "success" in got else "neutral"


def _cell(text: Any, limit: int = 300) -> str:
    s = " ".join(str(text).split())
    s = s[:limit - 1] + "…" if len(s) > limit else s
    return s.replace("|", "\\|")


def _num(x: Any, fmt: str = "+.4g") -> str:
    return "–" if not isinstance(x, (int, float)) or isinstance(x, bool) else format(x, fmt)


def title(rows: list[Row]) -> str:
    n = len(rows)
    failed = sum(r.conclusion == "failure" for r in rows)
    if failed:
        return f"{failed} of {n} verdicts failed"
    met = sum(r.conclusion == "success" for r in rows)
    return f"{met} of {n} verdicts as expected" + (f", {n - met} without results"
                                                   if met < n else "")


def markdown(rows: list[Row], head: str) -> str:
    """The check's text: one table row per check, then RunLab's warnings."""
    level = {r.check.alpha for r in rows}
    ci = f"{(1 - level.pop()) * 100:g}% CI" if len(level) == 1 else "CI at 1 − alpha"
    out = [f"### {CHECK_NAME}: {head}", "",
           f"| | A vs B | metric | A − B [{ci}] | p | seeds (A, B) | verdict | why |",
           "|---|---|---|---|---|---|---|---|"]
    notes = []
    for r in rows:
        c, v = r.check, r.v or {}
        lo_hi = v.get("ci") if isinstance(v.get("ci"), list) and len(v["ci"]) == 2 else None
        effect = _num(v.get("diff")) + (f" [{_num(lo_hi[0])}, {_num(lo_hi[1])}]"
                                        if lo_hi else "")
        p = v.get("p")
        ptxt = "–" if not isinstance(p, (int, float)) else "<0.001" if p < 0.001 else f"{p:.3g}"
        seeds = f"{v['n_a']}, {v['n_b']}" if "n_a" in v and "n_b" in v else "–"
        arrow = " (lower is better)" if v.get("lower_better") else ""
        out.append(f"| {ICON[r.conclusion]} | `{c.a}` vs `{c.b}` | {_cell(c.metric)}{arrow} | "
                   f"{effect} | {ptxt} | {seeds} | {_cell(v.get('verdict') or 'missing')} | "
                   f"{_cell(r.why)} |")
        issues = list(v.get("warnings") or [])
        got = v.get("prereg")
        pre: dict[str, Any] = got if isinstance(got, dict) else {}
        if pre.get("status") in ("deviation", "tampered"):
            issues += [{"kind": f"prereg {pre['status']}", "message": i.get("message", "")}
                       for i in pre.get("issues") or [] if isinstance(i, dict)]
        notes += [f"- `{c.a}` vs `{c.b}`: {_cell(w.get('kind', ''), 60)}: "
                  f"{_cell(w.get('message', ''))}" for w in issues if isinstance(w, dict)]
    if notes:
        out += ["", "**Warnings**", *notes]
    out += ["", f"<sub>runlab-client {__version__}. A − B: the mean difference over seeds; "
                "a row fails when its verdict misses its `expect`.</sub>"]
    return "\n".join(out)


# ---------------------------------------------------------------------- posting

def post(ctx: Context, concl: str, head: str, text: str, comment: bool) -> str:
    """Write the check run (and, when `comment`, the pull-request comment, updated in place
    on later runs); the check run's URL. A 403 (a fork's read-only token) only warns."""
    url = ""
    try:
        got, _ = github(ctx, "POST", f"/repos/{ctx.repo}/check-runs", {
            "name": CHECK_NAME, "head_sha": ctx.sha, "status": "completed",
            "conclusion": concl, "output": {"title": head, "summary": text[:SUMMARY_CHARS]}})
        url = str((got or {}).get("html_url") or "")
    except GitHubError as exc:
        if exc.status != 403:
            raise
        print(f"::warning::{CHECK_NAME}: no check run written ({exc}); give the job "
              "`permissions: checks: write`", file=sys.stderr)
    if comment and ctx.pr is not None:
        body = f"{MARK}\n{text}"
        try:
            mine = None
            for page in range(1, 31):
                got, _ = github(ctx, "GET", f"/repos/{ctx.repo}/issues/{ctx.pr}/comments"
                                f"?per_page=100&page={page}")
                got = got if isinstance(got, list) else []
                mine = next((c for c in reversed(got)
                             if str(c.get("body", "")).startswith(MARK)), mine)
                if len(got) < 100:
                    break
            if mine:
                github(ctx, "PATCH", f"/repos/{ctx.repo}/issues/comments/{mine['id']}",
                       {"body": body})
            else:
                github(ctx, "POST", f"/repos/{ctx.repo}/issues/{ctx.pr}/comments",
                       {"body": body})
        except GitHubError as exc:
            if exc.status != 403:
                raise
            print(f"::warning::{CHECK_NAME}: no comment written ({exc}); give the job "
                  "`permissions: pull-requests: write`", file=sys.stderr)
    return url


def _append(env_key: str, text: str) -> None:
    path = os.environ.get(env_key)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")


# -------------------------------------------------------------------------- run

def run(args: Any, ctx: Context | None = None, rl: RunLab | None = None) -> int:
    """`runlab verdict` (see the module doc); the exit status."""
    ctx = ctx or Context.from_env()
    concl, head, text, rows = "neutral", "", "", []
    try:
        if args.post and not (ctx.repo and ctx.sha and ctx.token):
            raise SpecError("--post needs GITHUB_REPOSITORY, GITHUB_SHA and GITHUB_TOKEN "
                            "(set in GitHub Actions)")
        raw = spec_text(args.spec, ctx, args.spec_from)
        sources = [parse_source(s) for s in args.results]
        if raw is None:
            where = "the base commit" if ctx.base and args.spec_from == "base" else "here"
            head = f"No {args.spec} at {where}: no verdict"
            text = (f"### {CHECK_NAME}: {head}\n\nA pull request is judged by the spec on its "
                    "base branch, so a new or changed spec takes effect once it is merged.")
        else:
            spec = parse_spec(raw)
            checks = filled(spec, ctx)
            files = pr_files(ctx) if spec.paths and ctx.pr is not None else None
            if files is not None and not touches(files, spec.paths):
                head = "No watched paths changed: no verdict needed"
                text = f"### {CHECK_NAME}: {head}\n\nWatched: " + ", ".join(
                    f"`{_cell(p, 80)}`" for p in spec.paths)
            else:
                rl = rl or RunLab(args.url, args.token)
                rl.agent = ("runlab-verdict/action" if os.environ.get("GITHUB_ACTIONS") == "true"
                            else "runlab-verdict/cli")
                projects = sorted({c.project for c in checks})
                for src in sources:
                    project = ctx.fill(src.project, "--project") if src.project else ""
                    if not project and len(projects) > 1:
                        raise SpecError(f"--results {' '.join(src.patterns)}: give --project "
                                        f"(the spec names {', '.join(projects)})")
                    lines = result_lines(src, ctx)
                    for part in batches(lines):
                        rl.record(project or projects[0], part)
                    print(f"recorded {len(lines)} RESULT lines from "
                          f"{' '.join(src.patterns)}", file=sys.stderr)
                rows = [judge(rl, c) for c in checks]
                concl, head = conclusion(rows), title(rows)
                text = markdown(rows, head)
        url = post(ctx, concl, head, text, any(r.v is not None for r in rows)) \
            if args.post else ""
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (RunLabError, GitHubError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, RunLabError) and exc.status == 401:
            print("hint: set RUNLAB_TOKEN (in Actions: the RUNLAB_KEY secret) to a key from "
                  "`runlab login`", file=sys.stderr)
        return EXIT_ERROR
    print(text)
    _append("GITHUB_STEP_SUMMARY", text)
    failed = sum(r.conclusion == "failure" for r in rows)
    _append("GITHUB_OUTPUT", f"conclusion={concl}\nfailed={failed}\nchecks={len(rows)}\n"
                             f"url={url}")
    return EXIT_FAILED if concl == "failure" else EXIT_OK


def add_parser(sub: Any) -> None:
    v = sub.add_parser(
        "verdict", help="a pull-request check: record RESULT lines, compare the arms the "
        "spec names, pass or fail on the verdicts",
        description="Read the spec (.runlab/verdict.yml) from the pull request's base commit, "
        "record the RESULT lines of each --results file, compare each check's arms and write "
        "the verdicts to the job summary (and, with --post, a check run and a PR comment). "
        "Exit 0: as expected or not needed; 4: a check failed; 2: an error.")
    v.add_argument("--results", action="append", default=[], metavar='"GLOB [--project P] '
                   '[--arm A]"', help="files whose RESULT lines to record first (repeatable); "
                   "--arm renames their experiment, e.g. --arm 'pr-{pr}-{sha7}'")
    v.add_argument("--spec", default=DEFAULT_SPEC, help=f"the spec's path (default: "
                   f"{DEFAULT_SPEC})")
    v.add_argument("--spec-from", choices=("base", "head"), default="base",
                   help="read the spec from the PR's base commit (default; a PR cannot loosen "
                   "its own test) or from the checked-out files")
    v.add_argument("--post", action="store_true", help="write the check run and PR comment "
                   "(needs GITHUB_TOKEN with checks: write, pull-requests: write)")
    v.add_argument("--url", help="your RunLab URL (default: env RUNLAB_URL, else the one you "
                   "logged in to)")
    v.add_argument("--token", help="your API token (default: env RUNLAB_TOKEN, else the key "
                   "runlab login saved)")
