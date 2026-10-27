"""Tests for report_store.py -- the single source of truth for report merging.

Every case calls the real implementation (no re-implementation here).

R1  a slug that failed in a previous run and is FIXED this run must lose its
    stale failure row (otherwise the report says videos[x].ok==True AND
    failures contains x -> an Agent retrying on "failed" would loop forever).
R2  a slug that still fails keeps exactly one failure row.
R3  merging a new slug does not drop the previous ones.
R4  the top-level counters are recomputed from `videos` and never drift.
R5  update() persists and round-trips through disk.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import report_store as rs  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print("   %-46s %s%s" % (label, "PASS" if cond else "FAIL",
                             "" if cond else "  <- " + str(detail)))
    if not cond:
        FAILED.append(label)


print("=== R1: 上轮失败 -> 本轮修复，清掉陈旧 failure ===")
prev = {"videos": {"s1": {"ok": False, "file": None, "report": {}}},
        "failures": [{"slug": "s1", "error": "verify failed"}], "ok": 0, "failed": 1}
n = rs.merge(prev, videos={"s1": {"ok": True, "file": "x", "report": {}}}, failures=[])
check("videos[s1].ok == True", n["videos"]["s1"]["ok"] is True, n["videos"]["s1"])
check("failures 已清空", n["failures"] == [], n["failures"])
check("failed == 0", n["failed"] == 0, n["failed"])
check("ok == 1", n["ok"] == 1, n["ok"])

print("=== R2: 本轮仍失败，只保留一条 failure ===")
n2 = rs.merge(prev, videos={"s1": {"ok": False, "file": None, "report": {}}},
               failures=[{"slug": "s1", "error": "verify failed"}])
check("failure 条数 == 1", len(n2["failures"]) == 1, n2["failures"])
check("failed == 1", n2["failed"] == 1, n2["failed"])

print("=== R3: 合并新 slug 不丢旧条目 ===")
n3 = rs.merge(prev, videos={"s2": {"ok": True, "file": "y", "report": {}}}, failures=[])
check("s1 与 s2 都在", {"s1", "s2"} <= set(n3["videos"]), sorted(n3["videos"]))
check("s1 的旧 failure 仍在（未被本轮覆盖）",
      any(f.get("slug") == "s1" for f in n3["failures"]), n3["failures"])

print("=== R4: 顶层计数从 videos 重算，不漂移 ===")
n4 = rs.merge(prev, videos={"s1": {"ok": True, "file": "x", "report": {}}}, failures=[])
expect_ok = sum(1 for e in n4["videos"].values() if e.get("ok"))
check("ok == videos 中 ok 的数量", n4["ok"] == expect_ok, (n4["ok"], expect_ok))
check("failed == failures 长度", n4["failed"] == len(n4["failures"]),
      (n4["failed"], len(n4["failures"])))

print("=== R5: update() 落盘并可回读 ===")
tmp = os.path.join(tempfile.mkdtemp(prefix="rs_"), "rep.json")
rs.update(tmp, videos={"a": {"ok": True, "file": "fa", "report": {}}}, failures=[])
rs.update(tmp, videos={"b": {"ok": True, "file": "fb", "report": {}}}, failures=[])
with open(tmp, encoding="utf-8") as f:
    back = json.load(f)
check("两次 update 后含 a 和 b", {"a", "b"} <= set(back["videos"]), sorted(back["videos"]))
check("ok == 2", back["ok"] == 2, back["ok"])
check("failed == 0", back["failed"] == 0, back["failed"])

print()
if FAILED:
    for f in FAILED:
        print("FAIL:", f)
    print("RESULT: FAIL (%d)" % len(FAILED))
    sys.exit(1)
print("RESULT: PASS")
