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


def _ambient_proxy():
    """Ambient HTTP(S) proxy from the environment, scheme-normalized."""
    val = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
           or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")
    if val and "://" not in val:
        val = "http://" + val
    return val


def _socks_candidates():
    """SOCKS5 candidate ports: registry + netstat listeners + well-known ports."""
    ports = set(p for p, _v in win_registry_socks_ports())
    listening = listening_ports()
    ports.update(p for p in CANDIDATE_PORTS if p in listening)
    return ports


def _curl_code_via_socks(port, url, timeout=30):
    cmd = ["curl", "-s", "--max-time", "20", "--socks5", "127.0.0.1:%d" % port,
           "-o", os.devnull, "-w", "%{http_code}", url]
    out = _run_text(cmd, timeout=timeout).strip()
    return int(out) if out.isdigit() else 0


def _socks_reachable(urls, port):
    """True when this SOCKS5 port reaches EVERY target URL."""
    for url in urls:
        code = _curl_code_via_socks(port, url)
        if not code or code >= 400:
            return False
    return True


def _decide(ambient, ambient_all_ok, socks_working):
    """Shared route decision.  Returns (proxy_or_None, mode)."""
    if ambient_all_ok:
        return (ambient or None, "ambient" if ambient else "direct")
    if socks_working:
        return ("socks5://127.0.0.1:%d" % socks_working[0], "socks5")
    return (None, "blocked")


def resolve_auto_proxy(urls, timeout=20):
    """Probe every egress route for the given URLs and pick a --proxy value.

    Returns (proxy_or_None, mode, detail).  Modes:
      direct   -- no ambient proxy set; the target is reachable bare
      ambient  -- the ambient HTTP(S)_PROXY reaches it.  Returned explicitly
                  because urllib-based callers build ProxyHandler({}) (which
                  DISABLES env proxies) and must pass it; yt-dlp/ffmpeg take
                  an explicit http:// proxy just as well.
      socks5   -- ambient blocked, but a local SOCKS5 reaches every target
      blocked  -- nothing works; detail explains what failed
    """
    ambient = _ambient_proxy()
    ambient_all_ok = True
    for url in urls:
        code, _err = http_probe(url, proxy=ambient or None, timeout=timeout)
        if not code or code >= 400:
            ambient_all_ok = False
            break
    if ambient_all_ok:
        proxy, mode = _decide(ambient, True, None)
        return (proxy, mode, "")
    socks_working = []
    for port in sorted(_socks_candidates()):
        if probe_socks(port) and _socks_reachable(urls, port):
            socks_working.append(port)
    proxy, mode = _decide(ambient, False, socks_working)
    if mode == "blocked":
        return (None, "blocked",
                "ambient route %s failed and no working local SOCKS5 found"
                % (ambient or "(none set)"))
    return (proxy, mode, "")


def _fetch_text(url, proxy, timeout=20):
    """GET a page body as text (up to 2 MB) through the given proxy or bare."""
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.read(2_000_000).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""


def classify_platform_urls(page_url, timeout=20):
    """Fetch the page over the ambient route (or bare) and return probe URLs
    for every video platform its iframes reference.

    Rationale: the page origin and the video CDN often have DIFFERENT
    reachability (e.g. cdisc.org reachable via a whitelist proxy that blocks
    player.vimeo.com).  Choosing a route for the page alone would break the
    actual video download, so auto-routing must probe the platform domains
    too.  An empty body (page unfetchable) yields [] and the caller just
    probes the page URL itself.
    """
    ambient = _ambient_proxy()
    body = _fetch_text(page_url, ambient or None, timeout)
    probes = []
    if not body:
        return probes
    if "player.vimeo.com" in body or "vimeo.com/video" in body:
        probes += ["https://player.vimeo.com/", "https://vimeo.com/"]
    if "youtube.com/embed" in body or "youtube-nocookie" in body or "youtu.be/" in body:
        probes += ["https://www.youtube.com/"]
    if "wistia" in body:
        probes += ["https://fast.wistia.com/"]
    return probes


