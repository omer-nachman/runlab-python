# Changelog

All notable changes to `runlab-client`. Breaking changes come first in each release.
[Semantic versioning](https://semver.org/); in 0.x a minor version may break something, a
patch never does (see "Versioning and 1.0" in the README).

## 0.3.3 (2026-11-24)

### Fixed
- Results are no longer lost to a dropped connection: `plan`, `record` / `result`, `compare`
  and `work` are retried (3 times) on connection errors and on 429, 502, 503 and 504, like
  reads. RunLab stores each of them only once, so a retry is safe. Starting a study is still
  never retried.
- A 429 or 503 now waits what its `Retry-After` header asks (seconds or an HTTP date, at most
  60 s) instead of 0.5, 1 and 2 s, so a per-minute rate limit is waited out instead of failing.
- Type checkers (mypy, pyright) now read the client's type hints: the wheel ships `py.typed`.

### Added
- README: public-preview status, [Known limits](https://github.com/omer-nachman/runlab-python/blob/main/README.md#known-limits) with measured numbers,
  the versioning policy and the criteria for 1.0.
- `plan(..., scale=...)` documented ("log" or "rank" for skewed metrics).
- PyPI classifiers and links to the docs, changelog and issues.

## 0.3.2 (2026-11-21)

### Security
- `runlab login` opens a sign-in link in the browser only when it is an http(s) page on
  RunLab's own host (never plain http when the client uses https), and drops terminal control
  characters from what it prints. 0.2.0 to 0.3.1 opened any link and printed escape
  sequences as they came; upgrade.

### Changed
- README and PyPI summary open with the question the client answers: did my change actually
  help?

## 0.3.1 (2026-11-21)

### Changed
- README says who RunLab is for (AI researchers, ML engineers and their coding agents) and
  that training stays where it runs; signing in is one step that `runlab demo` does.

## 0.3.0 (2026-11-21)

### Added
- `runlab demo`: sign in if needed, then a 10-seed comparison on simulated numbers, ending in
  a verdict.

## 0.2.1 (2026-11-21)

### Added
- The `runlab-client` command (the same commands, for when another `runlab` comes first on
  PATH) and `python -m runlab_client`.

## 0.2.0 (2026-11-21)

### Added
- `runlab login`, `whoami` and `logout`: GitHub sign-in on any device; the key is saved for
  this machine only. `RunLab()` needs no arguments after a login.
- Worker studies: `start_worker_study`, `work`, `run_worker` and `runlab worker` run a study's
  seed pairs on your own machine until the sequential test decides.

### Security
- The bearer token is only sent to the URL that issued it; a redirect to another host drops it.

## 0.1.0 (2026-11-20)

First release: `RunLab` with `me`, `plan`, `record`, `result`, `compare`, `verdicts`,
`verdict`, `report`, `start_study`, `study`, `studies` and `wait_study`. Standard library only,
Python 3.10+.
