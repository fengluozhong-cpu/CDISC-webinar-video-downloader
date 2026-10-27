"""Harvest a webinar / event listing into a structured JSON manifest.

Works on most Drupal/WordPress-style event sites: given a listing page it
follows event links, fetches each detail page, and extracts title / date /
presenters / video embed / attachments / description.

Video detection is platform-agnostic (Vimeo, YouTube, Wistia, native <video>,
HLS .m3u8) so it generalises beyond one site.

Usage
-----
    python harvest_events.py --listing "https://site.org/events/webinars/public" \\
        --base "https://site.org" --out manifest.json --pages-dir pages

    # keep already-fetched pages (resume an interrupted run)
    python harvest_events.py --listing ... --out manifest.json --pages-dir pages --reuse
"""
import argparse
import concurrent.futures as cf
import html
import json
import os
import re
import sys
import time
import urllib.parse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0 Safari/537.36"

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def fetch(url, timeout=45, retries=2):
    import urllib.request
    last = None
    for attempt in range(retries + 1):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with opener.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("fetch failed: %s" % last)


def strip_tags(s):
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


SKIP_SLUGS = {
    "webinars", "webinar", "events", "event", "upcoming-webinars", "partner-events",
    "past-webinars", "archive", "archives", "calendar", "upcoming", "past",
    # hub pages that also expose a video iframe. Matched EXACTLY so that dated
    # variants (e.g. 2025-ai-innovation-challenge) are NOT caught.
    "ai-innovation-challenge", "webinars-public", "upcoming-webinars-public",
}

# hub / landing pages: no descriptive slug, or a title that is a bare category name
HUB_TITLE_RE = re.compile(
    r"^(webinars?|events?|partner events?|upcoming (webinars?|events?)|past (webinars?|events?)"
    r"|calendar|home|all (webinars?|events?))$",
    re.I,
)


def is_hub_page(title, slug, iso, vids, desc):
    """True when the page is a category/hub rather than a single event."""
    if HUB_TITLE_RE.match((title or "").strip()):
        return True
    if iso:
        return False
    # no date + no video + short body -> not a real event record
    if not vids and len(desc or "") < 200:
        return True
    return False


def extract_links(listing_html, base):
    """Find detail-page links in a listing page.

    Parse every <a href="..."> properly (attributes may follow href, and the
    same path can appear in a video caption before the title link), and keep
    only paths that look like an event detail page.
    """
    found = set()
    for m in re.finditer(r'<a\s[^>]*href="([^"]+)"', listing_html, re.I):
        u = m.group(1).strip()
        if not u or u.startswith(("#", "mailto:", "javascript:")):
            continue
        full = urllib.parse.urljoin(base, u).split("#")[0]
        if not re.search(r"/webinar|/event", full, re.I):
            continue
        # keep only webinar-ish event types; sibling event families
        # (interchange / conference / workshop) are a different listing.
        if not re.search(r"/events?/webinar|/webinars?/", full, re.I):
            continue
        # skip obvious hub/category pages
        if re.search(r"/events?/?$|/events?/(public|private|member|upcoming|archived|past)/?$",
                     full, re.I):
            continue
        # the listing page itself (e.g. /events/webinars/public) is not an event
        if re.search(r"/webinars?/(public|private|member|upcoming|archived|past)/?$",
                     full, re.I):
            continue
        slug = full.rstrip("/").split("/")[-1].lower()
        if not slug or slug in SKIP_SLUGS:
            continue
        # A bare year (2026) or year+single-word (2026-cosis) is a calendar hub.
        # Keep dated slugs that carry a real title (2025-ai-innovation-challenge).
        if re.fullmatch(r"(19|20)\d{2}", slug) or \
           re.fullmatch(r"(19|20)\d{2}-[a-z0-9]+", slug):
            continue
        found.add(full)
    return sorted(found)


