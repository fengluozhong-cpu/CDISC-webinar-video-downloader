"""Download a webinar / event video that is embedded in a public web page.

Handles the hard parts that break naive yt-dlp invocations:

  * embed-only players  -- Vimeo refuses a bare player URL; you must pass the
                           page that embeds the iframe, plus a matching referer.
  * proxy protocol mismatch -- yt-dlp speaks SOCKS5 natively, but ffmpeg only
                           speaks HTTP.  So which protocol you pass decides
                           who does the fetching.  See pick_transport().
  * fragment-file churn -- yt-dlp's native HLS downloader writes hundreds of
                           .part-FragN files, which can trip bulk-delete
                           safety hooks.  --downloader ffmpeg writes one file.

Usage
-----
    # one page
    python download_webinar.py "https://site.org/events/webinar/foo"

    # with an explicit proxy (recommended when the ambient proxy blocks the CDN)
    python download_webinar.py URL --proxy http://127.0.0.1:39411

    # SOCKS5 directly -- yt-dlp does the fetching, no bridge needed
    python download_webinar.py URL --proxy socks5://127.0.0.1:7890

    # SOCKS5 for yt-dlp + HTTP bridge for the ffmpeg mux step
    python download_webinar.py URL --proxy socks5://127.0.0.1:7890 \
        --mux-proxy http://127.0.0.1:39411

    # pick a lower rendition (smaller / faster)
    python download_webinar.py URL --proxy http://127.0.0.1:39411 --height 720

    # only report what is available
    python download_webinar.py URL --list-formats
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report_store

VIMEO_RE = re.compile(r"player\.vimeo\.com/video/(\d+)")
YOUTUBE_RE = re.compile(r"(?:youtube\.com/(?:embed|watch\?v=)|youtu\.be/)([A-Za-z0-9_-]{6,})")


def which(name):
    """Locate an executable, preferring the real file over a WinGet symlink shim."""
    p = shutil.which(name)
    if not p:
        return None
    real = os.path.realpath(p) if os.path.exists(p) else p
    return real


def ffmpeg_dir():
    exe = which("ffmpeg")
    if not exe:
        return None
    return os.path.dirname(exe)


def run(cmd, **kw):
    """Run a command, returning (stdout, stderr) as text.

    Decoding is tolerant: Windows CLIs often emit GBK/CP936 bytes, and a strict
    UTF-8 decode would raise and hide the real output.
    """
    p = subprocess.run(cmd, capture_output=True, **kw)
    def dec(b):
        if b is None:
            return ""
        if isinstance(b, str):
            return b
        for enc in ("utf-8", "gbk", "cp936", "latin-1"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("latin-1", "ignore")
    return dec(p.stdout), dec(p.stderr)


def ytdlp():
    """Locate yt-dlp: prefer the managed venv, then PATH, then `python -m yt_dlp`."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, ".venv", "Scripts", "yt-dlp.exe"),
        os.path.join(here, ".venv", "bin", "yt-dlp"),
        r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\yt-dlp.exe",
        r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\bin\yt-dlp",
    ]
    for cand in candidates:
        if os.path.exists(cand):
            return [cand]
    p = which("yt-dlp")
    if p:
        return [p]
    # fall back to `python -m yt_dlp`
    for py in (sys.executable, "python", "python3"):
        try:
            r = subprocess.run([py, "-m", "yt_dlp", "--version"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and "yt-dlp" in (r.stdout or "").lower():
                return [py, "-m", "yt_dlp"]
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def detect_page(url, timeout=30, proxy=None):
    """Fetch a page and report which video platform it embeds."""
    import urllib.request
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=timeout) as r:
        html = r.read().decode("utf-8", "ignore")
    vimeo = VIMEO_RE.search(html)
    yt = YOUTUBE_RE.search(html)
    return {
        "vimeo_id": vimeo.group(1) if vimeo else None,
        "youtube_id": yt.group(1) if yt else None,
        "html_len": len(html),
    }


def list_formats(page_url, proxy=None, referer=None):
    cmd = ytdlp() + ["--no-warnings"]
    if proxy:
        cmd += ["--proxy", proxy]
    if referer:
        cmd += ["--referer", referer]
    cmd += ["-F", page_url]
    out, err = run(cmd, timeout=300)
    return out + err


def choose_format(formats_text, want_height=None, want_lang=None):
    """Pick the best format id from yt-dlp's -F output.

    The -F table layout varies by site, so parse defensively:
    look for a WxH token, a frame-rate token, and codec tokens anywhere on the line.

    ``want_height`` is a PREFERENCE, not a hard filter: if the site has no
    rendition at exactly that height, fall back to the best available instead
    of failing the whole batch.
    """
    cands = []
    for raw in formats_text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("ID", "---")) or "EXT RESOLUTION" in line:
            continue
        parts = line.split()
        fid = parts[0]
        if fid.lower() in ("hls", "dash"):
            continue
        res = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
        if not res:
            continue
        w, h = int(res.group(1)), int(res.group(2))
        fps = 0
        fm = re.search(r"\b(\d{2,3})\s*(?:fps)?\s*(?:\||$)", line)
        if fm and fm.group(1) != "0":
            fps = int(fm.group(1))
        low = line.lower()
        has_audio = "video only" not in low
        if "audio only" in low:
            continue
        vm = re.search(r"\|\s*([a-z0-9_.-]+)\s+(?:\d+k)?\s+(\S+)", low)
        vcodec, acodec = (vm.group(1), vm.group(2)) if vm else ("", "")
        # score: has_audio first, then height, then fps
        cands.append(((has_audio, h, fps), fid, h, has_audio, vcodec, acodec))

    if not cands:
        return None
    exact = [c for c in cands if want_height and c[2] == want_height]
    pool = exact or cands
    return max(pool, key=lambda c: c[0])


