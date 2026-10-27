"""Resolve the bundled sheet-generation helper scripts (recalc.py / aigc_label.py).

The sheet-generation skill lives under a versioned plugin cache directory
(``workbuddy-sheetagent/<version>-wb.<hash>/skills/workbuddy-sheet-generation/``),
so a hard-coded path breaks on every platform upgrade.  This script globs for
the newest matching version and prints an absolute path, so callers never have
to guess.

Usage
-----
    python resolve_sheet_scripts.py recalc        # -> path to recalc.py
    python resolve_sheet_scripts.py aigc_label    # -> path to aigc_label.py
    python resolve_sheet_scripts.py               # -> both, as "name=path" lines
    python resolve_sheet_scripts.py --shell       # -> ready-to-run commands
"""
import argparse
import glob
import os
import re
import sys

PLUGIN_ROOT = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "plugins", "cache",
    "workbuddy-builtin", "workbuddy-sheetagent")
SCRIPT_NAMES = ("recalc.py", "aigc_label.py")


def version_key(path):
    """Sort key: prefer the highest version, then the newest mtime."""
    m = re.search(r"(\d+\.\d+\.\d+)", path)
    ver = tuple(int(x) for x in m.group(1).split(".")) if m else (0, 0, 0)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    return (ver, mtime)


def find_all():
    """Return {script_name: [paths...]} for every installed version, newest first."""
    found = {}
    for name in SCRIPT_NAMES:
        pat = os.path.join(PLUGIN_ROOT, "*", "skills", "workbuddy-sheet-generation", "scripts", name)
        paths = glob.glob(pat)
        found[name] = sorted(paths, key=version_key, reverse=True)
    return found


def main():
    ap = argparse.ArgumentParser(description="Resolve sheet-generation helper script paths")
    ap.add_argument("name", nargs="?", choices=list(SCRIPT_NAMES), help="which script to resolve")
    ap.add_argument("--shell", action="store_true", help="print runnable commands")
    args = ap.parse_args()

    found = find_all()
    if not any(found.values()):
        print("ERROR: sheet-generation scripts not found under %s" % PLUGIN_ROOT, file=sys.stderr)
        print("Use the workbuddy-sheet-generation skill instead of calling these directly.",
              file=sys.stderr)
        return 2

    def best(name):
        return found[name][0] if found[name] else None

    if args.name:
        p = best(args.name)
        if not p:
            print("ERROR: %s not found" % args.name, file=sys.stderr)
            return 2
        print(p)
        return 0

    if args.shell:
        rec = best("recalc.py")
        lbl = best("aigc_label.py")
        if rec:
            print('python "%s" index.xlsx 60' % rec)
        if lbl:
            print('python "%s" index.xlsx --platform WORKBUDDY' % lbl)
        return 0

    for name in SCRIPT_NAMES:
        p = best(name)
        if p:
            print("%s=%s" % (name, p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
