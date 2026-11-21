"""`python -m runlab_client login | whoami | logout | worker ...`: the `runlab` command, for
when neither `runlab` nor `runlab-client` is on PATH."""

from runlab_client import main

raise SystemExit(main(prog="python -m runlab_client"))
