#!/usr/bin/env python
"""Backfill video ids from LISTING-page embeds into an existing manifest.

Why this exists
---------------
`harvest_events.py` reads video embeds from each event's DETAIL page.  Some
sites render the recording only on the listing page (e.g. CDISC's
/events/webinars/public) while the detail page is access-gated and contains
no iframe at all -- those events end up as `has_video: false` even though the
recording is publicly playable from the listing.

This script parses the listing page instead: it walks embeds in document
order and pairs each one with the *next* detail link that follows it (the
front-end prints `recording iframe` then `title link` inside the same card).

Usage
-----
    python scripts/harvest_listing_embeds.py \
        --listing "https://www.cdisc.org/events/webinars/public" \
        --manifest manifest.json --dry-run

    # then drop --dry-run to patch manifest.json in place (.bak written once)

Proxy: --proxy accepts http:// or socks5:// exactly like harvest_events.py.
"""
import argparse
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harvest_events import fetch  # noqa: E402  (reuses the proxy transports)
import probe_network  # noqa: E402

EMBED_RES = [
    ("vimeo", re.compile(r"player\.vimeo\.com/video/(\d+)")),
    ("vimeo", re.compile(r"vimeo\.com/(?:channels/[^/]+/|groups/[^/]+/videos/)?(\d{6,})")),
    ("youtube", re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=)|youtu\.be/)([\w-]{11})")),
]

# An embed and its caption/title link are printed in the same card, so the
# link shows up shortly after the iframe.  Generous window: cards carry long
# asset URLs and responsive wrappers.
PAIR_WINDOW = 1500
LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def find_embed(html, start, end):
    """First embed inside html[start:end] -> (kind, vid) or (None, None)."""
    best = None
    for kind, rx in EMBED_RES:
        m = rx.search(html, start, end)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), kind, m.group(1))
    return (best[1], best[2]) if best else (None, None)


def pairs_from_listing(html):
    """[(detail_href, title, kind, vid)] in document order."""
    rows, seen = [], set()
    for kind, rx in EMBED_RES:
        for m in rx.finditer(html):
            if m.start() in seen:
                continue
            seen.add(m.start())
            window = html[m.end(): m.end() + PAIR_WINDOW]
            hit = LINK_RE.search(window)
            if not hit:
                continue
            href, text = hit.group(1), strip_tags(hit.group(2))
            if not text or href.startswith("#") or "javascript:" in href:
                continue
            rows.append((m.start(), href, text, kind, m.group(1)))
    rows.sort(key=lambda r: r[0])
    return [(r[1], r[2], r[3], r[4]) for r in rows]


def norm(slug_or_url):
    s = slug_or_url.strip().rstrip("/")
    return s.rsplit("/", 1)[-1].lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listing", required=True, help="listing page that embeds the recordings")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--proxy", default=None,
                    help="'auto' = probe the network and pick the right transport; "
                         "or http://127.0.0.1:39411 or socks5://127.0.0.1:7890")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.proxy == "auto":
        args.proxy = probe_network.auto_proxy_arg([args.listing])

    html = fetch(args.listing, proxy=args.proxy)
    if not html:
        print("ERROR: listing page fetch failed: %s" % args.listing)
        return 2
    rows = pairs_from_listing(html)
    print("listing embeds paired: %d" % len(rows))

    by_slug = {}
    for href, title, kind, vid in rows:
        by_slug.setdefault(norm(href), (title, kind, vid))

    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    events = manifest["events"] if isinstance(manifest, dict) else manifest

    patched, still = 0, []
    for ev in events:
        if ev.get("vimeo_id") or ev.get("video_url"):
            continue
        hit = by_slug.get(norm(ev.get("slug") or ev.get("url") or ""))
        if not hit:
            still.append(ev.get("slug"))
            continue
        title, kind, vid = hit
        ev["vimeo_id" if kind == "vimeo" else "youtube_id"] = vid
        ev["video_url"] = ("https://player.vimeo.com/video/%s" % vid) if kind == "vimeo" \
            else ("https://www.youtube.com/watch?v=%s" % vid)
        ev["has_video"] = True
        ev["video_source"] = "listing"
        ev["embed_title"] = title
        patched += 1
        print("  + %-58s %s=%s" % ((ev.get("slug") or "")[:58], kind, vid))

    print("\npatched=%d  no_embed_on_listing=%d" % (patched, len(still)))
    if still:
        print("still without video: " + ", ".join(s for s in still if s)[:400])

    if args.dry_run:
        print("\n[dry-run] manifest untouched")
        return 0
    if not patched:
        print("nothing to write")
        return 0

    if not os.path.exists(args.manifest + ".bak"):
        shutil.copy2(args.manifest, args.manifest + ".bak")
        print("backup -> %s.bak" % args.manifest)
    with open(args.manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print("manifest written: %s" % args.manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
