#!/usr/bin/env python3
"""Read-only, exact-revision offline gate. Does not deploy or touch robots."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-revision", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    revision = git("rev-parse", "HEAD")
    if args.expected_revision != revision or git("status", "--porcelain"):
        parser.error("requires exact full HEAD and clean checkout, including untracked files")
    files = git("ls-files").splitlines()
    before = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
              for name in files if (root / name).is_file()}
    run = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=root,
                         stdout=sys.stderr, stderr=sys.stderr, timeout=300)
    unchanged = (revision == git("rev-parse", "HEAD")
                 and not git("status", "--porcelain")
                 and all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                         for name, digest in before.items()))
    passed = run.returncode == 0 and unchanged
    print(json.dumps({"revision": revision, "source_sha256": before,
                      "pytest_exit_code": run.returncode,
                      "sources_unchanged": unchanged, "offline_passed": passed,
                      "live_qualified": False, "speech_quality_qualified": False}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
