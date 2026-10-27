"""Regression test for the cross-key retry defect (judge J's A-1).

Scenario (the real workflow SKILL.md prescribes):
  1. a batch run fails for slug "my-event"  -> report has videos["my-event"].ok=False
                                             and a failure row for "my-event"
  2. the Agent retries THAT ONE video with the documented single-video command,
     which has no --slug -> the key becomes the VIDEO ID
     -> report now has videos["my-event"].ok=False (stale)
                 videos["777666555"].ok=True  (the successful retry)

The Excel step looks up by slug first. With slug-priority-only lookup, the
stale ok=False wins and the (verified, perfectly good) file is reported as
NOT downloaded -- Excel count 0 while the disk holds 1 file.

Fix: a video counts as downloaded if ANY of its keys was verified.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
PY = sys.executable
FF = shutil.which("ffmpeg")

FAILED = []


def check(label, cond, detail=""):
    print("   %-50s %s%s" % (label, "PASS" if cond else "FAIL",
                             "" if cond else "  <- " + str(detail)))
    if not cond:
        FAILED.append(label)


def make_sound(path, dur=2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run([FF, "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=%d:size=320x240:rate=10" % dur,
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=%d" % dur,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path],
                   capture_output=True)


print("=== A-1: 跨键重试后清单不能漏计已校验通过的下载 ===")
root = tempfile.mkdtemp(prefix="a1x_")
vd = os.path.join(root, "videos", "my-event")
make_sound(os.path.join(vd, "My Event [777666555].mp4"))

mp = os.path.join(root, "m.json")
with open(mp, "w", encoding="utf-8") as f:
    json.dump({"listing": "t", "base": "t", "count": 1, "with_video": 1,
               "skipped_hub_pages": 0, "errors": [],
               "events": [{"slug": "my-event", "title": "My Event", "url": "https://x/1",
                           "date": "2026-01-01", "time": "",
                           "videos": [{"platform": "vimeo", "id": "777666555"}],
                           "has_video": True, "presenters": [], "attachments": [],
                           "description": "d"}]}, f, ensure_ascii=False)

rep = os.path.join(root, "rep.json")
# exactly the state judge J described: stale slug failure + successful vid retry
with open(rep, "w", encoding="utf-8") as f:
    json.dump({"ok": 1, "skipped": 0, "failed": 1,
               "failures": [{"slug": "my-event", "error": "verify failed"}],
               "videos": {"my-event": {"ok": False, "file": None, "report": {}},
                          "777666555": {"ok": True, "file": "x", "report": {}}}}, f)

out = os.path.join(root, "i.xlsx")
r = subprocess.run([PY, os.path.join(SCRIPTS, "build_index_xlsx.py"),
                    "--manifest", mp, "--out", out, "--report", rep,
                    "--videos-dir", os.path.join(root, "videos")],
                   capture_output=True, text=True)
if r.returncode != 0:
    print("   build 失败:", r.stderr[-300:])
    FAILED.append("A-1: build 失败")
else:
    import openpyxl
    wb = openpyxl.load_workbook(out)
    d = wb["明细"]
    flag = None
    for rr in range(2, 8):
        if d.cell(rr, 3).value:
            flag = d.cell(rr, 14).value
    disk = len([f for f in os.listdir(vd) if f.endswith(".mp4")])
    print("   清单「是否已下载」 =", flag, "| 磁盘文件数 =", disk)
    check("已校验通过的文件被计入", flag == "是", flag)
    check("清单数字 == 磁盘数字", (1 if flag == "是" else 0) == disk, (flag, disk))

print("=== report 计数：调用方每个视频只写一个键，故计数即精确值 ===")
sys.path.insert(0, SCRIPTS)
import report_store as rs  # noqa: E402
n = rs.merge({"videos": {}, "failures": [], "ok": 0, "failed": 0},
             videos={"my-event": {"ok": True, "file": "f", "report": {}}},
             failures=[])
check("单个成功视频 ok == 1", n["ok"] == 1, n["ok"])
n2 = rs.merge({"videos": {}, "failures": [], "ok": 0, "failed": 0},
              videos={"my-event": {"ok": False, "file": None, "report": {}}},
              failures=[{"slug": "my-event", "error": "x"}])
check("单个失败视频 failed == 1", n2["failed"] == 1, n2["failed"])
n3 = rs.merge({"videos": {}, "failures": [], "ok": 0, "failed": 0},
              videos={"a": {"ok": True, "file": "fa", "report": {}},
                      "b": {"ok": True, "file": "fb", "report": {}},
                      "c": {"ok": False, "file": None, "report": {}}},
              failures=[{"slug": "c", "error": "x"}])
check("2 成功 + 1 失败 -> ok==2 failed==1", (n3["ok"], n3["failed"]) == (2, 1),
      (n3["ok"], n3["failed"]))
check("ok 与 videos 中 ok 的数量一致",
      n3["ok"] == sum(1 for e in n3["videos"].values() if e.get("ok")), n3["ok"])

print()
if FAILED:
    for f in FAILED:
        print("FAIL:", f)
    print("RESULT: FAIL (%d)" % len(FAILED))
    sys.exit(1)
print("RESULT: PASS")
