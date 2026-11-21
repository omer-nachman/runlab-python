# AGENTS.md: using RunLab from a coding agent

Use this when a user asks whether a change (to a model, data, training recipe or prompt)
actually helped. RunLab turns per-seed numbers into a verdict with a 95 % confidence
interval, checked against a plan registered before the seeds ran.

## Setup

- `pip install runlab-client` (Python 3.10+, no dependencies).
- `RUNLAB_URL` and `RUNLAB_TOKEN` must be set; if they are not, ask the user for them.
  Never print, log or commit the token.
- `RunLab().me()` checks access. It returns `{"name": ..., "role": "agent"}`. A 401 means a
  bad token; ask the user, don't retry.

## The loop

1. **Plan before any seed runs.** Pick the arms, the metric and the seed count. Call
   `plan(project, a, b, metric, hypothesis=..., n_max=N, stopping="fixed")`. `a` is the
   change, `b` the baseline. Registering the same plan again is harmless; a changed plan
   for the same pair needs `amend="<reason>"`, and you should tell the user why.
2. **Run seeds 0..N-1 for both arms**, with the same seeds for both. Change nothing else
   between arms; the same params on every seed of an arm. After each run, call
   `result(project, arm, seed=s, <metric>=value)`, or send the training log's
   `RESULT: <arm> <metric>=<value> seed=<n>` lines with `record(project, text)`.
3. **Compare once, after all N seeds**: `v = compare(project, a, b, metric)`.
4. **Report** to the user, quoting `v["text"]`, then:
   - `v["verdict"]`;
   - `v["prereg"]["status"]`;
   - every `v["warnings"]` entry.
5. **Before writing that a change helps** in a paper, PR or summary, call `report(project)`.
   Quote only claims whose `status` is `quotable`, and use their `quote`.

## Rules

- Don't call `compare` while seeds are still coming in. Its p-value is valid for one look.
  A second look on more data warns `peeking` and makes the pair `not_quotable` in
  `report()`. To stop early, use a study:
  plan with `stopping="msprt"`, then call `start_study(...)` and `wait_study(id)`.
  To run the seeds on this machine instead, run `runlab worker --project ... --a ... --b ...
  --metric ... --max-usd ... --usd-per-hour ... --cmd "<train command with {arm} {seed}>"`;
  the command must print `<metric>=<value>`.
- `no_detectable_difference` is not "no effect". Say "no detectable difference at n seeds".
  If the user cares about equality, compare with `margin=<smallest difference that
  matters>`.
- `refused` is not an error to work around. Read `refused_by` and `why`, fix the cause
  (e.g. tune both arms with the same budget), then compare again.
- `insufficient` means an arm has fewer than 2 seeds. Run more seeds.
- On a 400, read the message. It names what is wrong and, for an unknown name, the closest
  match.
- A POST is never retried by the client. Re-sending the same RESULT lines is safe: they are
  counted in `already` and not stored twice.

## Reference

- [README.md](README.md): every method, verdict, field and error.
- [openapi.json](openapi.json): the HTTP contract (10 operations under `/api/v1`).
- [examples/quickstart.py](examples/quickstart.py): the full loop in Python.
- [examples/curl.sh](examples/curl.sh): the loop over HTTP.
