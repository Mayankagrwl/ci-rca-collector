"""Fail if any golden file contains an unredacted secret (eval-spec §11.19).

Reuses ``redact.redact_text`` — it never re-lists secret patterns. Scans every
file under the goldens dir; a file whose text changes under redaction still
carries a secret and fails the check. No network.

    python -m tools.eval.check_goldens_redacted [--goldens tools/eval/goldens]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools.rca.redact import REPLACEMENT, redact_text

_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".zip", ".gz", ".pdf"}


def scan(goldens_dir: Path) -> list[tuple[Path, str]]:
    """Return (file, offending snippet) for every golden file with a secret."""
    offenders: list[tuple[Path, str]] = []
    for path in sorted(goldens_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        redacted, count = redact_text(text)
        if count > 0 and redacted != text:
            snippet = _first_change(text, redacted)
            offenders.append((path, snippet))
    return offenders


def _first_change(original: str, redacted: str) -> str:
    """A short context window around the first redacted span (already scrubbed)."""
    idx = redacted.find(REPLACEMENT)
    if idx < 0:
        return "secret pattern matched"
    start = max(0, idx - 30)
    return redacted[start : idx + len(REPLACEMENT) + 10].replace("\n", " ").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.eval.check_goldens_redacted")
    parser.add_argument("--goldens", default="tools/eval/goldens")
    args = parser.parse_args(argv)

    goldens_dir = Path(args.goldens)
    if not goldens_dir.exists():
        sys.stderr.write(f"goldens dir not found: {goldens_dir}\n")
        return 2
    offenders = scan(goldens_dir)
    if offenders:
        for path, snippet in offenders:
            sys.stderr.write(f"UNREDACTED SECRET in {path}: …{snippet}…\n")
        sys.stderr.write(f"{len(offenders)} golden file(s) contain a secret pattern.\n")
        return 1
    print(f"goldens clean: no secret patterns under {goldens_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
