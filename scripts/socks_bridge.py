"""HTTP CONNECT proxy that bridges to a local SOCKS5 proxy.

Why this exists
---------------
ffmpeg (and therefore yt-dlp's ``--downloader ffmpeg``) has NO SOCKS5 support.
On this machine the working egress to video CDNs is a Clash SOCKS5 proxy
(127.0.0.1:7890), while the environment's HTTP_PROXY is a whitelist proxy
that blocks most video hosts.  So we expose a local HTTP proxy that tunnels
CONNECT requests through SOCKS5, and point ffmpeg at that.

Usage
-----
    python socks_bridge.py [--listen-port 39411] [--socks-host 127.0.0.1] [--socks-port 7890]

Then use it as the proxy for ffmpeg / yt-dlp:
    yt-dlp --proxy http://127.0.0.1:39411 ...

Notes
-----
* Low ports (<1024) may raise PermissionError on Windows; use a high port.
* Run it with a long timeout / background execution -- it blocks forever.
* Verify with:  curl -x http://127.0.0.1:<port> -o /dev/null -w "%{http_code}" https://example.com
"""
import argparse
import select
import socket
import sys
import threading


def socks_connect(dst_host, dst_port, socks_host, socks_port, timeout=30):
    """Open a SOCKS5 CONNECT tunnel to (dst_host, dst_port). Returns a socket."""
    s = socket.create_connection((socks_host, socks_port), timeout=timeout)
    s.settimeout(timeout)

    # greeting: version 5, 1 method (no auth)
    s.sendall(b"\x05\x01\x00")
    resp = s.recv(2)
    if len(resp) < 2 or resp[0] != 5:
        s.close()
        raise OSError("socks5 handshake failed: %r" % (resp,))

    # connect request: domain name addressing
    try:
        hb = dst_host.encode("idna")
    except UnicodeError:
        hb = dst_host.encode()
    if len(hb) > 255:
        s.close()
        raise OSError("hostname too long for socks5")
    req = b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + int(dst_port).to_bytes(2, "big")
    s.sendall(req)

    head = s.recv(4)
    if len(head) < 4 or head[1] != 0:
        s.close()
        raise OSError("socks5 connect rejected (code=%r)" % (head[1] if len(head) > 1 else None,))
    atyp = head[3]
    if atyp == 1:
        s.recv(4)
    elif atyp == 3:
        s.recv(s.recv(1)[0])
    elif atyp == 4:
        s.recv(16)
    s.recv(2)  # bound port
    s.settimeout(None)
    return s


def _pump(a, b):
    """Relay bytes both ways.

    The select() timeout is a *stall* detector, not a lifetime: an idle tunnel
    (e.g. while ffmpeg is muxing) must NOT be torn down, otherwise long
    downloads break on a 30s pause.  On timeout we keep waiting; only a real
    EOF or error closes the tunnel.
    """
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 300)
            if not r:
                continue  # idle, not dead -- keep the tunnel open
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def handle(client, socks_host, socks_port):
    try:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            buf += chunk
            if len(buf) > 65536:
                client.close()
                return
        first = buf.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")[0]
        parts = first.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            client.close()
            return
        host, _, port = parts[1].partition(":")
        remote = socks_connect(host, int(port or 443), socks_host, socks_port)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        _pump(client, remote)
    except Exception:
        try:
            client.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="HTTP CONNECT -> SOCKS5 bridge")
    ap.add_argument("--listen-port", type=int, default=39411)
    ap.add_argument("--socks-host", default="127.0.0.1")
    ap.add_argument("--socks-port", type=int, default=7890)
    ap.add_argument("--probe", action="store_true",
                    help="Exit after verifying the SOCKS5 upstream is reachable")
    args = ap.parse_args()

    # fail fast with a clear message if the SOCKS5 upstream is down
    try:
        probe = socket.create_connection((args.socks_host, args.socks_port), timeout=5)
        probe.close()
    except OSError as e:
        print("ERROR: SOCKS5 upstream %s:%d unreachable: %s"
              % (args.socks_host, args.socks_port, e), file=sys.stderr)
        print("Hint: find the real proxy port (Windows registry ProxyServer, "
              "or netstat -ano | grep LISTENING), then re-run with --socks-port.",
              file=sys.stderr)
        return 2

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", args.listen_port))
    except PermissionError:
        print("ERROR: cannot bind port %d (privileged/denied). Use a high port."
              % args.listen_port, file=sys.stderr)
        return 2
    except OSError as e:
        print("ERROR: cannot bind port %d: %s" % (args.listen_port, e), file=sys.stderr)
        return 2
    srv.listen(64)

    print("socks_bridge: http://127.0.0.1:%d  ->  socks5://%s:%d"
          % (args.listen_port, args.socks_host, args.socks_port), flush=True)
    if args.probe:
        srv.close()
        return 0
    try:
        while True:
            client, _ = srv.accept()
            threading.Thread(target=handle,
                             args=(client, args.socks_host, args.socks_port),
                             daemon=True).start()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
