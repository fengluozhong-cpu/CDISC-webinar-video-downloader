# CDISC Webinar Video Downloader

Bulk-download CDISC webinars (https://www.cdisc.org/events/webinars/public) and
generate a stats-rich Excel index. Built as a WorkBuddy skill, usable as a plain
Python toolkit.

**Works out of the box for CDISC.** The download engine is platform-agnostic
(Vimeo / YouTube / Wistia / native `<video>` / HLS / mp4) — adapting it to
another site is a documented, testable procedure (see
`references/site-profiles.md`).

## What it solves

Four pitfalls that break naive `yt-dlp` invocations:

1. **Restricted networks** — the page origin and the video CDN often have
   *different* reachability. `--proxy auto` probes direct / ambient HTTP proxy /
   local SOCKS5 and picks the right transport, including the video platform
   domains embedded in the page.
2. **Embed-only players** — Vimeo refuses a bare `player.vimeo.com/video/<id>`
   URL; you must pass the page that embeds the iframe plus a matching referer.
3. **Proxy protocol mismatch** — yt-dlp speaks SOCKS5 natively, ffmpeg only
   speaks HTTP. The protocol you pass decides who does the fetching.
4. **HLS fragment churn** — yt-dlp's native HLS downloader writes hundreds of
   `.part-FragN` files; the script cleans them up (or switches to ffmpeg).

## Quick start

```bash
# 1) harvest the listing into manifest.json (auto-picks the network route)
python scripts/harvest_events.py \
  --listing "https://www.cdisc.org/events/webinars/public" \
  --base "https://www.cdisc.org" \
  --proxy auto \
  --out manifest.json --pages-dir pages --workers 6

# 2) batch download with per-file decode verification
python scripts/download_webinar.py --manifest manifest.json \
  --proxy auto --out videos --report verify_report.json \
  --skip-existing --height 720

# 3) build the Excel index (4 sheets, live formulas)
python scripts/build_index_xlsx.py \
  --manifest manifest.json --out index.xlsx \
  --title "CDISC Webinar Video Index" --videos-dir videos \
  --report verify_report.json
```

## Network auto-routing

`--proxy auto` (supported by all network-facing scripts) probes, in order:

| Environment | Detection | Route |
|---|---|---|
| No proxy (direct) | no `HTTP(S)_PROXY`, bare connect 200 | no `--proxy` |
| Ambient HTTP proxy | `HTTPS_PROXY` works | explicit `http://...` |
| Local SOCKS5 (VPN / Clash) | ambient blocked, SOCKS5 200 | `socks5://127.0.0.1:<port>` |

The page origin **and** the video-platform domains embedded in the page
(`player.vimeo.com`, `www.youtube.com`, `fast.wistia.com`) are probed together —
probing the page alone picks the wrong protocol when a whitelist proxy reaches
the page but blocks the CDN. Run `scripts/probe_network.py <url>` for a full
diagnosis; `--json` includes a machine-readable `recommendation`.

## YouTube: platform-level gates (not a script bug)

Vimeo / CDISC downloads work out of the box. YouTube additionally requires:

- **cookies** — `--cookies-from-browser chrome` (close the browser first:
  Windows locks the cookie DB while it runs); the bot check rejects anonymous
  requests no matter which player client you pick;
- **a JS runtime** — install deno (newer yt-dlp needs it for YouTube formats).

## Repository layout

```
SKILL.md                      # agent-facing workflow (Chinese), the heart of the skill
scripts/                      # 8 scripts: probe / harvest / download / index / bridge ...
tests/                        # regression tests (plain Python, no pytest needed)
references/site-profiles.md   # per-site DOM profiles + adaptation procedure
references/troubleshooting.md # symptom -> fix -> fallback playbook
test-prompts.json             # evaluation prompts with expected behavior
```

## Tests

```bash
for t in tests/test_*.py; do python "$t"; done
```

## License

MIT — see [LICENSE](LICENSE).
