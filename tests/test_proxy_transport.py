"""Regression tests for protocol-aware transport selection.

The bug this guards against: the skill used to hardcode
``--downloader ffmpeg``, which only speaks HTTP.  On a machine whose only
proxy is SOCKS5 (a VPN client such as spv.exe on port 7890), ffmpeg silently
ignores the socks5 proxy, connects directly, and the CDN answers 403.

So the protocol is NOT a cosmetic setting -- it decides which process does
the fetching.  These tests pin that mapping down.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))
import download_webinar as dw  # noqa: E402

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s: got %r, want %r" % (name, got, want))
        print("FAIL %s: got %r, want %r" % (name, got, want))
    else:
        print("PASS %s -> %r" % (name, got))


def main():
    # --- socks5 only: yt-dlp must fetch, because ffmpeg cannot speak socks5
    check("socks5 -> native", dw.pick_transport("socks5://127.0.0.1:7890")[0],
          "native")
    check("socks5 -> no mux proxy needed",
          dw.pick_transport("socks5://127.0.0.1:7890")[1], False)

    # --- socks5 + explicit mux proxy: yt-dlp fetches, ffmpeg mux gets HTTP
    check("socks5 + mux -> native",
          dw.pick_transport("socks5://127.0.0.1:7890",
                            "http://127.0.0.1:39411")[0], "native")
    check("socks5 + mux -> mux proxy required",
          dw.pick_transport("socks5://127.0.0.1:7890",
                            "http://127.0.0.1:39411")[1], True)

    # --- http proxy: ffmpeg may fetch, single-file output, no fragments
    check("http -> ffmpeg", dw.pick_transport("http://127.0.0.1:39411")[0],
          "ffmpeg")
    check("http -> no mux proxy",
          dw.pick_transport("http://127.0.0.1:39411")[1], False)

    # --- no proxy at all: direct, ffmpeg downloader is fine
    check("none -> ffmpeg", dw.pick_transport(None)[0], "ffmpeg")
    check("none -> no mux proxy", dw.pick_transport(None)[1], False)

    # --- case-insensitive scheme
    check("SOCKS5 uppercase -> native",
          dw.pick_transport("SOCKS5://127.0.0.1:7890")[0], "native")

    # --- an http mux proxy alone must NOT flip a socks5 run to ffmpeg
    check("socks5 with unrelated mux -> still native",
          dw.pick_transport("socks5://127.0.0.1:7890",
                            "http://127.0.0.1:39411")[0], "native")

    # --- cleanup_fragments must only remove .part-FragN files
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="fragtest_")
    try:
        keep = [
            "video.mp4",              # final output
            "video.mp4.part",        # partial container
            "video.mp4.ytdl",        # resume state
            "video.mp4.part-Frag12.part",   # a real fragment
            "notes.txt",
        ]
        for name in keep:
            with open(os.path.join(tmp, name), "wb") as f:
                f.write(b"x")
        removed = dw.cleanup_fragments(tmp)
        left = sorted(os.listdir(tmp))
        check("frag removed count", len(removed), 1)
        check("frag removed name", removed[0], "video.mp4.part-Frag12.part")
        check("other files untouched", left,
              ["notes.txt", "video.mp4", "video.mp4.part", "video.mp4.ytdl"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- cleanup on a missing directory must not raise
    check("cleanup on missing dir", dw.cleanup_fragments("/nonexistent/x"), [])

    # --- urllib cannot speak socks5; detect_page must not use it there.
    # A wrong proxy scheme makes urllib connect straight to the proxy port
    # and get reset (WinError 10054), which used to abort the probe.
    import urllib.request
    real_build_opener = urllib.request.build_opener
    calls = {"urllib": 0, "ytdlp": 0}

    def fake_build_opener(*a, **kw):
        calls["urllib"] += 1
        return real_build_opener(*a, **kw)

    real_ytdlp = dw._fetch_page_ytdlp
    real_urllib = dw._fetch_page_urllib

    def fake_urllib(url, timeout, proxy):
        calls["urllib"] += 1
        return "<html>123456</html>"

    def fake_ytdlp(url, timeout, proxy):
        calls["ytdlp"] += 1
        return "Vimeo", "123456"

    dw._fetch_page_urllib = fake_urllib
    dw._fetch_page_ytdlp = fake_ytdlp
    try:
        r = dw.detect_page("http://x/y", timeout=5, proxy="socks5://127.0.0.1:7890")
        check("socks5 probe uses yt-dlp", calls["ytdlp"], 1)
        check("socks5 probe skips urllib", calls["urllib"], 0)
        check("socks5 probe finds vimeo id", r["vimeo_id"], "123456")

        calls["urllib"] = calls["ytdlp"] = 0
        r = dw.detect_page("http://x/y", timeout=5, proxy="http://127.0.0.1:39411")
        check("http probe uses urllib", calls["urllib"], 1)
        check("http probe skips yt-dlp", calls["ytdlp"], 0)
    finally:
        dw._fetch_page_urllib = real_urllib
        dw._fetch_page_ytdlp = real_ytdlp

    # --- a failing probe must NOT raise (it is only advisory)
    def boom(url, timeout, proxy):
        raise OSError("probe exploded")

    dw._fetch_page_ytdlp = boom
    try:
        r = dw.detect_page("http://x/y", timeout=5, proxy="socks5://127.0.0.1:7890")
        check("failing probe returns empty", r["vimeo_id"], None)
    finally:
        dw._fetch_page_ytdlp = real_ytdlp

    # --- the report key fallback must be derivable from the URL, not from
    # the --out directory name (the directory is caller-chosen and never
    # matches a manifest slug).
    check("slug from url",
          dw._slug_from_url("https://x.org/events/webinar/my-topic"),
          "my-topic")
    check("slug strips query",
          dw._slug_from_url("https://x.org/events/webinar/foo?a=1#z"), "foo")
    check("slug strips trailing slash",
          dw._slug_from_url("https://x.org/events/webinar/foo/"), "foo")
    check("slug lowercases",
          dw._slug_from_url("https://x.org/events/webinar/Mixed-Case"), "mixed-case")

    if FAILURES:
        print("\nRESULT: FAIL (%d)" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
