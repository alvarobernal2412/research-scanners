"""Run each skill's tests (tests/*/) in its own pytest subprocess."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main() -> int:
    skills = sorted(p for p in HERE.iterdir() if p.is_dir() and not p.name.startswith((".", "__")))
    results = {}
    no_tests = set()
    for d in skills:
        print(f"== {d.name} ==", flush=True)
        proc = subprocess.run([sys.executable, "-m", "pytest", str(d), "-q"], cwd=ROOT)
        # pytest exits with 5 when a directory has no tests yet (a scaffold): not a failure
        results[d.name] = 0 if proc.returncode in (0, 5) else proc.returncode
        if proc.returncode == 5:
            no_tests.add(d.name)
            print(f"(no tests yet in {d.name})", flush=True)
    print("\nSummary")
    for name, code in results.items():
        label = "NO TESTS YET" if name in no_tests else ("PASS" if code == 0 else "FAIL")
        print(f"  {name}: {label}")
    return 1 if any(results.values()) or not results else 0


if __name__ == "__main__":
    sys.exit(main())
