"""Tests for the batch failure-isolation path (judge H's H4 / judge I's B-1).

The bug: the except branch called find_output(subdir) to quarantine a partial
download.  When the failure happened BEFORE download() ran (e.g. detect_page
could not reach the page because the proxy was not up), subdir had never been
created -> os.listdir raised FileNotFoundError -> the exception escaped the
except block -> the WHOLE batch aborted and no report was written at all.

These cases call the real batch_download().
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

spec = importlib.util.spec_from_file_location("dl", os.path.join(SCRIPTS, "download_webinar.py"))
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)

FAILED = []
FF = shutil.which("ffmpeg")


def check(label, cond, detail=""):
    print("   %-52s %s%s" % (label, "PASS" if cond else "FAIL",
                             "" if cond else "  <- " + str(detail)))
    if not cond:
        FAILED.append(label)


def manifest(path, entries):
    evs = [{"slug": s, "title": t, "url": u, "date": "2026-01-01", "time": "",
            "videos": [{"platform": "vimeo", "id": v}], "has_video": True,
            "presenters": [], "attachments": [], "description": "d"}
           for s, t, u, v in entries]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"listing": "t", "base": "t", "count": len(evs), "with_video": len(evs),
                   "skipped_hub_pages": 0, "errors": [], "events": evs}, f, ensure_ascii=False)


def ns_for(mp, out, report, limit=0):
    return argparse.Namespace(manifest=mp, out=out, proxy=None, referer=None, height=None,
                              fmt=None, audio_fmt=None, list_formats=False, no_verify=True,
                              skip_existing=False, limit=limit, report=report, slug=None)


print("=== H4/B-1: 失败发生在 download() 之前（subdir 未创建）不能整批崩溃 ===")
root = tempfile.mkdtemp(prefix="h4_")
out = os.path.join(root, "videos")
os.makedirs(out, exist_ok=True)
mp = os.path.join(root, "m.json")
manifest(mp, [("a", "A", "https://x/a", "111111"), ("b", "B", "https://x/b", "222222")])
rep = os.path.join(root, "rep.json")

# simulate "proxy not up": detect_page raises OSError -> subdir never created
dl.detect_page = lambda url, proxy=None, timeout=30: (_ for _ in ()).throw(
    OSError("WinError 10061 connection refused"))
dl.list_formats = lambda *a, **k: ""
dl.download = lambda *a, **k: None
dl.verify = lambda p: (True, {})

crashed = None
try:
    dl.batch_download(ns_for(mp, out, rep))
except BaseException as e:  # noqa: BLE001
    crashed = e
check("batch 未抛出异常（不整批崩溃）", crashed is None, repr(crashed))
check("report 已写出", os.path.exists(rep))
if os.path.exists(rep):
    with open(rep, encoding="utf-8") as f:
        d = json.load(f)
    check("两场都记为失败", len(d["videos"]) == 2, sorted(d["videos"]))
    check("videos 里没有 ok=True", all(not v.get("ok") for v in d["videos"].values()), d["videos"])
    check("failed 计数与 failures 一致", d["failed"] == len(d["failures"]),
          (d["failed"], len(d["failures"])))

print("=== H4: 下载失败后的半成品文件被隔离 ===")
if FF:
    root2 = tempfile.mkdtemp(prefix="h4b_")
    out2 = os.path.join(root2, "videos")
    mp2 = os.path.join(root2, "m.json")
    manifest(mp2, [("c", "C", "https://x/c", "333333")])
    rep2 = os.path.join(root2, "rep.json")
    # download() leaves a partial file behind, then fails
    def fake_download(url, out_dir, *a, **k):
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "Partial [333333].mp4"), "wb") as f:
            f.write(b"x" * 4096)
        raise SystemExit("yt-dlp failed (rc=1)")

    dl.detect_page = lambda url, proxy=None, timeout=30: {
        "vimeo_id": "333333", "youtube_id": None, "html_len": 0}
    dl.list_formats = lambda *a, **k: (
        "hls-x-282 mp4 426x240 16 | 1MiB m3u8 | avc1 video only\n"
        "hls-x-aud mp4 audio only | m3u8 | audio only unknown [en] English\n")
    dl.download = fake_download
    dl.verify = lambda p: (True, {})
    crashed2 = None
    try:
        dl.batch_download(ns_for(mp2, out2, rep2))
    except BaseException as e:  # noqa: BLE001
        crashed2 = e
    check("未崩溃", crashed2 is None, repr(crashed2))
    leftover = os.path.join(out2, "c", "Partial [333333].mp4")
    check("半成品已从交付目录移走", not os.path.exists(leftover))
    qd = os.path.join(out2, "_failed")
    check("半成品在 _failed/ 中", os.path.isdir(qd) and os.listdir(qd),
          os.listdir(qd) if os.path.isdir(qd) else "no _failed dir")
    if os.path.exists(rep2):
        with open(rep2, encoding="utf-8") as f:
            d2 = json.load(f)
        check("report 记录该场失败", not d2["videos"].get("c", {}).get("ok", True), d2["videos"])
else:
    print("   (跳过：需要 ffmpeg)")

print()
if FAILED:
    for f in FAILED:
        print("FAIL:", f)
    print("RESULT: FAIL (%d)" % len(FAILED))
    sys.exit(1)
print("RESULT: PASS")
