"""Run each skill's tests (tests/*/) in its own pytest subprocess."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main() -> int:
    skills = sorted(p for p in HERE.iterdir() if p.is_dir() and not p.name.startswith((".", "__")))
    results = {}
    for d in skills:
        print(f"== {d.name} ==", flush=True)
        proc = subprocess.run([sys.executable, "-m", "pytest", str(d), "-q"], cwd=ROOT)
        results[d.name] = proc.returncode
    print("\nSummary")
    for name, code in results.items():
        print(f"  {name}: {'PASS' if code == 0 else 'FAIL'}")
    return 1 if any(results.values()) or not results else 0


if __name__ == "__main__":
    sys.exit(main())
