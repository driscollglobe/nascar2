#!/usr/bin/env python3
"""Extract the inlined datasets from index.html into data/*.json and emit
template.html for build.py.

index.html carries four inline <script> data blocks:

  window.__LINEUP_DATA__  = {"_meta": ..., "orgSeasons": [...], "driverPool": [...]};
  window.__STREAK_POOL__  = [...];
  window.__LEGEND_POOL__  = [...];
  window.__GRID_FACTS__   = {...};

Each value is strict JSON. This script copies each value span out verbatim
(byte-for-byte, no reserialization) so build.py can splice the files back and
reproduce index.html exactly. __LINEUP_DATA__ is split into its three members
(_meta, orgSeasons, driverPool) at the raw-text level for the same reason.

The emitted template.html is index.html with each value span replaced by a
@@DATA:<name>@@ placeholder. Everything outside the spans is untouched.

Usage: python3 scripts/extract_data.py [index.html]
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "index.html"
DATA_DIR = ROOT / "data"
TEMPLATE = ROOT / "template.html"


def find_value_span(text, assign):
    """Return (start, end) byte offsets of the JSON value that follows the
    given `window.__X__ = ` assignment, ending before the trailing `;`.
    The value is found by balanced-bracket scanning that is string-aware."""
    i = text.index(assign) + len(assign)
    start = i
    depth = 0
    in_str = False
    escaped = False
    while i < len(text):
        c = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return start, i + 1
        i += 1
    raise ValueError("unbalanced value for " + assign)


def member_span(text, obj_start, obj_end, key):
    """Span of the value for a top-level object member, within [obj_start, obj_end)."""
    m = re.compile(r'"%s"\s*:\s*' % re.escape(key)).search(text, obj_start, obj_end)
    if not m:
        raise ValueError("member %s not found" % key)
    i = m.end()
    depth = 0
    in_str = False
    escaped = False
    start = i
    while i < obj_end:
        c = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return start, i + 1
        i += 1
    raise ValueError("unbalanced member " + key)


def main():
    html = SRC.read_text(encoding="utf-8")
    DATA_DIR.mkdir(exist_ok=True)

    spans = []  # (start, end, name) — value spans to replace with placeholders

    # __LINEUP_DATA__: split into its three members.
    ld_start, ld_end = find_value_span(html, "window.__LINEUP_DATA__ = ")
    for key, name in [("_meta", "lineup_meta"), ("orgSeasons", "org_seasons"), ("driverPool", "driver_pool")]:
        s, e = member_span(html, ld_start, ld_end, key)
        spans.append((s, e, name))

    for assign, name in [
        ("window.__STREAK_POOL__ = ", "streak_pool"),
        ("window.__LEGEND_POOL__ = ", "legend_pool"),
        ("window.__GRID_FACTS__ = ", "grid_facts"),
    ]:
        s, e = find_value_span(html, assign)
        spans.append((s, e, name))

    spans.sort()
    counts = {}
    out = []
    prev = 0
    for s, e, name in spans:
        raw = html[s:e]
        parsed = json.loads(raw)  # validate strict JSON
        counts[name] = len(parsed) if isinstance(parsed, list) else parsed
        (DATA_DIR / (name + ".json")).write_text(raw, encoding="utf-8")
        out.append(html[prev:s])
        out.append("@@DATA:" + name + "@@")
        prev = e
    out.append(html[prev:])
    TEMPLATE.write_text("".join(out), encoding="utf-8")

    # Report.
    ver = re.search(r"window\.__LINEUP_DATA_VERSION__\s*=\s*(\d+)", html)
    print("DATA_VERSION:", ver.group(1) if ver else "not found")
    for name in ["lineup_meta", "org_seasons", "driver_pool", "streak_pool", "legend_pool", "grid_facts"]:
        v = counts[name]
        if isinstance(v, int):
            print("%s.json: %d records" % (name, v))
        elif name == "grid_facts":
            print("grid_facts.json: %d drivers, %d orgs, %d tracks" % (len(v["drivers"]), len(v["orgs"]), len(v["tracks"])))
        else:
            print("%s.json: object with keys %s" % (name, ", ".join(v.keys())))
    print("template.html written (%d placeholders)" % len(spans))


if __name__ == "__main__":
    main()
