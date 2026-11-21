"""runlab-client: Python client for the RunLab research API.

    $ pip install runlab-client
    $ runlab demo                     # GitHub sign-in, then a first verdict on simulated numbers
    $ runlab login                    # sign in only; the key stays on this machine

    from runlab_client import RunLab

    rl = RunLab()                     # the key `runlab demo` / `login` saved, or RUNLAB_TOKEN
    rl.plan("demo", "treatment", "baseline", "acc", hypothesis="the change helps", n_max=10,
            stopping="fixed")
    rl.result("demo", "treatment", acc=0.91, seed=0)
    rl.result("demo", "baseline", acc=0.89, seed=0)
    ...                               # more seeds
    v = rl.compare("demo", "treatment", "baseline", "acc")
    print(v["verdict"], v["text"])

Seeds on your own machine: `runlab worker` (or `RunLab.run_worker`) runs both arms of each
seed pair RunLab hands out and posts the numbers back, until the plan's sequential test decides:

    runlab worker --project demo --a treatment --b baseline --metric acc --max-usd 5 \
        --usd-per-hour 0.8 --cmd "python train.py --arm {arm} --seed {seed}"

`runlab login` prints a link and a code (and opens a browser when this machine has a display);
sign in with GitHub there, on any device, and approve. The key is saved to
~/.config/runlab/token ($XDG_CONFIG_HOME respected), readable by you alone; `runlab whoami`
shows who it signs in as and `runlab logout` revokes it.

Standard library only, Python 3.10+. Each method is one operation of openapi.json.
Errors raise RunLabError with the HTTP status, RunLab's message and the request id.
GET requests are retried on connection errors and on 429, 502, 503 and 504 with exponential
backoff; POST requests are never retried.
"""

from __future__ import annotations

import json
import os
import random
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any

__all__ = ["RunLab", "RunLabError", "login", "result_line"]
__version__ = "0.3.1"

DEFAULT_URL = "https://runlab.tools"

RETRY_STATUS = (429, 502, 503, 504)


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urllib.parse.urlsplit(url)
    return p.scheme, (p.hostname or "").lower(), p.port or {"http": 80, "https": 443}.get(p.scheme)


