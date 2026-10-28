"""Regression tests for socks5 support in harvest_events.py.

urllib cannot speak SOCKS5: ProxyHandler with a socks5:// URL connects
straight to the proxy port and gets reset (WinError 10054).  harvest_events
therefore tunnels by hand when given a socks5 proxy.

These tests pin the endpoint parsing and the handler selection -- no network
access required.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))
import harvest_events as he  # noqa: E402

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s: got %r, want %r" % (name, got, want))
        print("FAIL %s: got %r, want %r" % (name, got, want))
    else:
        print("PASS %s -> %r" % (name, got))


def main():
    # --- socks endpoint parsing
    check("plain socks5", he._socks_endpoint("socks5://127.0.0.1:7890"),
          ("127.0.0.1", 7890))
    check("socks5h scheme", he._socks_endpoint("socks5h://127.0.0.1:7890"),
          ("127.0.0.1", 7890))
    check("uppercase scheme", he._socks_endpoint("SOCKS5://127.0.0.1:7890"),
          ("127.0.0.1", 7890))
    check("with credentials",
          he._socks_endpoint("socks5://user:pass@127.0.0.1:7890"),
          ("127.0.0.1", 7890))
    check("trailing slash", he._socks_endpoint("socks5://127.0.0.1:7890/"),
          ("127.0.0.1", 7890))
    check("default port", he._socks_endpoint("socks5://127.0.0.1"),
          ("127.0.0.1", 1080))

    # --- http proxy must NOT be treated as socks
    check("http proxy is not socks",
          he._socks_endpoint("http://127.0.0.1:39411"), None)
    check("none is not socks", he._socks_endpoint(None), None)
    check("empty is not socks", he._socks_endpoint(""), None)

    # --- the socks handler must exist and carry the endpoint
    h = he._Socks5ProxyHandler("127.0.0.1", 7890)
    check("handler keeps host", h.socks_host, "127.0.0.1")
    check("handler keeps port", h.socks_port, 7890)

    # --- fetch() must not blow up on a socks proxy argument: it should
    # attempt the tunnel (and fail with a network error here, since this
    # test runs without a VPN), never with a TypeError from a bad handler.
    try:
        he.fetch("http://127.0.0.1:1/", timeout=1, retries=0,
                 proxy="socks5://127.0.0.1:1")
        check("socks fetch raises", False, True)
    except RuntimeError:
        check("socks fetch raises RuntimeError (not TypeError)", True, True)
    except TypeError as exc:
        check("socks fetch raises RuntimeError (not TypeError)", str(exc), True)

    if FAILURES:
        print("\nRESULT: FAIL (%d)" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
