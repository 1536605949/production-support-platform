#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Idempotently splice the ``/support/`` location block into a shared edge nginx.

Why a script instead of a documented manual edit:

* The edge nginx belongs to *another* project (``novel-agents``). Editing its
  config by hand is a change with a blast radius beyond this repository, so it
  has to be reviewable, repeatable, and reversible.
* ``nginx`` resolves ``proxy_pass`` upstreams **once, at config-load time**. If
  the ``support-caddy`` container is not up when the edge reloads, nginx refuses
  to start and takes the *other* project down with it. This script therefore
  always runs ``nginx -t`` before reloading, and restores the backup on failure.

Behaviour:

* Already patched  -> prints ``already-patched`` and exits 0 (safe to re-run).
* Anchor missing   -> exits 3 without touching the file.
* Validation fails -> restores the backup, exits 4.

Usage::

    python3 inject_edge_nginx.py <nginx.conf> <snippet.conf>

Run inside the nginx container (so ``nginx -t`` sees the real config), or on the
host with ``--no-reload`` for a dry run.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

BEGIN = "# >>> support-platform (managed block — do not edit by hand) >>>"
END = "# <<< support-platform (managed block) <<<"

# The edge config's 443 server block ends with a catch-all ``location /``. Our
# prefixes (``= /support`` and ``/support/``) are more specific, so nginx picks
# them first regardless of textual order — but inserting *before* the catch-all
# keeps the file readable.
ANCHORS = ("\n    location / {\n", "\n\tlocation / {\n", "\nlocation / {\n")


def build_block(snippet: str) -> str:
    body = snippet.rstrip("\n")
    # Re-indent the snippet to live inside the 443 server block.
    lines = [ln for ln in body.split("\n")]
    indented = "\n".join(("\t" + ln) if ln.strip() else ln for ln in lines)
    return f"{BEGIN}\n{indented}\n{END}\n"


def find_anchor(text: str) -> tuple[int, str] | None:
    """Return (index, anchor) of the LAST catch-all location, or None."""
    best = None
    for anchor in ANCHORS:
        idx = text.rfind(anchor)
        if idx != -1 and (best is None or idx > best[0]):
            best = (idx, anchor)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("conf")
    ap.add_argument("snippet")
    ap.add_argument("--container", default="novel-nginx",
                    help="container used for `nginx -t` and reload")
    ap.add_argument("--no-reload", action="store_true")
    args = ap.parse_args()

    with open(args.conf, "r", encoding="utf-8") as fh:
        original = fh.read()

    if BEGIN in original:
        print("already-patched")
        return 0

    with open(args.snippet, "r", encoding="utf-8") as fh:
        snippet = fh.read()

    hit = find_anchor(original)
    if hit is None:
        print("ERROR: could not find a `location / {` anchor in " + args.conf,
              file=sys.stderr)
        return 3
    idx, anchor = hit

    # anchor starts with "\n", so idx+1 is the start of the "    location" line.
    patched = original[: idx + 1] + build_block(snippet) + original[idx + 1:]

    backup = args.conf + ".bak-support"
    shutil.copy2(args.conf, backup)
    with open(args.conf, "w", encoding="utf-8") as fh:
        fh.write(patched)
    print(f"patched {args.conf} (backup: {backup})")

    if args.no_reload:
        return 0

    test = subprocess.run(
        ["docker", "exec", args.container, "nginx", "-t"],
        capture_output=True, text=True,
    )
    sys.stderr.write(test.stdout)
    sys.stderr.write(test.stderr)
    if test.returncode != 0:
        shutil.copy2(backup, args.conf)
        print("ERROR: `nginx -t` failed; config restored from backup.",
              file=sys.stderr)
        return 4

    reload_ = subprocess.run(
        ["docker", "exec", args.container, "nginx", "-s", "reload"],
        capture_output=True, text=True,
    )
    sys.stderr.write(reload_.stdout)
    sys.stderr.write(reload_.stderr)
    if reload_.returncode != 0:
        shutil.copy2(backup, args.conf)
        print("ERROR: reload failed; config restored from backup.", file=sys.stderr)
        return 5

    print("reloaded " + args.container)
    return 0


if __name__ == "__main__":
    sys.exit(main())
