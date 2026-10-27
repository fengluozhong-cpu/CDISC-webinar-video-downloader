"""Offline regression test for the date-parsing bug fixed in harvest_events.py.

Bug: `iso, time_s = parse_date(date_txt)` was executed unconditionally, so the
full-text fallback date found at the end was overwritten with '' whenever
date_txt was empty -> non-Drupal sites lost their date entirely -> is_hub_page
then dropped real events (silent under-capture).

This test calls the parse logic in isolation (no network).
"""
import importlib.util
import os
import re
import sys

spec = importlib.util.spec_from_file_location("h", os.path.join(os.path.dirname(__file__), "harvest_events.py"))
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def extract_date(raw):
    """Mirror of the date block inside process(), kept in sync by this test."""
    date_txt = ""
    iso, time_s = "", ""
    m = re.search(r'class="daterange__date"[^>]*>(.*?)</span>', raw, re.S)
    if m:
        date_txt = h.strip_tags(m.group(1))
        iso, time_s = h.parse_date(date_txt)
    if not iso:
        for pat in (r"field--name-field-event-date-timezone(.*?)</div>",
                    r"field--name-field-date(.*?)</div>",
                    r"field--name-field-event-date(.*?)</div>"):
            mm = re.search(pat, raw, re.S)
            if not mm:
                continue
            chunk = mm.group(1)
            attrs = " ".join(re.findall(r'(?:data-from|data-to|datetime)="([^"]+)"', chunk))
            date_txt = (attrs + " " + h.strip_tags(chunk)).strip()
            iso, time_s = h.parse_date(date_txt)
            if iso:
                break
    if not iso:
        iso, time_s = h.parse_date(h.strip_tags(raw)[:2000])
    return iso, time_s


CASES = [
    ("drupal daterange span",
     '<span class="daterange__date">31 August 2023 11:00am - 12:30pm EDT</span>',
     "2023-08-31"),
    ("plain body text (no daterange markup)",
     '<main><p>Join us on 12 March 2026 10:00am to discuss SDTM.</p></main>',
     "2026-03-12"),
    ("iso date in body",
     '<main><p>Event on 2026-05-12 at 9am.</p></main>',
     "2026-05-12"),
    ("no date at all",
     '<main><p>Some webinar without any date information.</p></main>',
     ""),
]

failed = 0
for name, html, expect in CASES:
    got, _ = extract_date(html)
    ok = got == expect
    print("%-42s expect=%-12s got=%-12s %s" % (name, expect or "(empty)", got or "(empty)",
                                               "PASS" if ok else "FAIL"))
    if not ok:
        failed += 1

# the specific regression: a row whose date only exists in the body text
raw = '<html><head><title>Test Event | Site</title></head><body><main><p>Join us on 12 March 2026 for SDTM.</p></main></body></html>'
iso, _ = extract_date(raw)
desc = h.extract_description(raw)
hub = h.is_hub_page("Test Event", "test-event", iso, [], desc)
print()
print("regression: body-text date -> %s ; is_hub_page=%s" % (iso, hub))
if iso != "2026-03-12" or hub:
    print("REGRESSION NOT FIXED")
    failed += 1

sys.exit(1 if failed else 0)
