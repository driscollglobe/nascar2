#!/usr/bin/env python3
"""Assemble index.html from template.html plus the data/ JSON files.

template.html is index.html with each inlined dataset value replaced by a
@@DATA:<name>@@ placeholder (see scripts/extract_data.py). Each data/<name>.json
holds the exact byte span for that value, so the assembled output is
byte-identical to the file the template was extracted from as long as the data
files are unchanged. Editing a data file changes only that dataset.

Usage:
  python3 build.py            writes index.html
  python3 build.py --check    assembles to memory and diffs against index.html
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "template.html"
DATA_DIR = ROOT / "data"
OUT = ROOT / "index.html"

PLACEHOLDER = re.compile(r"@@DATA:([a-z_]+)@@")


def assemble():
    html = TEMPLATE.read_text(encoding="utf-8")

    def sub(m):
        name = m.group(1)
        path = DATA_DIR / (name + ".json")
        if not path.exists():
            raise SystemExit("missing data file: " + str(path))
        return path.read_text(encoding="utf-8")

    return PLACEHOLDER.sub(sub, html)


def main():
    out = assemble()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8")
        if out == current:
            print("OK: assembled output is byte-identical to index.html (%d bytes)" % len(out.encode("utf-8")))
        else:
            print("MISMATCH: assembled output differs from index.html")
            sys.exit(1)
    else:
        OUT.write_text(out, encoding="utf-8")
        print("wrote index.html (%d bytes)" % len(out.encode("utf-8")))


if __name__ == "__main__":
    main()
