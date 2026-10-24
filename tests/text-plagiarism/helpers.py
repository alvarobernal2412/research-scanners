"""Shared test helpers: in-process CLI calls and a driver that plays the approving user."""
import contextlib
import io
import json


def call_main(main, argv, **kw):
    """Run a CLI main() in-process; returns (exit code, parsed JSON stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv, **kw)
    out = buf.getvalue().strip()
    return code, (json.loads(out) if out else None)


def drive_strict(workdir, opener, max_rounds=12):
    """Plan and run strict rounds until done."""
    import strict

    rounds = []
    for _ in range(max_rounds):
        code, plan = call_main(strict.main, ["plan", "--workdir", str(workdir)])
        assert code == 0, plan
        if plan["done"]:
            return rounds
        code, res = call_main(strict.main, ["run", "--workdir", str(workdir), "--plan", plan["plan"],
                                            "--sha256", plan["sha256"]], opener=opener)
        assert code == 0, res
        rounds.append((plan["round"], res))
    raise AssertionError("strict rounds did not finish")


def write_md(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p
