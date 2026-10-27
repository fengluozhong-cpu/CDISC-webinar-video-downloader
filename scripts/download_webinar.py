"""Download a webinar / event video that is embedded in a public web page.

Handles the hard parts that break naive yt-dlp invocations:

  * embed-only players  -- Vimeo refuses a bare player URL; you must pass the
                           page that embeds the iframe, plus a matching referer.
  * no SOCKS5 in ffmpeg  -- ffmpeg cannot speak SOCKS5, so a local
                           HTTP->SOCKS5 bridge (socks_bridge.py) is used.
  * fragment-file churn -- yt-dlp's native HLS downloader writes hundreds of
                           .part-FragN files, which can trip bulk-delete
                           safety hooks.  --downloader ffmpeg writes one file.
  * missing audio/video  -- HLS "master" playlists often split A/V into two
                           renditions that must be muxed.

Usage
-----
    # one page
    python download_webinar.py "https://site.org/events/webinar/foo"

    # with an explicit proxy (recommended when the ambient proxy blocks the CDN)
    python download_webinar.py URL --proxy http://127.0.0.1:39411

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
    """
    best = None
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
        # acodec column: "video only" / "aac" / "unknown"
        low = line.lower()
        has_audio = "video only" not in low
        if "audio only" in low:
            continue
        vcodec = ""
        acodec = ""
        vm = re.search(r"\|\s*([a-z0-9_.-]+)\s+(?:\d+k)?\s+(\S+)", low)
        if vm:
            vcodec, acodec = vm.group(1), vm.group(2)
        if want_height and h != want_height:
            continue
        score = (has_audio, h, fps)
        if best is None or score > best[0]:
            best = (score, fid, h, has_audio, vcodec, acodec)
    if not best:
        return None
    return best


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


def download(page_url, out_dir, proxy=None, referer=None, fmt=None,
             height=None, audio_fmt=None, extra_args=None, quiet=False):
    """Run yt-dlp with ffmpeg as the downloader. Returns the output path."""
    os.makedirs(out_dir, exist_ok=True)
    cmd = ytdlp() + ["--no-warnings", "--newline"]
    if proxy:
        cmd += ["--proxy", proxy]
    if referer:
        cmd += ["--referer", referer]

    fdir = ffmpeg_dir()
    if fdir:
        cmd += ["--ffmpeg-location", fdir]

    # ffmpeg downloader => a single output file, no .part-FragN churn
    cmd += ["--downloader", "ffmpeg"]
    cmd += ["--merge-output-format", "mp4"]
    cmd += ["--concurrent-fragments", "1"]

    if fmt:
        cmd += ["-f", fmt]
    if extra_args:
        cmd += extra_args

    template = os.path.join(out_dir, "%(title)s [%(id)s].%(ext)s")
    cmd += ["-o", template, page_url]

    if not quiet:
        print("yt-dlp:", " ".join(cmd[:12]), "...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:] + "\n" + proc.stderr[-4000:] + "\n")
        raise SystemExit("yt-dlp failed (rc=%d)" % proc.returncode)
    if not quiet:
        print(proc.stdout[-2000:])
    return proc.stdout


def find_output(out_dir, video_id=None):
    """Locate the produced media file in out_dir."""
    files = []
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
        return False, {"error": "ffmpeg not found"}
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
    # full decode pass: catches truncated/corrupt payloads
    dec_out, _ = run([os.path.join(fdir, "ffmpeg.exe"), "-v", "error", "-i", path, "-f", "null", "-"],
                     timeout=3600)
    info["decode_errors"] = len([l for l in (dec_out or "").splitlines() if l.strip()])
    info["ok"] = size > 100000 and info["duration_sec"] > 1 and info["decode_errors"] == 0
    return info["ok"], info


def main():
    ap = argparse.ArgumentParser(description="Download a webinar video from an embedding page")
    ap.add_argument("url", help="the page that EMBEDS the video (not the bare player URL)")
    ap.add_argument("--out", default="videos", help="output directory")
    ap.add_argument("--proxy", default=None, help="e.g. http://127.0.0.1:39411 (bridge) "
                                                  "or socks5://127.0.0.1:7890")
    ap.add_argument("--referer", default=None, help="referer header, usually the site root")
    ap.add_argument("--height", type=int, default=None, help="prefer this rendition height")
    ap.add_argument("--fmt", default=None, help="explicit yt-dlp format id")
    ap.add_argument("--audio-fmt", default=None, help="explicit audio-only format id to mux")
    ap.add_argument("--list-formats", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    if not ytdlp():
        raise SystemExit("yt-dlp not found. Install: pip install yt-dlp")

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
            audio_fmt = pick_audio_format(fmts)
            if audio_fmt:
                fmt = fmt + "+" + audio_fmt
                print("muxing with audio: %s" % audio_fmt)

    t0 = time.time()
    download(args.url, args.out, args.proxy, referer, fmt=fmt)
    print("download finished in %.1f min" % ((time.time() - t0) / 60))

    out = find_output(args.out, video_id=info.get("vimeo_id") or info.get("youtube_id"))
    print("output: %s" % out)
    if not args.no_verify:
        ok, rep = verify(out)
        print("verify: %s" % json.dumps(rep, ensure_ascii=False))
        if not ok:
            raise SystemExit("verification FAILED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