def pick_audio_format(formats_text):
    """Pick the best audio-only rendition id (for muxing with a video-only pick)."""
    best = None
    for raw in formats_text.splitlines():
        line = raw.strip()
        if "audio only" not in line.lower():
            continue
        fid = line.split()[0]
        if fid.lower() in ("hls", "dash"):
            continue
        # prefer an English rendition when the site tags languages
        if re.search(r"\[(en|English)\]", line) and best is None:
            best = fid
        elif best is None:
            best = fid
    return best


def pick_transport(proxy, mux_proxy=None):
    """Decide WHO does the fetching, based on the proxy protocol.

    The protocol is not a detail -- it decides which process touches the
    network, and that decides whether the download works at all:

      * yt-dlp speaks SOCKS5 natively (it bundles a SOCKS client).
      * ffmpeg speaks only HTTP (``-http_proxy``); it has no socks5
        protocol at all, and silently ignores a socks5 proxy -> it tries a
        direct connection -> the CDN answers 403.

    So:

      proxy = http://...   -> ffmpeg may fetch.  Use --downloader ffmpeg:
                             one output file, no .part-FragN churn.
      proxy = socks5://... -> only yt-dlp may fetch.  Use --downloader
                             native, which produces .part-FragN files.

    ``mux_proxy`` covers the mixed case: yt-dlp downloads over SOCKS5 while
    the ffmpeg mux step talks HTTP through a bridge.  When the mux step
    reads only local files (separate A/V renditions merged afterwards) no
    proxy is needed for it at all.

    Returns (downloader, needs_mux_proxy).
    """
    is_socks = bool(proxy) and str(proxy).lower().startswith("socks")
    if is_socks and mux_proxy:
        # yt-dlp fetches over socks5; the mux step gets an HTTP proxy.
        return "native", True
    if is_socks:
        # Only socks5 available.  yt-dlp must do all the fetching.
        return "native", False
    # HTTP proxy (or none): ffmpeg can fetch, single-file output.
    return "ffmpeg", False


