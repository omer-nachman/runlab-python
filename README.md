# runlab-client

**Did my change actually help?** You record each seed's metric for two arms: your change and
its baseline. RunLab gives back a verdict you can quote, built from:

- a pre-registered plan;
- a paired test across seeds, with a 95 % confidence interval;
- checks that refuse a comparison that would mislead, e.g. arms tuned with unequal budgets.

This repo has the Python client (standard library only), the
[OpenAPI spec](openapi.json) ([rendered](api.html)) and examples. Install it and set your
RunLab URL and API token:

```bash
pip install runlab-client            # Python 3.10+, no dependencies
export RUNLAB_URL=https://runlab.example.com RUNLAB_TOKEN=<your token>
```

## Quickstart

```python
from runlab_client import RunLab

rl = RunLab()                                    # reads RUNLAB_URL and RUNLAB_TOKEN
rl.me()                                          # {'name': 'ada', 'role': 'agent', ...}

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

`train` is your own training run. The numbers above are an illustration. A runnable version
with a simulated `train` is [examples/quickstart.py](examples/quickstart.py), and
[examples/curl.sh](examples/curl.sh) does the same loop over plain HTTP.

## The loop

| Step | Client | HTTP | What RunLab does |
|---|---|---|---|
| 1. Plan | `plan(project, a, b, metric, hypothesis, n_max)` | `POST /api/v1/research/plans` | Stores the plan and its hash. Estimates power from seeds already recorded (`power_check`). |
| 2. Record | `result(project, arm, seed=s, metric=value)` or `record(project, lines)` | `POST /api/v1/research/results` | Stores one row per arm, seed and metric. A line it already has is skipped, so retries are safe. |
| 3. Compare | `compare(project, a, b, metric)` | `POST /api/v1/research/compare` | Runs the paired t-test on a − b over shared seeds (Welch's test if fewer than 2 are shared). Checks the plan and records the verdict. |
| 4. Quote | `report(project)` | `GET /api/v1/research/report` | Says which verdicts a write-up may quote, after the false-discovery-rate correction across every comparison in the project. |
| Audit | `verdicts(project)`, `verdict(id)` | `GET /api/v1/research/verdicts`, `GET /api/v1/verdicts/{id}` | Shows every recorded verdict, each seed's values, and whether it still holds on today's results. |
| Delegate | `start_study(...)`, `wait_study(id)` | `POST /api/v1/research/studies` | Runs the seeds itself under a dollar cap and stops once the evidence decides (see below). |

### Each user sees only their projects

On a RunLab where each user has their own data, a project name is yours alone: your `demo`
and a colleague's `demo` are two projects, never merged, deduplicated or compared with each
other. Lists (`verdicts()`, `studies()`, `report("")`) show only your projects. A verdict or
study id that is not yours answers 404, the same as one that does not exist, and an unknown
project's closest match is one of yours. Project names may not contain `~`.

`a` is the treatment and `b` the baseline. `diff` is the mean of a − b, so it is positive when
a scored higher. Whether higher is better comes from the metric name: lower for names
containing `loss`, `error`, `err`, `perplexity`, `ppl`, `wer`, `cer`, `mse`, `mae`, `rmse`,
`nll`, `latency`, `fid` or `regret`, higher otherwise. Pass `better="higher"` or
`better="lower"` to override.

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
- Project names are case-insensitive.
- Params are optional; keep an arm's params the same on every seed. Each distinct set of
  params counts as one tuned configuration. A compare whose arms differ by more than 1.25x
  in configurations is refused (`refused_by: equal_budget`).

### Verdicts

| `verdict` | Meaning |
|---|---|
| `a_better` / `b_better` | p < alpha (default 0.05) and the difference favours that arm. |
| `no_detectable_difference` | p ≥ alpha. This is not evidence that the arms are equal; for that, pass `margin`. |
| `equivalent` | With `margin`: the 95 % CI lies inside ±margin. |
| `non_inferior` | With `margin`: a is worse than b by less than margin. |
| `insufficient` | An arm has fewer than 2 seeds, so there is no test. |
| `refused` | A check found that any verdict would mislead. `refused_by` names the check and `why` says what to fix. |

Read these fields next to the verdict:

- `prereg.status`:
  - `ok`;
  - `deviation`, with `issues`, e.g. compared before `n_max` seeds, or the plan was registered
    after more than 3 seeds had been seen;
  - `missing`: no plan;
  - `tampered`.
- `warnings`, e.g. `peeking`.
- `report()`, before you quote anything.

### What keeps a verdict honest

| Rule | Why | What you see |
|---|---|---|
| Plan first | Choosing the metric or the number of seeds after seeing results inflates false positives. | `prereg.status` is `deviation` or `missing`. Amending a plan needs a reason, and every version is kept. |
| Look once | `compare` is a fixed-n test. Re-reading its p-value as seeds arrive breaks its alpha. | Each call on grown data is a new "look". From the second look on, the response warns `peeking`, and `report()` marks the pair `not_quotable`. A call on unchanged data is the same look. To stop early validly, use a study. |
| Equal budgets | A tuned arm against an untuned baseline measures the tuning, not the change. | With more than 1.25x the configurations on one arm, the verdict is `refused` (`refused_by: equal_budget`). |
| Many comparisons | At alpha 0.05, about 1 in 20 pairs with no real difference still comes out a "win". | `report()` applies Benjamini-Hochberg across all pairs in the project. Each claim is `quotable`, `quotable_with_caveat` or `not_quotable`, and carries a ready `quote`. |
| Results can go bad | A seed or dataset later found broken undermines verdicts built on it. | `verdicts()` counts verdicts built on results since marked bad in `stale`; `verdicts(recheck=True)` judges each pair again on today's results (`unchanged`, `inputs_changed` or `flipped`). |

## Studies: let RunLab run the seeds

RunLab can also run the seeds for you.
It runs both arms one seed pair at a time and stops as soon as an always-valid sequential test
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
- A study never spends more than `max_usd`.
- `state` is one of:

  | `state` | Meaning |
  |---|---|
  | `running` | Still running seeds. |
  | `decided` | The sequential test stopped. `decision` is `A` or `B` (`winner` names the arm), or `futile` (no detectable difference at this budget) or `equivalent` (`winner` is null). |
  | `undecided` | `n_max` seeds ran without a decision. |
  | `budget` | The compute budget was spent before a decision (your own machine: the next pair would pass `max_usd`). |
  | `killed` | Stopped at the dollar cap. |
  | `failed` | The command failed or printed no result; see `detail`. |
  | `lost` | Stopped reporting long past its deadline. |
  | `refused` | Refused at the start (that call raised a 400); listed for the record. |

### Run the seeds on your own machine

Your GPU box, laptop or cluster node can run the seeds instead: RunLab hands out seed pairs,
decides with the same sequential test, and stops at `n_max` or before the next pair would pass
your dollar cap. Your training command prints `<metric>=<value>`; `{arm}`, `{seed}` and your
params' names are filled in (no shell runs it).

```bash
pip install runlab-client
export RUNLAB_URL=... RUNLAB_TOKEN=...
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