class _KeepTokenOnOrigin(urllib.request.HTTPRedirectHandler):
    """urllib copies every header to wherever a redirect points, so one 302 from your URL
    (or anything in front of it) could hand the bearer token to another host. Keep Authorization only on the
    same scheme, host and port, or the http -> https upgrade of the same host (requests'
    `should_strip_auth`); drop it on any other redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        old, to = _origin(req.full_url), _origin(newurl)
        upgrade = old[:2] == ("http", to[1]) and to[0] == "https" and (old[2], to[2]) == (80, 443)
        if new is not None and old != to and not upgrade:
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_KeepTokenOnOrigin)


class RunLabError(RuntimeError):
    """An HTTP error (`status` 400-599) or RunLab unreachable (`status` 0)."""

    def __init__(self, status: int, message: str, request_id: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}"
                         + (f" (request {request_id})" if request_id else ""))
        self.status = status
        self.message = message
        self.request_id = request_id


class RunLab:
    """A connection to RunLab.

    url: your RunLab URL (default: env RUNLAB_URL, else the one `runlab login` signed in to,
        else https://runlab.tools).
    token: your API token (default: env RUNLAB_TOKEN, else the key `runlab login` saved, which
        is only ever sent to the URL it was issued by).
    """

    def __init__(self, url: str | None = None, token: str | None = None, *,
                 timeout: float = 30.0, retries: int = 3, backoff_s: float = 0.5) -> None:
        saved_url, saved = saved_login()
        self.url = _checked(url or os.environ.get("RUNLAB_URL") or saved_url or DEFAULT_URL)
        if token is None:
            token = os.environ.get("RUNLAB_TOKEN") or (
                saved if saved and _same(self.url, saved_url) else "")
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.backoff_s = backoff_s

    # ------------------------------------------------------------------ HTTP

    def request(self, method: str, path: str, body: dict[str, Any] | None = None,
                params: dict[str, Any] | None = None) -> Any:
        """One call to `/api/v1{path}`; returns the decoded JSON body."""
        q = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.url}/api/v1{path}" + (f"?{urllib.parse.urlencode(q)}" if q else "")
        rid = uuid.uuid4().hex[:16]
        headers = {"Accept": "application/json", "X-Request-Id": rid,
                   "User-Agent": f"runlab-client/{__version__}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            data = json.dumps(body or {}).encode("utf-8")
        attempts = 1 + (self.retries if method == "GET" else 0)
        for i in range(attempts):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with _OPENER.open(req, timeout=self.timeout) as r:  # noqa: S310
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_STATUS and i + 1 < attempts:
                    time.sleep(self.backoff_s * 2 ** i)
                    continue
                try:
                    msg = json.loads(exc.read() or b"{}").get("error") or exc.reason
                except ValueError:
                    msg = exc.reason
                raise RunLabError(exc.code, str(msg),
                                  exc.headers.get("X-Request-Id", rid)) from None
            except (urllib.error.URLError, OSError) as exc:
                if i + 1 < attempts:
                    time.sleep(self.backoff_s * 2 ** i)
                    continue
                raise RunLabError(0, f"cannot reach {self.url}: "
                                     f"{getattr(exc, 'reason', exc)}", rid) from None
        raise AssertionError("unreachable")  # pragma: no cover

    # --------------------------------------------------------------- account

    def me(self) -> dict[str, Any]:
        """Who the token belongs to: `name` and `role`. A quick check that access works."""
        return self.request("GET", "/me")

    # -------------------------------------------------------------- research

    def plan(self, project: str, a: str, b: str, metric: str, hypothesis: str, n_max: int,
             **options: Any) -> dict[str, Any]:
        """Pre-register the comparison before any seed runs. Returns `plan`, `power_check`
        and `text`. options: better ("auto" | "higher" | "lower"), alpha, n_min,
        stopping ("msprt" | "fixed"), amend (a reason, to replace an earlier plan)."""
        return self.request("POST", "/research/plans", {
            "project": project, "a": a, "b": b, "metric": metric,
            "hypothesis": hypothesis, "n_max": n_max, **options})

    def record(self, project: str, results: str | list[str]) -> dict[str, Any]:
        """Store RESULT lines (one string, or a list of lines from `result_line`).
        Returns `stored` and `already` (lines RunLab had already)."""
        text = results if isinstance(results, str) else "\n".join(results)
        return self.request("POST", "/research/results",
                            {"project": project, "results": text})

    def result(self, project: str, experiment: str, *, seed: int | None = None,
               params: dict[str, Any] | None = None, **metrics: float) -> dict[str, Any]:
        """One seed's numbers for one arm: `rl.result("demo", "treatment", acc=0.91, seed=3)`.
        Sends one RESULT line per metric in a single call."""
        if not metrics:
            raise ValueError("give at least one metric, e.g. acc=0.91")
        return self.record(project, [result_line(experiment, m, v, seed=seed, **(params or {}))
                                     for m, v in metrics.items()])

    def compare(self, project: str, a: str, b: str, metric: str,
                **options: Any) -> dict[str, Any]:
        """Judge arm `a` against arm `b` on `metric`. Returns `verdict`, the test's numbers
        and `text`. options: better ("auto" | "higher" | "lower"), alpha, margin."""
        return self.request("POST", "/research/compare", {
            "project": project, "a": a, "b": b, "metric": metric, **options})

    def verdicts(self, project: str = "", limit: int = 50,
                 recheck: bool = False) -> dict[str, Any]:
        """Recorded verdicts, newest first. recheck=True judges each pair again on today's
        results (`stale` says whether one changed)."""
        return self.request("GET", "/research/verdicts", params={
            "project": project or None, "limit": limit, "recheck": "1" if recheck else None})

    def verdict(self, verdict_id: int) -> dict[str, Any]:
        """One recorded verdict in full: each seed's values, the re-derived difference and
        whether it still holds on today's results."""
        return self.request("GET", f"/verdicts/{int(verdict_id)}")

    def report(self, project: str, *, arm: str = "", metric: str = "",
               q: float = 0.05) -> dict[str, Any]:
        """Which verdicts of a project you can quote, after correcting for the number of
        comparisons (false discovery rate `q`, Benjamini-Hochberg)."""
        return self.request("GET", "/research/report", params={
            "project": project, "arm": arm or None, "metric": metric or None, "q": q})

    # ----------------------------------------------------------- seed studies

    def start_study(self, project: str, command: str, a: str, b: str, metric: str,
                    max_usd: float, *, params: dict[str, Any] | None = None,
                    arm_params: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Let RunLab run the seeds of a planned comparison under a hard dollar cap.
        `command` names the training command to run; `params` fill its placeholders
        for both arms, `arm_params` per arm, keyed by arm name: {"mixup": {"alpha": "0.2"}}.
        A refusal raises RunLabError (400) before anything runs."""
        body: dict[str, Any] = {"project": project, "command": command, "a": a, "b": b,
                                "metric": metric, "max_usd": max_usd}
        if params:
            body["params"] = params
        if arm_params:
            body["arm_params"] = arm_params
        return self.request("POST", "/research/studies", body)

    def study(self, study_id: str) -> dict[str, Any]:
        """One study by id (`st_...`)."""
        return self.request("GET", f"/research/studies/{urllib.parse.quote(study_id, safe='')}")

    def studies(self, project: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Studies, newest first."""
        return self.request("GET", "/research/studies", params={
            "project": project or None, "limit": limit})["studies"]

    def start_worker_study(self, project: str, a: str, b: str, metric: str, max_usd: float,
                           usd_per_hour: float, *, params: dict[str, Any] | None = None,
                           arm_params: dict[str, dict[str, Any]] | None = None
                           ) -> dict[str, Any]:
        """A study whose seeds run on your machine (`run_worker`, `runlab worker`), stopped by
        the plan's sequential test, at `n_max` or before a pair would pass `max_usd`, with
        each pair's run time charged at `usd_per_hour` (what an hour of your machine costs).
        `params` / `arm_params` are handed back with each pair to fill your command."""
        body: dict[str, Any] = {"project": project, "a": a, "b": b, "metric": metric,
                                "max_usd": max_usd, "usd_per_hour": usd_per_hour,
                                "worker": True}
        if params:
            body["params"] = params
        if arm_params:
            body["arm_params"] = arm_params
        return self.request("POST", "/research/studies", body)

    def work(self, study_id: str, *, seed: int | None = None,
             results: str | list[str] | None = None, failed: list[str] | None = None,
             wall_s: float | None = None, worker: str | None = None,
             lease_s: float | None = None) -> dict[str, Any]:
        """Post a finished pair (its `seed`, RESULT lines, the arms that `failed`, its run
        time `wall_s`) and get `{"study", "pair"}`: the next pair to run (`seed`, `arms` in
        run order, `params` per arm), or None with `retry_after_s` while other workers hold
        the open pairs, or None once the study ended. Without `seed`, only asks."""
        body: dict[str, Any] = {"worker": worker or _worker_name()}
        if lease_s is not None:
            body["lease_s"] = lease_s
        if seed is not None:
            body.update(seed=int(seed), failed=list(failed or []),
                        results=results if isinstance(results, str)
                        else "\n".join(results or []), wall_s=wall_s)
        return self.request("POST", f"/research/studies/{urllib.parse.quote(study_id, safe='')}"
                                    "/work", body)

    def run_worker(self, study_id: str, cmd: str, *, worker: str | None = None,
                   cwd: str | None = None, timeout: float | None = None,
                   lease_s: float | None = None, log: Any = None) -> dict[str, Any]:
        """Run the study's seed pairs here until it ends; returns the ended study.

        `cmd` is your training command with `{arm}`, `{seed}` and your params' names as
        placeholders; it runs without a shell, and the last `<metric>=<value>` (or `:`) on
        its stdout is the arm's result (stderr passes through to your terminal). A run that
        exits non-zero, prints no value or passes `timeout` seconds counts as failed. Each
        run's time is posted too, as `wall_s` and `usd` results of its arm and seed, so a
        comparison can check that both arms got the same compute. `worker` names this worker:
        the same name gets its open pair back after a restart. `log` (a callable) receives one
        line per pair."""
        template = shlex.split(cmd)
        if not template:
            raise ValueError("cmd is empty")
        name = worker or _worker_name()
        out = self.work(study_id, worker=name, lease_s=lease_s)
        while True:
            study, pair = out["study"], out.get("pair")
            if pair is None:
                if study.get("state") != "running":
                    return study
                time.sleep(min(max(float(out.get("retry_after_s") or 10.0), 1.0), 60.0))
                out = self.work(study_id, worker=name, lease_s=lease_s)
                continue
            seed, metric = int(pair["seed"]), str(study["metric"])
            rate = float(study.get("usd_per_hour") or 0.0)
            lines: list[str] = []
            failed: list[str] = []
            got: list[str] = []
            t0 = time.monotonic()
            for arm in pair["arms"]:
                params = (pair.get("params") or {}).get(arm) or {}
                t_arm = time.monotonic()
                v = _run_arm(template, arm, seed, params, metric, cwd, timeout)
                dt = time.monotonic() - t_arm
                if v is None:
                    failed.append(arm)
                    got.append(f"{arm} failed")
                    continue
                safe = {k: x for k, x in params.items()
                        if k not in _RESERVED and "=" not in str(x)}
                lines.append(result_line(arm, metric, v, seed=seed, **safe))
                if metric not in ("wall_s", "usd"):         # the run's cost, as run-seeds does
                    lines.append(result_line(arm, "wall_s", round(dt, 3), seed=seed, **safe))
                    if rate > 0:
                        lines.append(result_line(arm, "usd", round(dt / 3600 * rate, 9),
                                                 seed=seed, **safe))
                got.append(f"{arm} {metric}={v:g}")
            wall = time.monotonic() - t0
            if log:
                log(f"seed {seed}: {', '.join(got)} ({wall:.1f} s)")
            out = self.work(study_id, worker=name, seed=seed, results=lines, failed=failed,
                            wall_s=wall, lease_s=lease_s)

    def wait_study(self, study_id: str, timeout: float = 24 * 3600,
                   poll_s: float = 30.0) -> dict[str, Any]:
        """Poll until the study stops running and return it. Raises TimeoutError after
        `timeout` seconds (the study keeps running in RunLab)."""
        deadline = time.monotonic() + timeout
        while True:
            s = self.study(study_id)
            if s.get("state") != "running":
                return s
            if time.monotonic() + poll_s > deadline:
                raise TimeoutError(f"study {study_id} still running after {timeout:.0f}s")
            time.sleep(poll_s)


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+=-]{0,99}\Z")
_HOLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_RESERVED = ("experiment", "metric", "value", "reported")   # not params of a RESULT line
_NUM = r"[-+]?(?:(?i:nan|inf(?:inity)?)(?!\w)|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"


def _worker_name() -> str:
    host = re.sub(r"[^A-Za-z0-9_.:-]", "-", socket.gethostname())[:40].lstrip("-_.:")
    return f"{host or 'worker'}-{os.getpid()}"


def fill(template: list[str], arm: str, seed: int, params: dict[str, Any]) -> list[str]:
    """`template` (a command split into words) with `{arm}`, `{seed}` and each param's
    `{name}` replaced inside every word; no shell ever parses the result. Names and values
    are checked first (letters, digits and `_.:+=-`, starting with a letter or digit), so
    none can start a flag or hold a space."""
    values = {"arm": arm, "seed": str(int(seed))}
    if not _NAME.match(arm):
        raise ValueError(f"bad arm name {arm[:40]!r}")
    for k, v in params.items():
        if not _KEY.match(str(k)) or not _VALUE.match(str(v)) or k in values:
            raise ValueError(f"bad param {str(k)[:40]}={str(v)[:40]!r}")
        values[str(k)] = str(v)
    missing = sorted({m for w in template for m in _HOLE.findall(w)} - set(values))
    if missing:
        raise ValueError(f"cmd needs {', '.join(missing)}: give them as params "
                         "(--param NAME=VALUE)")
    return [_HOLE.sub(lambda m: values[m.group(1)], w) for w in template]


def last_value(text: str, metric: str) -> float | None:
    """The last `<metric>=<value>` (or `<metric>: <value>`) in `text`; None if none."""
    hits = re.findall(rf"\b{re.escape(metric)}\s*[=:]\s*({_NUM})", text)
    return float(hits[-1]) if hits else None


def _run_arm(template: list[str], arm: str, seed: int, params: dict[str, Any], metric: str,
             cwd: str | None, timeout: float | None) -> float | None:
    argv = fill(template, arm, seed, params)
    try:
        p = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return last_value(p.stdout or "", metric)


def result_line(experiment: str, metric: str, value: float, *, seed: int | None = None,
                **params: Any) -> str:
    """`RESULT: <experiment> <metric>=<value> seed=<n> [<param>=<value> ...]`.

    Names use letters, digits, `_`, `.` and `-`; parameter values cannot hold spaces or `=`.
    """
    for k, v in params.items():
        if k == "reported":                    # RunLab reads it as a paper's number
            raise ValueError("param 'reported' is reserved")
        if " " in str(v) or "=" in str(v):
            raise ValueError(f"param {k}={v!r}: values cannot hold spaces or '='")
    extra = "".join(f" {k}={v}" for k, v in params.items())
    return (f"RESULT: {experiment} {metric}={float(value)!r}"
            + ("" if seed is None else f" seed={int(seed)}") + extra)


# ------------------------------------------------------------- runlab login

def config_dir() -> Path:
    """Where `runlab login` keeps its key: $XDG_CONFIG_HOME/runlab, else ~/.config/runlab
    (a relative XDG_CONFIG_HOME is ignored, as the XDG spec says)."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or ""
    return (Path(xdg) if os.path.isabs(xdg) else Path.home() / ".config") / "runlab"


def saved_login() -> tuple[str, str]:
    """(url, key) that `runlab login` saved here, or ("", "")."""
    d = config_dir()
    try:
        token = (d / "token").read_text(encoding="utf-8").strip()
    except OSError:
        return "", ""
    try:
        url = (d / "url").read_text(encoding="utf-8").strip()
    except OSError:
        url = ""
    return _base(url or DEFAULT_URL), token


def _write_private(path: Path, text: str) -> None:
    """`text` into `path`, readable by you alone: the file is created 0600 (never briefly
    wider, whatever the umask) in a 0700 directory, then swapped in whole."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_login(url: str, token: str) -> Path:
    """Save `token` for `url`; returns the token file's path. The old key goes first, so a
    key is never paired with another URL, even after a crash halfway."""
    d = config_dir()
    (d / "token").unlink(missing_ok=True)
    _write_private(d / "url", _base(url))
    _write_private(d / "token", token)
    return d / "token"


def forget_login() -> bool:
    """Delete the saved key and URL; whether there was a key."""
    d = config_dir()
    had = (d / "token").exists()
    for name in ("token", "url"):
        (d / name).unlink(missing_ok=True)
    return had


def _base(url: str) -> str:
    return url.strip().rstrip("/")


def _checked(url: str) -> str:
    """`url` without its trailing slash, if it is http(s)://<host> with no credentials in it."""
    base = _base(url)
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be http:// or https://<host>")
    if parts.username or parts.password:
        raise ValueError("pass the token as token=, not inside the url")
    return base


def _same(a: str, b: str) -> bool:
    return _base(a).lower() == _base(b).lower()


def _post_json(url: str, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    """POST JSON; (status, decoded body). Status 0 when RunLab cannot be reached."""
    req = urllib.request.Request(url, json.dumps(body).encode("utf-8"), {
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": f"runlab-client/{__version__}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": f"cannot reach {url}: {getattr(exc, 'reason', exc)}"}
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        out = {}
    return status, out if isinstance(out, dict) else {}


def _printable(text: Any) -> str:
    """`text` with terminal control characters dropped: RunLab's words are printed to your
    terminal, where an escape sequence could rewrite what it shows."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(text))


def _own_page(link: str, base: str) -> bool:
    """`link` is an http(s) page on RunLab's own host, and not plain http when `base` is
    https: the only kind of link `login` opens in a browser."""
    to, at = urllib.parse.urlsplit(link), urllib.parse.urlsplit(base)
    return (to.scheme in ("http", "https") and bool(to.hostname)
            and to.hostname == at.hostname and not (at.scheme == "https" and to.scheme == "http"))


def _has_display() -> bool:
    """A browser here would open a window rather than take over this terminal (SSH, a
    headless GPU box)."""
    return sys.platform in ("darwin", "win32") or bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def login(url: str | None = None, *, browser: bool = True, host: str | None = None,
          show: Any = None, timeout: float = 30.0) -> dict[str, Any]:
    """Sign this machine in (what `runlab login` runs): print a link and a code, open the
    link when there is a display, wait while you sign in with GitHub and approve, then save
    the new key (`save_login`). Returns `user`, `role` and `url`. A denial, an expired code
    or an unreachable RunLab raises RunLabError and saves nothing. `show` (a callable)
    receives each line meant for you (default: printed to stderr)."""
    say = show or (lambda line: print(line, file=sys.stderr))
    base = _checked(url or os.environ.get("RUNLAB_URL") or saved_login()[0] or DEFAULT_URL)
    parts = urllib.parse.urlsplit(base)
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        say(f"warning: {base} is plain http, so the key would travel unencrypted")
    name = host or socket.gethostname()
    status, start = _post_json(f"{base}/auth/device/start", {"host": name}, timeout)
    if status != 200 or not start.get("device_code"):
        raise RunLabError(status, str(start.get("error") or "cannot start a sign-in"))
    link = _printable(start.get("verification_uri_complete") or start.get("verification_uri"))
    say(f"Sign in to RunLab: open {link}")
    say(f"and check that the page shows the code {_printable(start.get('user_code'))} (it "
        f"works for {int(start.get('expires_in') or 600) // 60} minutes; open it on any device).")
    own = _own_page(link, base)
    if not own:
        say(f"warning: that link is not RunLab's sign-in page on {parts.hostname}: "
            "not opened; open it only if you trust it")
    if browser and own and _has_display():
        try:
            webbrowser.open(link)
        except Exception as exc:  # noqa: BLE001 - the printed link still works
            say(f"(no browser opened: {exc}; open the link above)")
    say("Waiting for you to approve in the browser (Ctrl-C to stop)...")
    interval = float(start.get("interval") or 5)
    deadline = time.monotonic() + float(start.get("expires_in") or 600)
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, out = _post_json(f"{base}/auth/device/poll",
                                 {"device_code": start["device_code"]}, timeout)
        err = out.get("error")
        if status == 200 and out.get("token"):
            save_login(base, str(out["token"]))
            return {"user": out.get("user"), "role": out.get("role"), "url": base}
        if err == "slow_down":
            interval = float(out.get("interval") or interval + 5)
        elif err == "expired_token":
            raise RunLabError(status, "the code expired: run `runlab login` again")
        elif err == "access_denied":
            raise RunLabError(status, "denied in the browser: nothing was saved")
        elif err != "authorization_pending" and status not in (0, *RETRY_STATUS):
            raise RunLabError(status, str(err or "the sign-in failed"))
    raise RunLabError(0, "the code expired: run `runlab login` again")


def _cmd_login(args: Any) -> int:
    try:
        who = login(args.url, browser=not args.no_browser)
    except RunLabError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped: nothing was saved", file=sys.stderr)
        return 130
    print(f"Logged in as {who['user']}")
    return 0


def _cmd_whoami(args: Any) -> int:
    rl = RunLab(args.url)
    if not rl.token:
        print(f"not logged in to {rl.url}: run `{args.prog} login`", file=sys.stderr)
        return 1
    try:
        me = rl.me()
    except RunLabError as exc:
        hint = f": run `{args.prog} login`" if exc.status == 401 else ""
        print(f"error: {exc}{hint}", file=sys.stderr)
        return 1
    print(f"{me.get('name')} ({me.get('role')}) at {rl.url}")
    return 0


def _cmd_logout(args: Any) -> int:
    url, token = saved_login()
    if not token:
        print("not logged in on this machine")
        return 0
    try:
        RunLab(url, token, retries=0).request("DELETE", "/me/token")
        how = "key revoked"
    except RunLabError as exc:
        if exc.status != 401:
            print(f"error: cannot revoke the key at {url}: {exc.message}. It stays saved: run "
                  f"`{args.prog} logout` again, or delete {config_dir() / 'token'} to forget it "
                  "here only", file=sys.stderr)
            return 1
        how = "the key had already stopped working"
    forget_login()
    print(f"Logged out of {url} ({how})")
    if os.environ.get("RUNLAB_TOKEN"):
        print("note: RUNLAB_TOKEN is still set in this shell", file=sys.stderr)
    return 0


DEMO_SEEDS = 10
QUICKSTART = ("https://github.com/omer-nachman/runlab-python/blob/main/examples/"
              "quickstart.py")


def _demo_train(arm: str, seed: int) -> float:
    """The demo's simulated training run, as in examples/quickstart.py: accuracy 0.80 with
    seed noise (sd 0.01), and the change ("treatment") adds +0.02."""
    rng = random.Random(f"{arm}-{seed}")
    return 0.80 + rng.gauss(0, 0.01) + (0.02 if arm == "treatment" else 0.0)


def _cmd_demo(args: Any) -> int:
    """Sign in if needed, then the whole loop on simulated numbers: plan, record 10 seeds per
    arm, compare, check it is quotable; prints the verdict and where to go next."""
    def say(line: str) -> None:
        print(line, file=sys.stderr)

    t0, project = time.monotonic(), args.project
    try:
        rl = RunLab(args.url)
        if not rl.token:
            say(f"Not logged in to {rl.url}: signing in first.")
            say(f"Logged in as {login(args.url, browser=not args.no_browser)['user']}")
            rl = RunLab(args.url)
        say(f"1/4 plan: treatment vs baseline on acc, {DEMO_SEEDS} seeds, one test at the end")
        rl.plan(project, "treatment", "baseline", "acc", n_max=DEMO_SEEDS, stopping="fixed",
                hypothesis="the change raises accuracy")
        say(f"2/4 record: {DEMO_SEEDS} seeds per arm (simulated: the change adds +0.02)")
        rl.record(project, [result_line(arm, "acc", round(_demo_train(arm, seed), 6), seed=seed)
                            for seed in range(DEMO_SEEDS) for arm in ("treatment", "baseline")])
        say("3/4 compare: paired test over the shared seeds")
        v = rl.compare(project, "treatment", "baseline", "acc")
        say("4/4 report: may a write-up quote it?")
        claims = rl.report(project, arm="treatment", metric="acc").get("claims") or []
        listed = rl.verdicts(project, limit=1)
        rows = listed.get("verdicts") if isinstance(listed, dict) else listed
    except RunLabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:                    # a project name RunLab cannot store
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130
    print()
    print(v.get("text") or v.get("verdict"))
    for c in claims:
        print(f"{c.get('status')}: {c.get('quote')}")
    vid = rows[0].get("id") if rows and isinstance(rows[0], dict) else None
    if vid is not None:
        print(f"\nThe verdict page: {rl.url}/v/{vid}")
    print(f"\nDone in {time.monotonic() - t0:.0f} s, in your project `{project}`. The numbers "
          "were simulated.\nNext, your own runs: replace train() in " + QUICKSTART)
    return 0


# ---------------------------------------------------------------- command line

def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    """`runlab login | whoami | logout | worker ...` (see the module doc); also installed as
    `runlab-client`, and run by `python -m runlab_client`. Every hint names `prog`, the
    command that was run (default: `runlab-client` when run under that name, else `runlab`),
    so following it reaches this client and not another `runlab` on PATH."""
    import argparse

    if prog is None:
        prog = "runlab-client" if Path(sys.argv[0]).name.startswith("runlab-client") else "runlab"
    ap = argparse.ArgumentParser(prog=prog, description="RunLab from the command line.")
    ap.add_argument("--version", action="version", version=f"runlab-client {__version__}")
    ap.set_defaults(prog=prog)
    sub = ap.add_subparsers(dest="command", required=True)
    lg = sub.add_parser("login", help="sign this machine in with GitHub",
                        description="Print a link and a code, open the link when this machine "
                        "has a display, and wait while you sign in with GitHub and approve. "
                        "The key is saved to ~/.config/runlab/token, readable by you alone.")
    lg.add_argument("--url", help="your RunLab URL (default: env RUNLAB_URL, else the one you "
                    f"logged in to, else {DEFAULT_URL})")
    lg.add_argument("--no-browser", action="store_true", help="only print the link")
    who = sub.add_parser("whoami", help="who this machine is signed in as")
    who.add_argument("--url", help="your RunLab URL (default: the one you logged in to)")
    sub.add_parser("logout", help="revoke the saved key and delete it")
    dm = sub.add_parser("demo", help="try RunLab in one command: sign in if needed, then a "
                        "10-seed comparison on simulated numbers, ending in a verdict",
                        description="Sign in if this machine is not yet, then plan, record 10 "
                        "simulated seeds per arm, compare and report, in the project --project.")
    dm.add_argument("--project", default="runlab-demo",
                    help="the project to use (default: runlab-demo)")
    dm.add_argument("--url", help="your RunLab URL (default: env RUNLAB_URL, else the one you "
                    f"logged in to, else {DEFAULT_URL})")
    dm.add_argument("--no-browser", action="store_true", help="only print the sign-in link")
    w = sub.add_parser("worker", help="run a study's seed pairs on this machine",
                       description="Run seed pairs of a planned comparison here and post the "
                       "numbers to RunLab until its sequential test decides, n_max seeds ran "
                       "or the next pair would pass --max-usd.")
    w.add_argument("--url", help="your RunLab URL (default: env RUNLAB_URL, else the one you "
                   "logged in to)")
    w.add_argument("--token", help="your API token (default: env RUNLAB_TOKEN, else the key "
                   "runlab login saved)")
    w.add_argument("--cmd", required=True, help='the training command, with {arm}, {seed} and '
                   'param names as placeholders, e.g. "python train.py --arm {arm} --seed '
                   '{seed}"; it prints <metric>=<value>')
    w.add_argument("--study", help="work on this study (st_...) instead of starting one")
    w.add_argument("--project")
    w.add_argument("--a", help="the treatment arm")
    w.add_argument("--b", help="the baseline arm")
    w.add_argument("--metric", help="the planned metric")
    w.add_argument("--max-usd", type=float, help="the study's dollar cap")
    w.add_argument("--usd-per-hour", type=float, help="what an hour of this machine costs")
    w.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                   help="a placeholder's value for both arms (repeatable)")
    w.add_argument("--arm-param", action="append", default=[], metavar="ARM:NAME=VALUE",
                   help="a placeholder's value for one arm (repeatable)")
    w.add_argument("--worker", help="this worker's name; the same name resumes its pair")
    w.add_argument("--lease-s", type=float, help="seconds a pair stays reserved for this "
                   "worker (default 3600); set it above one pair's run time")
    w.add_argument("--timeout", type=float, help="seconds per run; a longer run counts as "
                   "failed")
    w.add_argument("--cwd", help="where the command runs (default: here)")
    args = ap.parse_args(argv)
    if args.command != "worker":
        return {"login": _cmd_login, "whoami": _cmd_whoami, "logout": _cmd_logout,
                "demo": _cmd_demo}[args.command](args)
    try:
        rl = RunLab(args.url, args.token)
        sid = args.study
        if not sid:
            need = [f"--{k.replace('_', '-')}" for k in ("project", "a", "b", "metric",
                                                         "max_usd", "usd_per_hour")
                    if getattr(args, k) is None]
            if need:
                ap.error(f"give --study, or {' '.join(need)} to start one")
            bad = [x for x in args.param if "=" not in x] + [
                x for x in args.arm_param if ":" not in x or "=" not in x.partition(":")[2]]
            if bad:
                ap.error(f"give --param NAME=VALUE and --arm-param ARM:NAME=VALUE, not {bad[0]!r}")
            params = dict(kv.split("=", 1) for kv in args.param)
            per: dict[str, dict[str, str]] = {}
            for x in args.arm_param:
                arm, _, kv = x.partition(":")
                k, _, v = kv.partition("=")
                per.setdefault(arm, {})[k] = v
            st = rl.start_worker_study(args.project, args.a, args.b, args.metric,
                                       args.max_usd, args.usd_per_hour, params=params,
                                       arm_params=per)
            sid = st["id"]
            print(f"study {sid}: {args.a} vs {args.b} on {args.metric}, up to "
                  f"{st.get('n_max')} seed pairs, cap ${args.max_usd:g}", file=sys.stderr)
        end = rl.run_worker(sid, args.cmd, worker=args.worker, cwd=args.cwd,
                            timeout=args.timeout, lease_s=args.lease_s,
                            log=lambda line: print(line, file=sys.stderr))
    except RunLabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    win = f", winner {end['winner']}" if end.get("winner") else ""
    print(f"study {sid}: {end.get('state')} ({end.get('decision')}{win}) after "
          f"{end.get('pairs')} pairs, ${float(end.get('usd') or 0):.4f}")
    print(json.dumps(end, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