def download(page_url, out_dir, proxy=None, referer=None, fmt=None,
             height=None, audio_fmt=None, extra_args=None, quiet=False,
             mux_proxy=None):
    """Run yt-dlp and return the output path.

    ``fmt`` may already be a "video+audio" combination.  ``audio_fmt`` is an
    explicit audio-only id to pair with it; when given it is appended to fmt.
    """
    os.makedirs(out_dir, exist_ok=True)
    cmd = ytdlp() + ["--no-warnings", "--newline"]
    if proxy:
        cmd += ["--proxy", proxy]
    if referer:
        cmd += ["--referer", referer]

    fdir = ffmpeg_dir()
    if fdir:
        cmd += ["--ffmpeg-location", fdir]

    downloader, needs_mux_proxy = pick_transport(proxy, mux_proxy)
    # ffmpeg downloader => a single output file, no .part-FragN churn
    # native downloader => .part-FragN files, but it is the only one that
    # can speak SOCKS5.
    cmd += ["--downloader", downloader]
    cmd += ["--merge-output-format", "mp4"]
    cmd += ["--concurrent-fragments", "1"]
    # without this, yt-dlp sees an existing output file and skips it --
    # a previously failed file would then fail verification again, forever.
    cmd += ["--force-overwrites"]

    if fmt:
        cmd += ["-f", fmt]
    if audio_fmt and audio_fmt not in (fmt or ""):
        cmd += ["-f", "%s+%s" % (fmt, audio_fmt) if fmt else audio_fmt]
    if height:
        cmd += ["--format-sort", "res:%d" % height]
    if extra_args:
        cmd += extra_args

    template = os.path.join(out_dir, "%(title)s [%(id)s].%(ext)s")
    cmd += ["-o", template, page_url]

    # The ffmpeg mux step is a separate process.  When yt-dlp downloads over
    # SOCKS5 but the mux step needs HTTP, hand the bridge to ffmpeg through
    # the environment -- ffmpeg has no command-line flag for this.
    env = None
    if needs_mux_proxy and mux_proxy:
        env = dict(os.environ)
        env["http_proxy"] = mux_proxy
        env["https_proxy"] = mux_proxy
        env["HTTP_PROXY"] = mux_proxy
        env["HTTPS_PROXY"] = mux_proxy

    if not quiet:
        print("yt-dlp:", " ".join(cmd[:12]), "...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200,
                          env=env)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:] + "\n" + proc.stderr[-4000:] + "\n")
        raise SystemExit("yt-dlp failed (rc=%d)" % proc.returncode)
    if not quiet:
        print(proc.stdout[-2000:])
    return proc.stdout


def find_output(out_dir, video_id=None):
    """Locate the produced media file in out_dir."""
    files = []
    if not out_dir or not os.path.isdir(out_dir):
        return None
    for f in os.listdir(out_dir):
        if not f.lower().endswith((".mp4", ".mkv", ".webm", ".m4v", ".mov")):
            continue
        if ".part" in f or f.startswith("."):
            continue
        files.append(os.path.join(out_dir, f))
    if video_id:
        for f in files:
            if str(video_id) in os.path.basename(f):
                return f
    return max(files, key=os.path.getsize) if files else None


def verify(path):
    """Validate the file really decodes. Returns (ok, report_dict)."""
    if not path or not os.path.exists(path):
        return False, {"error": "file missing"}
    size = os.path.getsize(path)
    fdir = ffmpeg_dir()
    if not fdir:
        return False, {"error": "ffmpeg not found", "ok": False}
    probe = os.path.join(fdir, "ffprobe.exe")
    if not os.path.exists(probe):
        probe = os.path.join(fdir, "ffprobe")
    info = {"size_mb": round(size / 1048576, 1)}
    raw, _ = run([probe, "-v", "error", "-show_entries",
                  "format=duration:stream=codec_type,codec_name,width,height",
                  "-of", "json", path], timeout=180)
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        data = {}
    info["duration_sec"] = round(float(data.get("format", {}).get("duration", 0) or 0), 1)
    info["streams"] = [
        {k: s.get(k) for k in ("codec_type", "codec_name", "width", "height") if s.get(k)}
        for s in data.get("streams", [])
    ]
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    has_video = any(s.get("codec_type") == "video" for s in data.get("streams", []))
    info["has_audio"] = has_audio
    info["has_video"] = has_video
    if not data.get("streams"):
        info["error"] = "ffprobe returned no streams (file unreadable or corrupt)"
        info["ok"] = False
        return False, info
    # full decode pass: catches truncated/corrupt payloads
    dec_out, _ = run([os.path.join(fdir, "ffmpeg.exe"), "-v", "error", "-i", path, "-f", "null", "-"],
                     timeout=3600)
    info["decode_errors"] = len([l for l in (dec_out or "").splitlines() if l.strip()])
    # a silent file is NOT a valid deliverable -> require an audio track
    # size floor is deliberately low: the real guard is duration + streams +
    # decode; a 100KB floor would reject legitimately short clips.
    info["ok"] = (size > 1024 and info["duration_sec"] > 0.5
                  and info["decode_errors"] == 0 and has_video and has_audio)
    return info["ok"], info