VIDEO_PATTERNS = {
    "vimeo": [r"player\.vimeo\.com/video/(\d+)",
              r"vimeo\.com/(?:channels/[^/]+/|groups/[^/]+/videos/)?(\d{6,})"],
    "youtube": [r"youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]{6,})",
                r"youtu\.be/([A-Za-z0-9_-]{6,})"],
    "wistia": [r"fast\.wistia\.net/embed/iframe/([A-Za-z0-9]+)"],
    "m3u8": [r'["\'](https?://[^"\'\s]+\.m3u8[^"\'\s]*)["\']'],
    "mp4": [r'["\'](https?://[^"\'\s]+\.mp4[^"\'\s]*)["\']'],
}


def detect_videos(raw):
    """Return list of {platform, id|url} found in the page."""
    out = []
    for plat, pats in VIDEO_PATTERNS.items():
        for p in pats:
            m = re.search(p, raw)
            if m:
                out.append({"platform": plat, "id": m.group(1)})
                break
    # native <video src=...>
    for m in re.finditer(r"<video[^>]+src=[\"']([^\"']+)[\"']", raw, re.I):
        out.append({"platform": "file", "id": html.unescape(m.group(1))})
    # dedupe, keep order
    seen, uniq = set(), []
    for v in out:
        k = (v["platform"], v["id"])
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    return uniq


def parse_date(text):
    """Return ISO date + raw string from a date-ish blob."""
    m = re.search(r"(\d{1,2})\s+([A-Z][a-z]{2,8})\.?\s+(\d{4})", text)
    if m:
        d, mon, y = int(m.group(1)), m.group(2)[:3], int(m.group(3))
        for full, num in MONTHS.items():
            if full[:3].lower() == mon.lower():
                tm = re.search(r"(\d{1,2}:\d{2}\s*(?:am|pm)?)", text, re.I)
                return ("%04d-%02d-%02d" % (y, num, d),
                        tm.group(1).strip() if tm else "")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0), ""
    return "", ""


