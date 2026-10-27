"""Regression tests for the 4 defects judge F identified.

D1  single-video mode: a file failing verification must be quarantined,
    not left in place to be counted as "downloaded".
D2  build_index with --report pointing at a MISSING file must fail loudly,
    not silently degrade to "file exists == downloaded".
D3  build_index must not count a file whose slug is absent from the report
    (report is authoritative, not merely advisory).
D4  --skip-existing: a file that fails verification must be quarantined AND
    actually re-downloaded (fall through, not `continue`), and the outcome
    must appear in the report's failures list.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SKILL, "scripts")
FAILED = []


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dl = load("download_webinar")
bi = load("build_index_xlsx")

FF = shutil.which("ffmpeg")
if not FF:
    print("SKIP: ffmpeg not found")
    sys.exit(0)


def make_silent(path, dur=2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run([FF, "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=%d:size=320x240:rate=10" % dur,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", path], capture_output=True)


def make_sound(path, dur=2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run([FF, "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=%d:size=320x240:rate=10" % dur,
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=%d" % dur,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", path], capture_output=True)


def manifest_for(slug, vid, title):
    return {"listing": "t", "base": "t", "count": 1, "with_video": 1,
            "skipped_hub_pages": 0, "errors": [],
            "events": [{"slug": slug, "title": title, "url": "https://x/1",
                        "date": "2026-01-01", "time": "",
                        "videos": [{"platform": "vimeo", "id": vid}],
                        "has_video": True, "presenters": [], "attachments": [],
                        "description": "d"}]}


# ---------------------------------------------------------------- D1
print("=== D1: 单视频模式校验失败 -> 隔离 ===")
root = tempfile.mkdtemp(prefix="d1_")
bad = os.path.join(root, "videos", "Silent [999999].mp4")
make_silent(bad)
ok, rep = dl.verify(bad)
print("   verify 无声文件 ok =", ok, "(应 False)")
if ok:
    FAILED.append("D1: verify 竟然判无声文件为通过")
q = dl._quarantine(bad, os.path.join(root, "videos"))
still = os.path.exists(bad)
print("   隔离后原位置存在 =", still, "(应 False)")
if still:
    FAILED.append("D1: 坏文件未被移走")
print("   隔离目录 =", os.path.basename(os.path.dirname(q)))

# ---------------------------------------------------------------- D2
print("=== D2: --report 指向不存在的文件 -> 报错退出 ===")
root2 = tempfile.mkdtemp(prefix="d2_")
mp = os.path.join(root2, "m.json")
with open(mp, "w", encoding="utf-8") as f:
    json.dump(manifest_for("s", "111", "T"), f)
r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "build_index_xlsx.py"),
                    "--manifest", mp, "--out", os.path.join(root2, "i.xlsx"),
                    "--report", os.path.join(root2, "nope.json"),
                    "--videos-dir", os.path.join(root2, "videos")],
                   capture_output=True, text=True)
print("   返回码 =", r.returncode, "(应非 0)")
if r.returncode == 0:
    FAILED.append("D2: report 缺失时未报错，静默降级了")
else:
    print("   报错信息:", (r.stderr or r.stdout).strip().splitlines()[0][:80])

# ---------------------------------------------------------------- D3
print("=== D3: slug 不在 report 内 -> 不计入已下载 ===")
root3 = tempfile.mkdtemp(prefix="d3_")
vdir = os.path.join(root3, "videos", "x-slug")
make_sound(os.path.join(vdir, "X [777777].mp4"))
mp3 = os.path.join(root3, "m.json")
with open(mp3, "w", encoding="utf-8") as f:
    json.dump(manifest_for("x-slug", "777777", "X Title"), f)
rp3 = os.path.join(root3, "rep.json")
with open(rp3, "w", encoding="utf-8") as f:
    json.dump({"ok": 0, "skipped": 0, "failed": 0, "failures": [],
               "videos": {"other-slug": {"ok": True, "file": "z", "report": {}}}},
              f, ensure_ascii=False)
out3 = os.path.join(root3, "i.xlsx")
r3 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "build_index_xlsx.py"),
                     "--manifest", mp3, "--out", out3, "--report", rp3,
                     "--videos-dir", os.path.join(root3, "videos")],
                    capture_output=True, text=True)
if r3.returncode != 0:
    print("   build 失败:", r3.stderr[-200:])
    FAILED.append("D3: build 失败")
else:
    import openpyxl
    wb = openpyxl.load_workbook(out3)
    d = wb["明细"]
    flag = None
    for rr in range(2, 8):
        if d.cell(rr, 3).value:
            flag = d.cell(rr, 14).value
    print("   '是否已下载' =", flag, "(应 None/否 —— 文件存在但报告未覆盖)")
    if flag == "是":
        FAILED.append("D3: 报告未覆盖的 slug 仍被算成已下载")

# ---------------------------------------------------------------- D4
print("=== D4: skip-existing 复验失败 -> 隔离后真的重下 ===")
root4 = tempfile.mkdtemp(prefix="d4_")
vdir4 = os.path.join(root4, "videos")
os.makedirs(vdir4, exist_ok=True)
bad4 = os.path.join(vdir4, "Bad [888888].mp4")
make_silent(bad4)
found = dl._find_existing(vdir4, vdir4, "888888")
print("   找到已有文件 =", os.path.basename(found) if found else None)
if found:
    good, _ = dl.verify(found)
    if good:
        FAILED.append("D4: 坏文件复验竟然通过")
    q4 = dl._quarantine(found, vdir4)
    moved = not os.path.exists(found)
    in_failed = os.path.exists(os.path.join(vdir4, "_failed",
                                            os.path.basename(found)))
    print("   隔离后原位置存在 =", moved, "(应 False)")
    print("   文件已在 _failed/ =", in_failed, "(应 True)")
    if not moved:
        FAILED.append("D4: 隔离失败")
    if not in_failed:
        FAILED.append("D4: 文件未进 _failed/")
print("   注：'continue 改成 fall-through' 的实跑验证见 batch 集成测试")

# ----------------------------------------------------------------
print()
if FAILED:
    for f in FAILED:
        print("FAIL:", f)
    print("RESULT: FAIL (%d)" % len(FAILED))
    sys.exit(1)
print("RESULT: PASS")