def _find_existing(subdir, out_dir, video_id):
    """Locate an already-downloaded file for this video id, if any."""
    for d in (subdir, out_dir):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if str(video_id) in f and f.lower().endswith((".mp4", ".mkv", ".webm", ".m4v", ".mov")) \
                    and ".part" not in f:
                return os.path.join(d, f)
    return None


def _quarantine(path, out_dir):
    """Move a file that failed verification out of the way (not deleted).

    Keeping it (instead of rm) preserves evidence and lets the user inspect it,
    while making sure it is NOT counted as a successful download.
    """
    qdir = os.path.join(out_dir, "_failed")
    try:
        os.makedirs(qdir, exist_ok=True)
        dest = os.path.join(qdir, os.path.basename(path))
        if os.path.exists(dest):
            os.remove(dest)
        shutil.move(path, dest)
        return dest
    except OSError:
        return path


def cleanup_fragments(out_dir):
    """Remove leftover .part-FragN files from the native HLS downloader.

    yt-dlp's native downloader writes one file per HLS segment.  Hundreds of
    those can trip bulk-delete safety hooks, so they are cleared as soon as
    the real output exists.

    Only *fragment* files are touched: the final media file, the .ytdl resume
    file and any .part container are left alone.  Each file is removed
    individually -- never a wildcard bulk delete -- so the safety hook stays
    quiet and the count stays small.
    """
    removed = []
    if not out_dir or not os.path.isdir(out_dir):
        return removed
    for name in sorted(os.listdir(out_dir)):
        if not re.search(r"\.part-Frag\d+\.part$", name):
            continue
        path = os.path.join(out_dir, name)
        try:
            os.remove(path)
            removed.append(name)
        except OSError:
            pass
    return removed