def auto_proxy_arg(urls, timeout=20):
    """Resolve `--proxy auto`: probe, print the choice, exit with diagnosis
    when nothing works.  Returns the value to hand to the caller's --proxy
    (None means "no explicit proxy needed")."""
    proxy, mode, detail = resolve_auto_proxy(urls, timeout=timeout)
    if mode == "direct":
        print("auto proxy: reachable directly, no proxy needed")
    elif mode == "ambient":
        print("auto proxy: ambient proxy works -> %s" % proxy)
    elif mode == "socks5":
        print("auto proxy: ambient blocked, SOCKS5 works -> %s" % proxy)
    else:
        raise SystemExit(
            "auto proxy: no working route to %s (%s)\n"
            "  NOTE: if only the video CDN is blocked, the page may still be "
            "fetchable -- the download needs a route that reaches BOTH.\n"
            "  1) start your VPN / Clash (SOCKS5 listener)\n"
            "  2) or export HTTPS_PROXY to a working HTTP proxy\n"
            "  3) or run scripts/probe_network.py <url> for a full diagnosis"
            % (", ".join(urls), detail))
    return proxy


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
        socks_candidates.update(_socks_candidates())

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
            code = _curl_code_via_socks(port, url)
            entry["routes"]["socks5_%d" % port] = {"code": code}
        report["targets"].append(entry)

    # machine-readable recommendation (--json), consistent with resolve_auto_proxy
    def _code_ok(v):
        c = v.get("code", 0) if isinstance(v, dict) else 0
        return bool(c) and c < 400

    ambient_all_ok = all(
        _code_ok(entry["routes"].get("ambient_http_proxy", {}))
        for entry in report["targets"])
    socks_working = [port for port in report["socks_ports"]
                     if all(_code_ok(entry["routes"].get("socks5_%d" % port, {}))
                            for entry in report["targets"])]
    rec_proxy, rec_mode = _decide(ambient, ambient_all_ok, socks_working)
    report["recommendation"] = {"proxy": rec_proxy, "mode": rec_mode}
    if rec_mode == "blocked":
        report["recommendation"]["detail"] = "no route reaches every target"

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
        # Which SOCKS5 port actually worked?  Report it by name so the
        # caller can use it verbatim.
        socks_port = None
        for entry in report["targets"]:
            for k, v in entry["routes"].items():
                if k.startswith("socks5_") and v.get("code") and v["code"] < 400:
                    socks_port = k.split("_", 1)[1]
                    break
            if socks_port:
                break
        print("=> ACTION: ambient proxy blocks the target but SOCKS5 works.")
        if socks_port:
            print("   1) simplest: --proxy socks5://127.0.0.1:%s" % socks_port)
            print("      (yt-dlp speaks SOCKS5 natively; the script then uses the")
            print("       native downloader and cleans up .part-FragN files)")
            print("   2) if ffmpeg must fetch too (e.g. --download-sections),")
            print("      start scripts/socks_bridge.py and add")
            print("      --mux-proxy http://127.0.0.1:<bridge_port>")
        else:
            print("   1) start scripts/socks_bridge.py (HTTP->SOCKS5 bridge)")
            print("   2) give ffmpeg/yt-dlp --proxy http://127.0.0.1:<bridge_port>")
    elif blocked_by_ambient:
        print("=> ACTION: no working route found. Check Clash/VPN is running, or use a mirror.")
    else:
        print("=> ACTION: ambient proxy is sufficient; no proxy needed.")

    if rec_mode == "direct":
        print("=> AUTO: no explicit proxy needed (direct)")
    elif rec_mode == "ambient":
        print("=> AUTO: --proxy %s   # ambient proxy, pass explicitly" % rec_proxy)
    elif rec_mode == "socks5":
        print("=> AUTO: --proxy %s" % rec_proxy)
    else:
        print("=> AUTO: no working route; --proxy auto will exit with a diagnosis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