def extract_presenters(raw):
    """Pull names from a Presenters / Speakers section."""
    people = []
    for pat in (r"<strong>\s*(?:Presenters?|Speakers?|Faculty)\s*</strong>\s*(?:<br\s*/?>)?\s*<ul>(.*?)</ul>",
                r"<strong>\s*(?:Presenters?|Speakers?)\s*</strong>(.*?)(?:<strong>Language|</ul>)"):
        for m in re.finditer(pat, raw, re.S | re.I):
            chunk = m.group(1)
            if "<ul" in pat:
                names = [strip_tags(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", chunk, re.S)]
            else:
                names = [strip_tags(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", chunk, re.S)]
            for n in names:
                n = n.strip(" ,;")
                if 2 < len(n) < 140:
                    people.append(n)
            if people:
                break
    return list(dict.fromkeys(people))


def extract_attachments(raw, base):
    files = []
    for m in re.finditer(r'href="([^"]+\.(?:pdf|pptx?|xlsx?|docx?|zip|mp4|epub))"[^>]*>(.*?)</a>',
                         raw, re.S | re.I):
        url = urllib.parse.urljoin(base, html.unescape(m.group(1)))
        label = strip_tags(m.group(2)) or os.path.basename(url)
        files.append({"name": label, "url": url})
    return files


def extract_description(raw):
    for pat in (r'field--name-body[^"]*"(.*?)</div>', r'field--name-field-body[^"]*"(.*?)</div>'):
        m = re.search(pat, raw, re.S)
        if m:
            txt = strip_tags(m.group(1))
            txt = re.sub(r"^field--type-\S+\s*field--label-hidden\"?\s*", "", txt)
            txt = re.sub(r"^field__item\"?\s*", "", txt)
            if len(txt) > 50:
                return txt[:900]
    m = re.search(r"<main[^>]*>(.*?)</main>", raw, re.S)
    if m:
        return strip_tags(m.group(1))[:900]
    return ""


def process(url, pages_dir, base):
    slug = url.rstrip("/").split("/")[-1] or "index"
    path = os.path.join(pages_dir, slug + ".html")
    os.makedirs(pages_dir, exist_ok=True)
    if not (os.path.exists(path) and os.path.getsize(path) > 500):
        raw = fetch(url)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
    else:
        raw = open(path, encoding="utf-8", errors="ignore").read()

    t = re.search(r"<title>(.*?)</title>", raw, re.S)
    title = html.unescape(t.group(1)).split("|")[0].split("—")[0].strip() if t else slug

    # date: prefer a labelled field, else the first date-like blob.
    # Drupal daterange fields render as <span class="daterange__date">31 August 2023 ...</span>
    # with heavy whitespace -- read that span first, then fall back to the field body.
    date_txt = ""
    iso, time_s = "", ""
    m = re.search(r'class="daterange__date"[^>]*>(.*?)</span>', raw, re.S)
    if m:
        date_txt = strip_tags(m.group(1))
        iso, time_s = parse_date(date_txt)
    if not iso:
        for pat in (r"field--name-field-event-date-timezone(.*?)</div>",
                    r"field--name-field-date(.*?)</div>",
                    r"field--name-field-event-date(.*?)</div>"):
            mm = re.search(pat, raw, re.S)
            if not mm:
                continue
            chunk = mm.group(1)
            attrs = " ".join(re.findall(r'(?:data-from|data-to|datetime)="([^"]+)"', chunk))
            date_txt = (attrs + " " + strip_tags(chunk)).strip()
            iso, time_s = parse_date(date_txt)
            if iso:
                break
    if not iso:
        iso, time_s = parse_date(strip_tags(raw)[:2000])

    vids = detect_videos(raw)
    desc = extract_description(raw)
    iso, time_s = parse_date(date_txt)

    # a hub/category page has no date and no real description -> drop it
    if is_hub_page(title, slug, iso, vids, desc):
        return None

    return {
        "slug": slug,
        "title": title,
        "url": url,
        "date": iso,
        "time": time_s,
        "videos": vids,
        "has_video": bool(vids),
        "presenters": extract_presenters(raw),
        "attachments": extract_attachments(raw, base),
        "description": desc,
    }


def main():
    ap = argparse.ArgumentParser(description="Harvest an event/webinar listing into JSON")
    ap.add_argument("--listing", required=True, help="listing page URL")
    ap.add_argument("--base", default=None, help="site root (default: listing origin)")
    ap.add_argument("--out", default="manifest.json")
    ap.add_argument("--pages-dir", default="pages")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--reuse", action="store_true", help="reuse cached detail pages")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    base = args.base or (urllib.parse.urlparse(args.listing).scheme + "://" +
                         urllib.parse.urlparse(args.listing).netloc)

    print("fetching listing: %s" % args.listing)
    listing = fetch(args.listing)
    links = extract_links(listing, base)
    if args.limit:
        links = links[:args.limit]
    print("found %d detail links" % len(links))
    if not links:
        print("WARNING: no links matched. Inspect the listing HTML and widen the patterns "
              "in extract_links().", file=sys.stderr)

    results = []
    errors = []
    skipped = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, u, args.pages_dir, base): u for u in links}
        for i, f in enumerate(cf.as_completed(futs), 1):
            u = futs[f]
            try:
                row = f.result()
                if row is None:
                    skipped += 1
                else:
                    results.append(row)
            except Exception as e:  # noqa: BLE001
                errors.append({"url": u, "error": str(e)[:200]})
            if i % 10 == 0 or i == len(links):
                print("  %d/%d processed" % (i, len(links)), flush=True)

    results.sort(key=lambda r: (r["date"] or "0000-00-00"), reverse=True)
    out = {
        "listing": args.listing,
        "base": base,
        "count": len(results),
        "with_video": sum(1 for r in results if r["has_video"]),
        "skipped_hub_pages": skipped,
        "errors": errors,
        "events": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("wrote %s: %d events, %d with video, %d skipped, %d errors"
          % (args.out, len(results), out["with_video"], skipped, len(errors)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