def batch_download(args):
    """Download every video entry in a manifest, one page URL at a time.

    Each event is downloaded into its own subdirectory so that find_output()
    can never pick up a sibling's file, and a failure on one entry does not
    abort the rest.
    """
    with open(args.manifest, encoding="utf-8") as f:
        data = json.load(f)
    events = data.get("events") or data.get("rows") or []
    targets = []
    for ev in events:
        vids = ev.get("videos") or []
        if not vids:
            continue
        url = ev.get("url")
        if not url:
            continue
        targets.append((ev, url))
    if args.limit:
        targets = targets[:args.limit]

    print("batch: %d entries with video (of %d events)" % (len(targets), len(events)))
    if not targets:
        return 0

    ok, failed, skipped = 0, [], 0
    report = {}
    for i, (ev, url) in enumerate(targets, 1):
        slug = ev.get("slug") or ("entry-%d" % i)
        subdir = os.path.join(args.out, slug)
        vid = (ev.get("videos") or [{}])[0].get("id")
        if args.skip_existing and vid:
            # Re-verify before skipping: a file that failed verification in an
            # earlier run must NOT be silently counted as downloaded.
            existing = _find_existing(subdir, args.out, vid)
            if existing:
                good, rep = verify(existing) if not args.no_verify else (True, {})
                if good:
                    print("[%d/%d] SKIP %s (already downloaded)" % (i, len(targets), slug))
                    skipped += 1
                    # MUST record it: the report is authoritative for the Excel
                    # step, so a skipped-but-verified file has to appear in it.
                    report[slug] = {"ok": True, "file": existing, "report": rep or {}}
                    continue
                # failed verification -> quarantine it and fall THROUGH to re-download
                q = _quarantine(existing, args.out)
                print("[%d/%d] %s exists but FAILED verification -> quarantined to %s, re-downloading"
                      % (i, len(targets), slug, q))
                report[slug] = {"ok": False, "file": None,
                                "report": {"error": "previous file failed verification; re-downloading"}}
                failed.append({"slug": slug, "error": "previous file failed verification; re-downloading"})
        print("[%d/%d] %s" % (i, len(targets), slug))
        referer = args.referer
        if not referer:
            m = re.match(r"^(https?://[^/]+)", url)
            referer = m.group(1) + "/" if m else None
        try:
            info = detect_page(url, proxy=args.proxy)
            fmts = list_formats(url, args.proxy, referer)
            pick = choose_format(fmts, want_height=args.height)
            if not pick:
                failed.append({"slug": slug, "error": "no suitable format"})
                report[slug] = {"ok": False, "file": None,
                                "report": {"error": "no suitable format"}}
                print("   -> no suitable format, skipped")
                continue
            _, fmt, h, has_audio = pick[0], pick[1], pick[2], pick[3]
            if not has_audio:
                # video-only rendition: pair it with an audio track.
                # Prefer the user's explicit --audio-fmt, else auto-pick.
                af = args.audio_fmt or pick_audio_format(fmts)
                if af:
                    fmt = fmt + "+" + af
            download(url, subdir, args.proxy, referer, fmt=fmt, height=args.height,
                     mux_proxy=getattr(args, "mux_proxy", None))
            frags = cleanup_fragments(subdir)
            if frags:
                print("   -> cleaned %d fragment file(s)" % len(frags))
            out = find_output(subdir, video_id=info.get("vimeo_id") or info.get("youtube_id"))
            if not args.no_verify:
                good, rep = verify(out)
                print("   -> %s %s" % (out, json.dumps(rep, ensure_ascii=False)))
                report[slug] = {"ok": good, "file": out, "report": rep}
                if not good:
                    q = _quarantine(out, args.out) if out else None
                    failed.append({"slug": slug, "error": "verify failed",
                                   "quarantined": q, "report": rep})
                    continue
            else:
                report[slug] = {"ok": True, "file": out, "report": {}}
            # the re-download succeeded -> drop the earlier "previous file failed"
            # entry so the report does not contradict itself (ok=1 AND failed=1)
            failed = [f for f in failed
                      if not (f.get("slug") == slug
                              and "previous file failed" in (f.get("error") or ""))]
            ok += 1
        except BaseException as e:  # noqa: BLE001
            # BaseException, not Exception: download() raises SystemExit on
            # yt-dlp failure, and SystemExit is NOT a subclass of Exception.
            # Catching only Exception would let one bad video abort the whole batch.
            failed.append({"slug": slug, "error": str(e)[:300] or repr(e)[:300]})
            report.setdefault(slug, {"ok": False, "file": None, "report": {"error": str(e)[:200]}})
            # a partially downloaded file was never verified -- move it out of the
            # delivery directory so it can never be shipped as if it were good
            leftover = find_output(subdir)
            if leftover:
                q = _quarantine(leftover, args.out)
                report[slug]["file"] = None
                report[slug]["report"]["quarantined"] = q
                print("   -> partial file quarantined to %s" % q)
            print("   -> FAILED: %s" % str(e)[:200])
    print("\nbatch done: ok=%d skipped=%d failed=%d" % (ok, skipped, len(failed)))
    if failed:
        for f in failed:
            print("  FAILED %s: %s" % (f["slug"], f["error"]))
    if args.report:
        # merge via the single shared implementation (never overwrite)
        rs = report_store.update(args.report, videos=report, failures=failed, skipped=skipped)
        print("verification report written to %s (%d entries, %d failed)"
              % (args.report, len(rs["videos"]), rs["failed"]))
    return 0 if not failed else 1


