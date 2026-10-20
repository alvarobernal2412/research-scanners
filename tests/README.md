# Tests

Tests live here, outside `skills/<name>/`, so installing a skill does not copy them.

Skills have scripts with the same module names (`scan.py`, `extract.py`, ...) and each skill's tests put its own `scripts/` on `sys.path`. Run each skill's tests in its own process, never one pytest session over all skills:

    pip install -r requirements-dev.txt
    python -m pytest tests/<skill> -q

`python tests/run_all.py` does this for every `tests/*/` directory (one subprocess each), prints a per-skill pass/fail summary, and exits non-zero if any skill fails.
