"""Did the change help? Plan, record 10 seeds per arm, get the verdict, check it is quotable.

    pip install runlab-client     # or: pip install "git+https://github.com/omer-nachman/runlab-python"
    export RUNLAB_URL=https://<your-server> RUNLAB_TOKEN=<your-token>
    python quickstart.py [project]

`train` is a stand-in: replace it with your training run, returning the metric for one seed.
"""

import random
import sys

from runlab_client import RunLab, RunLabError

PROJECT = sys.argv[1] if len(sys.argv) > 1 else "quickstart"
SEEDS = 10


def train(arm: str, seed: int) -> float:
    """Stand-in for one training run: the change ("treatment") adds +0.02 accuracy."""
    rng = random.Random(f"{arm}-{seed}")
    return 0.80 + rng.gauss(0, 0.01) + (0.02 if arm == "treatment" else 0.0)


rl = RunLab()                                  # reads RUNLAB_URL and RUNLAB_TOKEN
try:
    # 1. Pre-register before any seed runs: arms, metric, seeds, one fixed-n test at the end.
    #    Re-running this script is fine; a changed plan for the same pair needs amend="<why>".
    plan = rl.plan(PROJECT, "treatment", "baseline", "acc",
                   hypothesis="the change raises accuracy", n_max=SEEDS, stopping="fixed")
    print(plan["text"], end="\n\n")
    # 2. Record each seed's metric for both arms (same seeds for both: a paired test).
    for seed in range(SEEDS):
        for arm in ("treatment", "baseline"):
            rl.result(PROJECT, arm, acc=train(arm, seed), seed=seed)
    # 3. Compare once, after all planned seeds ran.
    v = rl.compare(PROJECT, "treatment", "baseline", "acc")
    # 4. May a write-up quote it? (false discovery rate across every pair of the project)
    claims = rl.report(PROJECT, arm="treatment", metric="acc")["claims"]
except RunLabError as exc:                     # HTTP status, the server's message, request id
    sys.exit(f"RunLab: {exc}")

print(v["text"])
print("verdict:", v["verdict"], "| preregistration:", v.get("prereg", {}).get("status"))
for w in v.get("warnings", []):
    print("warning:", w["kind"], "-", w["message"])
for c in claims:
    print(f"{c['status']}: {c['quote']}")