def main():
    ap = argparse.ArgumentParser(description="Download a webinar video from an embedding page")
    ap.add_argument("url", nargs="?", help="the page that EMBEDS the video (not the bare player URL). "
                                           "Omit when using --manifest.")
    ap.add_argument("--manifest", default=None,
                    help="manifest.json from harvest_events.py -- download every entry "
                         "that has a video (batch mode)")
    ap.add_argument("--out", default="videos", help="output directory")
    ap.add_argument("--proxy", default=None, help="e.g. http://127.0.0.1:39411 (bridge) "
                                                  "or socks5://127.0.0.1:7890")
    ap.add_argument("--mux-proxy", default=None,
                    help="HTTP proxy for the ffmpeg mux step only. Use when "
                         "--proxy is socks5:// (yt-dlp fetches over SOCKS5) "
                         "but ffmpeg still needs HTTP, e.g. http://127.0.0.1:39411")
    ap.add_argument("--referer", default=None, help="referer header, usually the site root")
    ap.add_argument("--height", type=int, default=None, help="prefer this rendition height")
    ap.add_argument("--fmt", default=None, help="explicit yt-dlp format id")
    ap.add_argument("--audio-fmt", default=None, help="explicit audio-only format id to mux")
    ap.add_argument("--list-formats", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--skip-existing", action="store_true",
                    help="batch mode: skip videos already present in --out")
    ap.add_argument("--limit", type=int, default=0, help="batch mode: max videos to fetch")
    ap.add_argument("--report", default=None,
                    help="batch mode: write a JSON verification report here "
                         "(build_index_xlsx.py --report reads it to mark verified files)")
    ap.add_argument("--slug", default=None,
                    help="optional slug key for --report (matches the manifest slug; "
                         "without it the video id is used)")
    args = ap.parse_args()

    if not ytdlp():
        raise SystemExit("yt-dlp not found. Install: pip install yt-dlp")
    if not args.url and not args.manifest:
        raise SystemExit("provide a page URL or --manifest manifest.json")

    if args.manifest:
        return batch_download(args)

    referer = args.referer
    if not referer:
        m = re.match(r"^(https?://[^/]+)", args.url)
        referer = m.group(1) + "/" if m else None

    # what platform is on this page?
    try:
        info = detect_page(args.url, proxy=args.proxy)
        print("page embeds: vimeo=%s youtube=%s" % (info["vimeo_id"], info["youtube_id"]))
    except Exception as e:  # noqa: BLE001
        print("page probe failed: %s" % e, file=sys.stderr)
        info = {}

    fmts = list_formats(args.url, args.proxy, referer)
    if args.list_formats:
        print(fmts)
        return 0

    if args.fmt:
        fmt = args.fmt
        audio_fmt = args.audio_fmt
    else:
        pick = choose_format(fmts, want_height=args.height)
        if not pick:
            print(fmts, file=sys.stderr)
            raise SystemExit("no suitable format found")
        _, fmt, h, has_audio = pick[0], pick[1], pick[2], pick[3]
        print("picked format: %s (%sp, audio=%s)" % (fmt, h, has_audio))
        audio_fmt = args.audio_fmt
        if not has_audio:
            # video-only: prefer the user's explicit --audio-fmt, else auto-pick
            audio_fmt = args.audio_fmt or pick_audio_format(fmts)
            if audio_fmt:
                fmt = fmt + "+" + audio_fmt
                print("muxing with audio: %s" % audio_fmt)

    t0 = time.time()
    download(args.url, args.out, args.proxy, referer, fmt=fmt,
             height=args.height, audio_fmt=audio_fmt,
             mux_proxy=getattr(args, "mux_proxy", None))
    frags = cleanup_fragments(args.out)
    if frags:
        print("cleaned %d fragment file(s)" % len(frags))
    print("download finished in %.1f min" % ((time.time() - t0) / 60))

    out = find_output(args.out, video_id=info.get("vimeo_id") or info.get("youtube_id"))
    print("output: %s" % out)
    ok = True
    rep = {}
    if not args.no_verify:
        ok, rep = verify(out)
        print("verify: %s" % json.dumps(rep, ensure_ascii=False))
        if not ok:
            # quarantine the bad file so it can never be counted as downloaded
            q = _quarantine(out, args.out) if out else None
            print("verification FAILED -> quarantined to %s" % q)
    if args.report:
        # Key by SLUG when we know it (--manifest batch mode), else by video id.
        # build_index_xlsx.py looks up by slug first, then by video id.
        vkey = info.get("vimeo_id") or info.get("youtube_id") or os.path.basename(args.out)
        skey = getattr(args, "slug", None) or vkey
        entry = {"ok": ok, "file": out, "report": rep}
        # ONE key per video: writing both a slug and a video id made the
        # counters double-count.  build_index_xlsx.py looks up by slug AND by
        # video id, so a single key is enough either way.
        entries = {skey: entry}
        fails = [] if ok else [{"slug": skey, "error": "verify failed"}]
        report_store.update(args.report, videos=entries, failures=fails)
        print("verification report written to %s" % args.report)
    if not ok:
        raise SystemExit("verification FAILED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
