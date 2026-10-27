"""Single source of truth for the verification report.

Both the batch path and the single-video path in download_webinar.py write the
same report file, and both must MERGE into it (never overwrite -- a reduced
second run must not wipe earlier entries that the Excel step relies on).

Having two copies of that merge logic is how "stale failure rows survived a
successful re-download" and "counters drifted from len(videos)" got in, so the
logic lives here exactly once.

Report schema
-------------
{
  "videos":   { <key>: {"ok": bool, "file": str|None, "report": {...}} },
  "failures": [ {"slug": str, "error": str, ...} ],
  "ok": int, "skipped": int, "failed": int
}

``videos`` is authoritative: build_index_xlsx.py decides "downloaded" purely
from ``videos[key].ok``. The top-level counters are informational and are
recomputed from ``videos``/``failures`` on every merge so they cannot drift.
"""
import json
import os


def load(path):
    """Read an existing report. Returns {} when absent or unreadable."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge(prev, videos=None, failures=None, skipped=0):
    """Merge this run's results into a previous report.

    videos:   {key: entry} entries produced by THIS run -- they win over
              previous entries for the same key.
    failures: [{slug, error, ...}] failures produced by THIS run.
    skipped:  number of entries skipped by THIS run.

    A previous failure row for a slug that this run fixed is DROPPED, so the
    report never says "videos[x].ok == True" and "failures contains x" at once.

    NOTE on alias keys: one video may be stored under BOTH its slug and its
    video id (a single-video run has no slug).  Counters therefore count
    DISTINCT ENTITIES, not raw keys -- otherwise one video would be counted
    twice.
    """
    videos = videos or {}
    failures = list(failures or [])

    merged_videos = dict(prev.get("videos") or {})
    merged_videos.update(videos)

    # Every slug this run covered supersedes the previous outcome for that
    # slug -- so a previous failure row for it is stale and must go. Otherwise
    # the report would say "videos[x].ok == True" AND "failures contains x".
    # Alias keys (slug + video id) for the SAME video are collapsed: the
    # failure is attributed to the slug, so the video id row disappears too.
    covered = set(videos.keys())
    # One key per video is written by the callers, so a key IS the identity.

    def canon(slug):
        return str(slug)

    live = [f for f in failures if canon(f.get("slug")) not in covered]
    prev_failures = [f for f in (prev.get("failures") or [])
                     if canon(f.get("slug")) not in covered]

    all_failures = prev_failures + live

    # Count DISTINCT KEYS -- one key per video, so this is exact.
    ok_count = sum(1 for e in merged_videos.values() if e.get("ok"))

    reported = {canon(f.get("slug")) for f in all_failures}
    for k, e in merged_videos.items():
        if not e.get("ok") and canon(k) not in reported:
            all_failures.append({"slug": k, "error": "verification failed"})

    fail_entities = {canon(f.get("slug")) for f in all_failures}

    return {
        "videos": merged_videos,
        "failures": all_failures,
        "ok": ok_count,
        "skipped": int(prev.get("skipped") or 0) + int(skipped or 0),
        "failed": len(fail_entities),
    }


def write(path, payload):
    """Persist a merged report."""
    if not path:
        return payload
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload


def update(path, videos=None, failures=None, skipped=0):
    """Load -> merge -> write. The only entry point callers should use."""
    prev = load(path)
    payload = merge(prev, videos, failures, skipped=skipped)
    return write(path, payload)
