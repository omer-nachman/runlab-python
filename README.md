# runlab-client

[![PyPI](https://img.shields.io/pypi/v/runlab-client)](https://pypi.org/project/runlab-client/)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Status: public preview](https://img.shields.io/badge/status-public%20preview-orange)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/omer-nachman/runlab-python/blob/main/LICENSE)

**Did my change actually help?**

RunLab compares a change (a new augmentation, loss or learning rate) with its baseline across
random seeds and tells you whether it really improved your metric. For AI researchers and ML
engineers, and coding agents working with them.

Your training stays where it runs today. You record each seed's metric for both arms from
Python or the `runlab` command, and RunLab gives back a verdict you can quote, built from:

- a pre-registered plan;
- a paired test across seeds, with a 95 % confidence interval;
- checks that refuse a comparison that would mislead, e.g. arms tuned with unequal budgets.

**Status: public preview (0.x).** Method names, response fields and the CLI may still change
before 1.0; every change is in [CHANGELOG.md](https://github.com/omer-nachman/runlab-python/blob/main/CHANGELOG.md),
breaking ones first. The statistics are not a preview: each test is checked by simulation, and
where one is known to be weak it is listed under [Known limits](#known-limits), with numbers.

## Documentation

- [API reference](https://omer-nachman.github.io/runlab-python/api.html)
- [Examples](https://github.com/omer-nachman/runlab-python/tree/main/examples): Python and curl

## Get started

```bash
pip install runlab-client       # Python 3.10+, no dependencies
runlab demo                     # sign in with GitHub, then a first verdict on simulated numbers
```

The first time, a browser opens to sign in with GitHub. Then the demo plans, records 10 seeds
per arm, compares and reports, in seconds:

```text
treatment vs baseline on acc (higher is better): treatment is better
  paired_t: diff +0.01868 [95% CI +0.01371, +0.02365], p=1.37e-05, n=10 (A 10, B 10)
  ...
quotable: treatment − baseline on acc = +0.01868 (95 % CI [+0.01371, +0.02365]; ...)
The verdict page: https://runlab.tools/v/...
```

Next, your own runs: the [Quickstart](#quickstart) below, with your training run in place of
the simulated one.

## Quickstart

```python
from runlab_client import RunLab

rl = RunLab()                                    # the key `runlab demo` / `login` saved

# 1. Pre-register, before any seed runs.
rl.plan("resnet-aug", "mixup", "baseline", "acc",
        hypothesis="mixup raises top-1 accuracy", n_max=10, stopping="fixed")

# 2. Record each seed's metric for both arms (use the same seeds for both).
for seed in range(10):
    rl.result("resnet-aug", "mixup", acc=train("mixup", seed), seed=seed)
    rl.result("resnet-aug", "baseline", acc=train("baseline", seed), seed=seed)

# 3. Compare once, after the planned seeds ran.
v = rl.compare("resnet-aug", "mixup", "baseline", "acc")
print(v["verdict"], v["prereg"]["status"])      # a_better ok
print(v["text"])
# mixup vs baseline on acc (higher is better): mixup is better
#   paired_t: diff +0.0178 [95% CI +0.0094, +0.0262], p=0.00098, n=10 (A 10, B 10)
#   ... (relative effect, rank test, seed stability, fragility, plan, equal budget)

# 4. May a write-up quote it?
for c in rl.report("resnet-aug")["claims"]:
    print(c["status"], c["quote"])                # quotable mixup − baseline on acc = ...
```

`train` is your training run.
[examples/quickstart.py](https://github.com/omer-nachman/runlab-python/blob/main/examples/quickstart.py)
runs as is, with a simulated `train`;
[examples/curl.sh](https://github.com/omer-nachman/runlab-python/blob/main/examples/curl.sh)
is the same loop over plain HTTP.

## The loop

| Step | Client | HTTP | What RunLab does |
|---|---|---|---|
| 1. Plan | `plan(project, a, b, metric, hypothesis, n_max)` | `POST /api/v1/research/plans` | Stores the plan and its hash. Estimates power from seeds already recorded (`power_check`). |
| 2. Record | `result(project, arm, seed=s, metric=value)` or `record(project, lines)` | `POST /api/v1/research/results` | Stores one row per arm, seed and metric. A line it already has is skipped, so retries are safe. |
| 3. Compare | `compare(project, a, b, metric)` | `POST /api/v1/research/compare` | Runs the paired t-test on a − b over shared seeds (Welch's test if fewer than 2 are shared). Checks the plan and records the verdict. |
| 4. Quote | `report(project)` | `GET /api/v1/research/report` | Says which verdicts a write-up may quote, after the false-discovery-rate correction across every comparison in the project. |
| Audit | `verdicts(project)`, `verdict(id)` | `GET /api/v1/research/verdicts`, `GET /api/v1/verdicts/{id}` | Shows every recorded verdict, each seed's values, and whether it still holds on today's results. |
| Delegate | `start_study(...)`, `wait_study(id)` | `POST /api/v1/research/studies` | Runs the seeds itself under a dollar cap and stops once the evidence decides (see below). |

### Results

RunLab stores each result as one line:

```
RESULT: <arm> <metric>=<number> seed=<n> [<param>=<value> ...]
```

`result()` builds these lines, so you only write them yourself when your training script
prints them (send its log to `record()`) or when you call the HTTP API directly.

- Names use letters, digits, `_`, `.` and `-`; arm names start with a letter.
- Lines that do not start with `RESULT:` are ignored, so a whole training log can go to
  `record()` as is.
- If one RESULT line is malformed, or there is none, the call stores nothing and fails with
  a 400.
- Project names are case-insensitive and may not contain `~`.
- Params are optional; keep an arm's params the same on every seed. Each distinct set of
  params counts as one tuned configuration. A compare whose arms differ by more than 1.25x
  in configurations is refused (`refused_by: equal_budget`).

### Verdicts

`a` is the treatment, `b` the baseline; `diff` is the mean of a − b, so it is positive when a
scored higher. Lower is better for metric names containing `loss`, `error`, `err`,
`perplexity`, `ppl`, `wer`, `cer`, `mse`, `mae`, `rmse`, `nll`, `latency`, `fid` (also
`fid50k`, `fid10k`), `kid`, `regret`, `bpb`, `bpc`, `bpd`, `bits_per_byte`, `bits_per_char`,
`bits_per_dim`, `cross_entropy`, `xent`, `kl`, `ece`, `fpr`, `fnr` or `eer` as a whole word
(between `_`, `.`, `/` or `-`), higher otherwise; `better="higher"` or `better="lower"`
overrides. Pass `better` whenever your metric's name is not in this list.

| `verdict` | Meaning |
|---|---|
| `a_better` / `b_better` | p < alpha (default 0.05) and the difference favours that arm. |
| `no_detectable_difference` | p ≥ alpha. This is not evidence that the arms are equal; for that, pass `margin`. |
| `equivalent` | With `margin`: the 95 % CI lies inside ±margin. |
| `non_inferior` | With `margin`: a is worse than b by less than margin. |
| `insufficient` | An arm has fewer than 2 seeds, so there is no test. |
| `refused` | A check found that any verdict would mislead. `refused_by` names the check and `why` says what to fix. |

Next to `verdict`, read `prereg.status`: `ok`; `deviation`, with `issues` (e.g. compared
before `n_max` seeds, or planned after more than 3 seeds were seen); `missing` (no plan); or
`tampered`. Then read `warnings`, e.g. `peeking`.

### What keeps a verdict honest

| Rule | Why | What you see |
|---|---|---|
| Plan first | Choosing the metric or the number of seeds after seeing results inflates false positives. | `prereg.status` is `deviation` or `missing`. Amending a plan needs a reason, and every version is kept. |
| Look once | `compare` is a fixed-n test. Re-reading its p-value as seeds arrive breaks its alpha. | Each call on grown data is a new "look". From the second look on, the response warns `peeking`, and `report()` marks the pair `not_quotable`. A call on unchanged data is the same look. To stop early validly, use a study. |
| Equal budgets | A tuned arm against an untuned baseline measures the tuning, not the change. | With more than 1.25x the configurations on one arm, the verdict is `refused` (`refused_by: equal_budget`). |
| Many comparisons | At alpha 0.05, about 1 in 20 pairs with no real difference still comes out a "win". | `report()` applies Benjamini-Hochberg across all pairs in the project. Each claim is `quotable`, `quotable_with_caveat` or `not_quotable`, and carries a ready `quote`. |
| Results can go bad | A seed or dataset later found broken undermines verdicts built on it. | `verdicts()` counts verdicts built on results since marked bad in `stale`; `verdicts(recheck=True)` judges each pair again on today's results (`unchanged`, `inputs_changed` or `flipped`). |

### Known limits

Every test is checked by simulation: 10,000 synthetic data sets per case, at 3, 5, 10 and 30
seeds; a case counts as failing only when it fails again on a second, independent run. These
cases fail, and RunLab does **not** warn about them in the verdict (a skewness warning was tried
and dropped: it missed most of the false wins):

| Data | What goes wrong | Measured (target) | What to do |
|---|---|---|---|
| Skewed per-seed differences: latency, cost, perplexity, a loss with rare blow-ups | `compare` calls a win too often; its 95 % CI is too narrow | false wins 0.12-0.18 (0.05); CI coverage 0.82-0.88 (0.95) | before any seed runs, `plan(..., scale="log")` (or `"rank"`): `compare` then tests on that scale |
| The same, in a study (sequential test, one look per seed) | stops on a false win too often; the interval at the stop is too narrow | false wins 0.15 / 0.21 at 10 / 30 seeds (0.05); coverage 0.79-0.93 (0.95) | record the metric's log as its own metric and run the study on that |
| The same, with `margin` (equivalence) | calls arms equivalent too often when the true gain equals the margin | 0.16-0.24 (0.05) | as in the first row |

## Studies: let RunLab run the seeds

RunLab runs both arms one seed pair at a time and stops as soon as an always-valid sequential test
(mSPRT) decides. It also stops at `n_max`, or at your dollar cap, whichever comes first.

```python
rl.plan("resnet-aug", "mixup", "baseline", "acc",
        hypothesis="mixup raises top-1 accuracy", n_max=20)        # stopping="msprt", the default
s = rl.start_study("resnet-aug", command="train", a="mixup", b="baseline", metric="acc",
                   max_usd=5, arm_params={"mixup": {"alpha": "0.2"}})
s = rl.wait_study(s["id"])
print(s["state"], s["winner"], s["pairs"], s["usd"])                  # e.g. decided mixup 6 0.84
```

- `command` names the training command to run. `params` and `arm_params` fill its
  placeholders, using values of letters, digits and `_.:+=-` only.
- Every check runs before anything starts. A refusal raises `RunLabError` with status 400
  and costs nothing, e.g. an unknown command, no msprt plan, or a cap that is too high.
- `state` is one of:

  | `state` | Meaning |
  |---|---|
  | `running` | Still running seeds. |
  | `decided` | The sequential test stopped. `decision` is `A` or `B` (`winner` names the arm), or `futile` (no detectable difference at this budget) or `equivalent` (`winner` is null). |
  | `undecided` | `n_max` seeds ran without a decision. |
  | `budget` | Stopped undecided: the next pair would have passed `max_usd`. |
  | `killed` | Still running when the time `max_usd` buys ran out; stopped hard. |
  | `failed` | The command failed or printed no result; see `detail`. |
  | `lost` | Stopped reporting long past its deadline. |
  | `refused` | Refused at the start (that call raised a 400); listed for the record. |

### Run the seeds on your own machine

Your GPU box, laptop or cluster node can run the seeds instead: RunLab hands out seed pairs,
decides with the same sequential test, and stops at `n_max` or before the next pair would pass
your dollar cap. Your training command prints `<metric>=<value>`; `{arm}`, `{seed}` and your
params' names are filled in (no shell runs it).

```bash
runlab worker --project resnet-aug --a mixup --b baseline --metric acc \
    --max-usd 5 --usd-per-hour 0.8 --param epochs=10 \
    --cmd "python train.py --arm {arm} --seed {seed} --epochs {epochs}"
# seed 0: mixup acc=0.853, baseline acc=0.801 (412.3 s)
# ...
# study st_...: decided (A, winner mixup) after 6 pairs, $0.5496
```

- `--usd-per-hour` is what an hour of your machine costs; each pair's run time is charged at
  it, so `--max-usd` caps your own spend.
- Start more workers with `--study st_...` (another terminal or machine): each gets its own
  seeds. Give each a `--worker` name to get its open pair back after a restart.
- A run that exits non-zero, prints no value or passes `--timeout` seconds counts as failed;
  the pair is skipped, as when RunLab runs the seeds.
- Each run's time is recorded next to its metric, as `wall_s` and `usd` of its arm and seed.
- From Python: `s = rl.start_worker_study(...)`, then `rl.run_worker(s["id"], cmd)`.

## Pull-request check: `runlab verdict`

`runlab verdict` turns a comparison into a check on each pull request: the difference, its
95 % CI and the verdict, passing or failing on what you expected of the change. On GitHub the
[runlab-action](https://github.com/omer-nachman/runlab-action) runs it in about 5 lines of
workflow YAML.

1. Commit the spec to your default branch as `.runlab/verdict.yml`:

   ```yaml
   paths: ["train/**", "configs/**"]   # other changes need no verdict
   project: my-model
   checks:
     - a: "pr-{pr}-{sha7}"             # this pull request's seeds
       b: "base-{pr}-{sha7}"           # the base branch's seeds, from the same job
       metric: val_acc
       expect: better
   ```

2. In the job, train both arms' seeds (each prints `RESULT:` lines with `seed=`), then:

   ```bash
   pip install "runlab-client[yaml]"
   runlab verdict --post \
     --results "out/pr/*.txt --arm pr-{pr}-{sha7}" \
     --results "out/base/*.txt --arm base-{pr}-{sha7}"
   ```

Keyless: give the job `permissions: id-token: write` and set no `RUNLAB_TOKEN`. The client
then signs in with the short-lived OIDC token GitHub gives the job, minted for your RunLab URL
only, and RunLab accepts it as the RunLab user bound to the repository owner's GitHub account
(the one `runlab login` signed in), for the calls `runlab verdict` makes only, at most at role
`agent`. Nothing to store, leak or rotate. An organization's repository sets `RUNLAB_TOKEN`
from a secret instead.

It records the `RESULT:` lines of each file (other lines are skipped; `--arm` renames their
experiment, `--project` picks the project when the spec has several), compares each row and
writes the table to the job summary; `--post` also writes a check run named `runlab verdict`
and one pull-request comment, updated on each push. `{pr}`, `{sha}`, `{sha7}` and `{branch}`
are filled in: with `{sha7}` every push gets its own arms, so each pair is compared once (a
second look at a pair warns `peeking`).

| Spec key | Default | Meaning |
|---|---|---|
| `paths` | every path | globs: `*` within a folder, `**` across folders, `dir/` everything under it. A pull request that changes none gets a neutral check and records nothing. |
| `project` | none | the project of every row that names none |
| `a`, `b` | required | the change's arm and the baseline's |
| `metric` | required | the metric to test |
| `expect` | `better` | `better` passes on `a_better`; `not_worse` also on `equivalent` or `non_inferior` and needs `margin`; `different` on `a_better` or `b_better` |
| `margin` | none | the smallest difference that matters, in the metric's units |
| `better` | `auto` | `higher`, `lower`, or `auto` from the metric's name |
| `alpha` | 0.05 | the test's level; the interval is 1 − alpha (95 %) |
| `missing` | `failure` | the row's result when an arm has no results yet: `failure` or `neutral` |

What keeps the check honest:

- The spec is read from the pull request's **base** commit, so a pull request cannot loosen
  the test it is judged by. One that adds or changes the spec is judged by the old one, or
  gets a neutral check when there was none.
- `refused` always fails, naming the check that refused and why; `insufficient` (fewer than
  2 seeds in an arm) fails too, whatever the row expects.
- An unknown key in the spec is an error, so a typo never turns a check off.

Limits: the pull request's own job runs the seeds, so the check is only as trustworthy as
that job. Pull requests from forks get no token or secrets in GitHub Actions, so they get no
verdict.

| Exit status | Meaning |
|---|---|
| 0 | every row met its `expect`, or no verdict was needed |
| 4 | a row failed |
| 2 | the spec, a results file, RunLab or GitHub stopped the check |

Without `--post` it only prints the table and writes the job summary. `--spec-from head` reads
the checked-out spec instead, to try one out. The spec may be JSON when PyYAML is not
installed.

## Errors

Every failure raises `RunLabError` with:

- `.status`: the HTTP status, or 0 when RunLab cannot be reached;
- `.message`: RunLab's `error` text;
- `.request_id`: quote it when reporting a problem.

| Status | Meaning |
|---|---|
| 400 | Bad input or a refused action. The message says what to fix; an unknown arm, metric or project names the closest match. |
| 401 | Missing or wrong token; for a keyless GitHub Actions job the message says why (e.g. the repository's owner has not signed in to RunLab). |
| 403 | This key may not do this; the message says why. |
| 404 | No such verdict or study. |
| 413 | Body over 64 KiB; send results in several calls. |

The client retries 3 times on connection errors and on 429, 502, 503 and 504, waiting what the
`Retry-After` header asks (at most 60 s), else 0.5, 1 and 2 s. It retries reads and the writes
RunLab stores only once (`plan`, `record` / `result`, `compare`, `work`), so a dropped
connection at the end of a training job does not lose its results. Starting a study is never
retried: a repeat would start a second one.

## Client reference

`RunLab(url=None, token=None, *, timeout=30, retries=3, backoff_s=0.5)`. Every method returns
the decoded JSON of one operation in
[openapi.json](https://github.com/omer-nachman/runlab-python/blob/main/openapi.json).
With no arguments it uses your sign-in; in CI, set `RUNLAB_TOKEN` instead, or, in a GitHub
Actions job with `id-token: write`, nothing (keyless, for `runlab verdict`).

| Method | Operation | Returns |
|---|---|---|
| `me()` | `getMe` | `name`, `role` |
| `plan(project, a, b, metric, hypothesis, n_max, **options)` | `registerPlan` | `plan`, `power_check`, `text`. Options: `better`, `alpha`, `n_min`, `stopping`, `scale`, `amend`. |
| `record(project, results)` | `recordResults` | `stored`, `already` |
| `result(project, arm, *, seed=None, params=None, **metrics)` | `recordResults` | one line per metric, in one call |
| `compare(project, a, b, metric, **options)` | `compareArms` | `verdict`, `diff`, `ci`, `p`, `test`, `n`, `prereg`, `warnings`, `text`. Options: `better`, `alpha`, `margin`. |
| `verdicts(project="", limit=50, recheck=False)` | `listVerdicts` | `verdicts`, `stale`, and `recheck` when asked |
| `verdict(id)` | `getVerdict` | `seeds`, `check`, `now` |
| `report(project, *, arm="", metric="", q=0.05)` | `getResearchReport` | `claims`, the counts per status, `text` |
| `start_study(project, command, a, b, metric, max_usd, *, params=None, arm_params=None)` | `startStudy` | the study |
| `study(id)`, `studies(project="", limit=50)`, `wait_study(id, timeout, poll_s)` | `getStudy`, `listStudies` | the study, or a list of studies |
| `start_worker_study(project, a, b, metric, max_usd, usd_per_hour, *, params=None, arm_params=None)` | `startStudy` | the study (`mode: worker`) |
| `work(id, *, seed=None, results=None, failed=None, wall_s=None, worker=None, lease_s=None)` | `workOnStudy` | `study`, `pair` (`seed`, `arms`, `params`) or null, `retry_after_s` |
| `run_worker(id, cmd, *, worker=None, cwd=None, timeout=None, lease_s=None, log=None)` | `workOnStudy` | the ended study, after running its pairs here |
| `result_line(arm, metric, value, *, seed=None, **params)` | none | one `RESULT:` line, for `record()` |
| `login(url=None, *, browser=True, host=None, show=None)` | none | `user`, `role`, `url`, after you approve; saves the key |
| `saved_login()`, `config_dir()` | none | `(url, key)` saved here, and where it is kept |

| Command | What it does |
|---|---|
| `runlab demo [--project P] [--url URL] [--no-browser]` | Sign in if needed, then plan, record 10 simulated seeds per arm, compare and report, in project `runlab-demo`. |
| `runlab login [--url URL] [--no-browser]` | Sign in with GitHub. |
| `runlab whoami [--url URL]` | Who you are signed in as. |
| `runlab logout` | Sign out. |
| `runlab worker ...` | Run a study's seed pairs here (see above). |
| `runlab verdict [--results "GLOB [--project P] [--arm A]"] [--spec PATH] [--spec-from base\|head] [--post]` | Record RESULT lines, compare the spec's arms, pass or fail as a pull-request check (see above). |
| `runlab --version` | The client's version. |

## Coding agents

[AGENTS.md](https://github.com/omer-nachman/runlab-python/blob/main/AGENTS.md)
is the short version for Claude Code, Codex and other agents.
[llms.txt](https://github.com/omer-nachman/runlab-python/blob/main/llms.txt) indexes this repo for LLM tools.

## Versioning and 1.0

[Semantic versioning](https://semver.org/). In 0.x a minor version (0.3 → 0.4) may break
something, and CHANGELOG.md says what and how to move; a patch (0.3.2 → 0.3.3) never does.
Pin `runlab-client~=0.4.0` to take fixes only. 1.0 comes once all of these hold:

| Criterion | Threshold |
|---|---|
| Breaking API or CLI changes | none for 8 weeks |
| RunLab uptime | ≥ 99.5 % over 30 days |
| Known limits | each one fixed, or flagged in the verdict's `warnings` |

From 1.0 on, a breaking change needs a new major version and the old behaviour keeps working,
with a deprecation warning, for at least 3 months.

## Support

Questions and bug reports: [GitHub Issues](https://github.com/omer-nachman/runlab-python/issues).

## License

MIT ([LICENSE](https://github.com/omer-nachman/runlab-python/blob/main/LICENSE)).
