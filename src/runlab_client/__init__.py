"""runlab-client: Python client for the RunLab research API.

    from runlab_client import RunLab

    rl = RunLab()                     # reads RUNLAB_URL and RUNLAB_TOKEN
    rl.plan("demo", "treatment", "baseline", "acc", hypothesis="the change helps", n_max=10,
            stopping="fixed")
    rl.result("demo", "treatment", acc=0.91, seed=0)
    rl.result("demo", "baseline", acc=0.89, seed=0)
    ...                               # more seeds
    v = rl.compare("demo", "treatment", "baseline", "acc")
    print(v["verdict"], v["text"])

Standard library only, Python 3.10+. Each method is one operation of openapi.json.
Errors raise RunLabError with the HTTP status, the server's message and the request id.
GET requests are retried on connection errors and on 429, 502, 503 and 504 with exponential
backoff; POST requests are never retried.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

__all__ = ["RunLab", "RunLabError", "result_line"]
__version__ = "0.1.0"

RETRY_STATUS = (429, 502, 503, 504)


class RunLabError(RuntimeError):
    """An HTTP error (`status` 400-599) or an unreachable server (`status` 0)."""

    def __init__(self, status: int, message: str, request_id: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}"
                         + (f" (request {request_id})" if request_id else ""))
        self.status = status
        self.message = message
        self.request_id = request_id


class RunLab:
    """A connection to one RunLab server.

    url: the server, e.g. "https://runlab.example.com" (default: env RUNLAB_URL).
    token: your API token (default: env RUNLAB_TOKEN).
    """

    def __init__(self, url: str | None = None, token: str | None = None, *,
                 timeout: float = 30.0, retries: int = 3, backoff_s: float = 0.5) -> None:
        url = url or os.environ.get("RUNLAB_URL", "")
        if not url:
            raise ValueError("no server: pass url= or set RUNLAB_URL")
        self.url = url.rstrip("/")
        self.token = token if token is not None else os.environ.get("RUNLAB_TOKEN", "")
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
                with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
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
        Returns `stored` and `already` (lines the server had already)."""
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
        """Let the server run the seeds of a planned comparison under a hard dollar cap.
        `command` is a name your server's operator set up; `params` fill its placeholders
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

    def wait_study(self, study_id: str, timeout: float = 24 * 3600,
                   poll_s: float = 30.0) -> dict[str, Any]:
        """Poll until the study stops running and return it. Raises TimeoutError after
        `timeout` seconds (the study keeps running on the server)."""
        deadline = time.monotonic() + timeout
        while True:
            s = self.study(study_id)
            if s.get("state") != "running":
                return s
            if time.monotonic() + poll_s > deadline:
                raise TimeoutError(f"study {study_id} still running after {timeout:.0f}s")
            time.sleep(poll_s)


def result_line(experiment: str, metric: str, value: float, *, seed: int | None = None,
                **params: Any) -> str:
    """`RESULT: <experiment> <metric>=<value> seed=<n> [<param>=<value> ...]`.

    Names use letters, digits, `_`, `.` and `-`; parameter values cannot hold spaces or `=`.
    """
    for k, v in params.items():
        if k == "reported":                    # the server reads it as a paper's number
            raise ValueError("param 'reported' is reserved")
        if " " in str(v) or "=" in str(v):
            raise ValueError(f"param {k}={v!r}: values cannot hold spaces or '='")
    extra = "".join(f" {k}={v}" for k, v in params.items())
    return (f"RESULT: {experiment} {metric}={float(value)!r}"
            + ("" if seed is None else f" seed={int(seed)}") + extra)