## Errors

Every failure raises `RunLabError` with:

- `.status`: the HTTP status, or 0 when RunLab cannot be reached;
- `.message`: RunLab's `error` text;
- `.request_id`: quote it when reporting a problem.

| Status | Meaning |
|---|---|
| 400 | Bad input or a refused action. The message says what to fix; an unknown arm, metric or project names the closest match. |
| 401 | Missing or wrong token. |
| 403 | Your role cannot do this: writes need `agent` or above. |
| 404 | No such verdict or study. |
| 413 | Body over 64 KiB; send results in several calls. |

The client retries GET requests 3 times with exponential backoff, on connection errors and on
429, 502, 503 and 504. It never retries POST requests. Recording the same result line twice is
harmless anyway.

## Client reference

`RunLab(url=None, token=None, *, timeout=30, retries=3, backoff_s=0.5)`. Every method returns
the decoded JSON of one operation in [openapi.json](openapi.json).

| Method | Operation | Returns |
|---|---|---|
| `me()` | `getMe` | `name`, `role` |
| `plan(project, a, b, metric, hypothesis, n_max, **options)` | `registerPlan` | `plan`, `power_check`, `text`. Options: `better`, `alpha`, `n_min`, `stopping`, `amend`. |
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

## Coding agents

[AGENTS.md](AGENTS.md) is the short version for Claude Code, Codex and other agents.
[llms.txt](llms.txt) indexes this repo for LLM tools.

## License

MIT ([LICENSE](LICENSE)).
