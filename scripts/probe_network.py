"""Diagnose which network egress can reach a target host.

Run this BEFORE attempting any download.  It answers three questions:
  1. Is the target reachable through the ambient HTTP_PROXY? (often a whitelist)
  2. Is it reachable through a local SOCKS5 proxy (Clash etc.)?
  3. Do we need an HTTP->SOCKS5 bridge for ffmpeg?

Usage
-----
    python probe_network.py https://player.vimeo.com/ https://vimeo.com/
    python probe_network.py --json https://vimeo.com/

Auto-discovers candidate SOCKS5 ports from the Windows registry and netstat.
"""
import argparse
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

CANDIDATE_PORTS = [7890, 1080, 10808, 10809, 20171, 33210, 7891, 8888]


def _run_text(cmd, timeout=25):
    """Run a command and decode output as text, tolerating non-UTF8 bytes."""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    raw = p.stdout or b""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk", "latin-1", errors_mode()):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "ignore")


def errors_mode():
    return "ignore"


def win_registry_socks_ports():
    """Read ProxyServer from the Windows registry (Chrome usually uses SOCKS5 here).

    Uses winreg directly -- shelling out to reg.exe may be blocked by security policy.
    Handles both forms:
        socks5://127.0.0.1:7890        (single proxy, no '=' separator)
        socks5=127.0.0.1:7890;http=... (per-protocol, '=' separated)
    """
    ports = []
    try:
        import winreg
    except ImportError:
        return ports
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            val, _ = winreg.QueryValueEx(k, "ProxyServer")
    except (OSError, ImportError):
        return ports
    if not val:
        return ports
    val = str(val)
    for part in val.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            _, _, p = part.partition("=")
        else:
            p = part
        # strip scheme, keep the port: socks5://127.0.0.1:7890 -> 7890
        m = re.search(r":(\d+)\s*$", p)
        if m:
            ports.append((int(m.group(1)), val))
    return ports


def listening_ports():
    """Return set of ports that are LISTENING on 127.0.0.1."""
    found = set()
    out = _run_text(["netstat", "-ano"], timeout=25)
    for line in (out or "").splitlines():
        if "LISTENING" not in line.upper():
            continue
        m = re.search(r"127\.0\.0\.1:(\d+)", line)
        if m:
            found.add(int(m.group(1)))
    return found


def probe_socks(port, host="127.0.0.1", timeout=6):
    """Return True if port speaks SOCKS5 (we send a greeting and read the reply)."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        data = s.recv(2)
        return len(data) >= 2 and data[0] == 5
    except OSError:
        return False
    finally:
        s.close()


def http_probe(url, proxy=None, timeout=20):
    """GET url (headers only) and report the HTTP status code."""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, "http-error"
    except urllib.error.URLError as e:
        return 0, str(getattr(e, "reason", e))[:120]
    except (OSError, ssl.SSLError) as e:
        return 0, str(e)[:120]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:120]


def main():
    ap = argparse.ArgumentParser(description="Probe network egress for a target host")
    ap.add_argument("urls", nargs="+", help="URLs to test")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--socks-port", type=int, help="force a specific SOCKS5 port")
    ap.add_argument("--ambient-only", action="store_true", help="skip SOCKS discovery")
    args = ap.parse_args()

    report = {"targets": [], "socks_ports": [], "notes": []}

    # 1) ambient HTTP proxy (env vars)
    ambient = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    report["ambient_proxy"] = ambient or None

    # 2) discover SOCKS5 ports
    socks_candidates = set()
    for port, val in win_registry_socks_ports():
        socks_candidates.add(port)
        report["notes"].append("registry ProxyServer=%s -> candidate port %d" % (val, port))
    if args.socks_port:
        socks_candidates.add(args.socks_port)
    if not args.ambient_only:
        for p in listening_ports():
            if p in CANDIDATE_PORTS:
                socks_candidates.add(p)
        socks_candidates.update(p for p in CANDIDATE_PORTS if p in listening_ports())

    for port in sorted(socks_candidates):
        if probe_socks(port):
            report["socks_ports"].append(port)
    report["notes"].append("socks5 ports confirmed: %s" % (report["socks_ports"] or "none"))

    # 3) test each target through each route
    for url in args.urls:
        entry = {"url": url, "routes": {}}
        code, err = http_probe(url, proxy=ambient or None)
        entry["routes"]["ambient_http_proxy"] = {"code": code, "error": err}
        for port in report["socks_ports"]:
            # curl supports socks5; urllib does not, so shell out
            cmd = ["curl", "-s", "--max-time", "20", "--socks5", "127.0.0.1:%d" % port,
                   "-o", os.devnull, "-w", "%{http_code}", url]
            out = _run_text(cmd, timeout=30).strip()
            entry["routes"]["socks5_%d" % port] = {"code": int(out) if out.isdigit() else 0}
        report["targets"].append(entry)

    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
        return 0

    # human-readable summary
    print("ambient HTTP_PROXY : %s" % (report["ambient_proxy"] or "(none)"))
    print("socks5 ports OK    : %s" % (report["socks_ports"] or "(none found)"))
    print()
    for entry in report["targets"]:
        print(entry["url"])
        for name, res in entry["routes"].items():
            code = res.get("code")
            flag = "OK " if code and code < 400 else "BLOCKED"
            extra = res.get("error", "")
            print("   %-22s %s  code=%s %s" % (name, flag, code, extra))
        print()

    # actionable guidance
    blocked_by_ambient = any(
        entry["routes"]["ambient_http_proxy"].get("code", 0) in (0, 403, 407)
        for entry in report["targets"])
    socks_works = any(
        any(k.startswith("socks5_") and v.get("code") and v["code"] < 400
            for k, v in entry["routes"].items())
        for entry in report["targets"])
    if blocked_by_ambient and socks_works:
        print("=> ACTION: ambient proxy blocks the target but SOCKS5 works.")
        print("   1) start scripts/socks_bridge.py (HTTP->SOCKS5 bridge)")
        print("   2) give ffmpeg/yt-dlp --proxy http://127.0.0.1:<bridge_port>")
    elif blocked_by_ambient:
        print("=> ACTION: no working route found. Check Clash/VPN is running, or use a mirror.")
    else:
        print("=> ACTION: ambient proxy is sufficient; no bridge needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
