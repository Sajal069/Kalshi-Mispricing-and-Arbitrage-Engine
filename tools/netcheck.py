"""Layered connectivity diagnostic for the Kalshi API.

Run when ``kima auth`` reports UNREACHABLE but ``curl`` to the same URL works.
That combination means the problem is local to Python rather than to the
network, and the useful question is *which layer* breaks. This walks up the
stack and reports each one separately:

    1. DNS
    2. raw TCP to :443
    3. TLS handshake (and which certificate store is in use)
    4. urllib      -- stdlib, no third-party HTTP code
    5. httpx sync  -- the library the recorder uses
    6. httpx async -- the code path the recorder actually takes

The first layer that fails is the answer. Common outcomes:

* everything fails, curl works  -> per-application firewall rule on python.exe,
  or a proxy variable Python honours and curl does not
* TLS fails, TCP succeeds       -> TLS interception (antivirus/corporate MITM);
  Python validates against certifi while curl uses the Windows store
* only httpx async fails        -> an event-loop problem, worth reporting

Usage:
    python tools/netcheck.py
    python tools/netcheck.py --host api.elections.kalshi.com --path /trade-api/v2/exchange/status
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
import time

DEFAULT_HOST = "api.elections.kalshi.com"
DEFAULT_PATH = "/trade-api/v2/exchange/status"
TIMEOUT = 15.0


def _ok(label: str, detail: str = "", t0: float | None = None) -> None:
    ms = f"  [{(time.monotonic() - t0) * 1000:.0f} ms]" if t0 else ""
    print(f"  OK    {label:<22}{detail}{ms}", flush=True)


def _fail(label: str, exc: BaseException, t0: float | None = None) -> None:
    ms = f"  [{(time.monotonic() - t0) * 1000:.0f} ms]" if t0 else ""
    msg = str(exc) or exc.__class__.__name__
    print(f"  FAIL  {label:<22}{type(exc).__name__}: {msg}{ms}", flush=True)


def check_env() -> None:
    print("\n=== Environment ===", flush=True)
    print(f"  python                {sys.version.split()[0]} ({sys.executable})", flush=True)
    proxy_vars = {k: v for k, v in os.environ.items()
                  if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                   "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE")}
    if proxy_vars:
        for k, v in sorted(proxy_vars.items()):
            print(f"  {k:<22}{v}", flush=True)
        print("  NOTE: Python honours these. curl may not honour the same set, which is\n"
              "        the single most common reason curl works and Python does not.",
              flush=True)
    else:
        print("  proxy/CA env vars     none set", flush=True)
    try:
        import certifi
        print(f"  certifi bundle        {certifi.where()}", flush=True)
    except ImportError:
        print("  certifi bundle        not installed (using system store)", flush=True)


def check_dns(host: str) -> list[str]:
    print("\n=== 1. DNS ===", flush=True)
    t0 = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        _fail("resolve", exc, t0)
        return []
    addrs, families = [], set()
    for family, _t, _p, _c, sockaddr in infos:
        addrs.append(sockaddr[0])
        families.add("IPv6" if family == socket.AF_INET6 else "IPv4")
    uniq = sorted(set(addrs))
    _ok("resolve", f"{', '.join(sorted(families))}: {', '.join(uniq[:4])}", t0)
    if "IPv6" in families:
        print("  NOTE: AAAA records present. If IPv6 egress is broken, connects hang\n"
              "        until timeout while an IPv4 attempt would succeed.", flush=True)
    return uniq


def check_tcp(host: str, addrs: list[str]) -> None:
    print("\n=== 2. Raw TCP to :443 ===", flush=True)
    for addr in addrs[:4]:
        t0 = time.monotonic()
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        try:
            s.connect((addr, 443))
            _ok(f"connect {addr}", "", t0)
        except Exception as exc:
            _fail(f"connect {addr}", exc, t0)
        finally:
            s.close()


def check_tls(host: str) -> None:
    print("\n=== 3. TLS handshake ===", flush=True)
    t0 = time.monotonic()
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                issuer = dict(x[0] for x in cert.get("issuer", ())).get("organizationName", "?")
                _ok("handshake", f"{tls.version()}, issuer={issuer}", t0)
                if "kalshi" not in issuer.lower() and "amazon" not in issuer.lower():
                    print(f"  NOTE: issuer {issuer!r} looks like TLS interception "
                          "(antivirus or corporate proxy).", flush=True)
    except Exception as exc:
        _fail("handshake", exc, t0)


def check_urllib(host: str, path: str) -> None:
    print("\n=== 4. urllib (stdlib) ===", flush=True)
    import urllib.request
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(f"https://{host}{path}",
                                     headers={"User-Agent": "kima-netcheck/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            _ok("GET", f"HTTP {r.status}, {len(r.read())} bytes", t0)
    except Exception as exc:
        _fail("GET", exc, t0)


def check_httpx(host: str, path: str) -> None:
    print("\n=== 5. httpx (sync) ===", flush=True)
    try:
        import httpx
    except ImportError as exc:
        _fail("import", exc)
        return
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=TIMEOUT,
                          headers={"Accept-Encoding": "gzip, deflate"}) as c:
            r = c.get(f"https://{host}{path}")
            _ok("GET", f"HTTP {r.status_code}, {len(r.content)} bytes", t0)
    except Exception as exc:
        _fail("GET", exc, t0)

    print("\n=== 6. httpx (async, the recorder's path) ===", flush=True)
    import asyncio

    async def go() -> None:
        t1 = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(TIMEOUT, connect=TIMEOUT),
                headers={"Accept-Encoding": "gzip, deflate"},
            ) as c:
                r = await c.get(f"https://{host}{path}")
                _ok("GET", f"HTTP {r.status_code}, {len(r.content)} bytes", t1)
        except Exception as exc:
            _fail("GET", exc, t1)

    print(f"  event loop policy     {type(asyncio.get_event_loop_policy()).__name__}",
          flush=True)
    asyncio.run(go())


def check_kima_client(host: str) -> None:
    """Replicate the exact client `kima auth` builds, one variable at a time.

    netcheck's plain async client can succeed while `kima auth` times out, which
    means the difference is in how the client is configured rather than in the
    network. Rather than guess which knob matters, vary them one at a time and
    repeat each three times -- an intermittent failure is a different diagnosis
    from a deterministic one, and only repetition tells them apart.
    """
    print("\n=== 7. Exactly what `kima auth` builds ===", flush=True)
    try:
        import httpx
    except ImportError as exc:
        _fail("import", exc)
        return
    import asyncio

    base = f"https://{host}/trade-api/v2"
    kima_headers = {
        "User-Agent": "kima/0.1 (research)",
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    }

    variants = [
        ("plain, absolute URL", None, {}, "/trade-api/v2/exchange/status"),
        ("base_url only", base, {}, "/exchange/status"),
        ("base_url + kima headers", base, kima_headers, "/exchange/status"),
        ("...and /portfolio/balance", base, kima_headers, "/portfolio/balance"),
    ]

    async def one(label: str, base_url, headers, path) -> None:
        results = []
        for _ in range(3):
            t0 = time.monotonic()
            try:
                kwargs = {"timeout": httpx.Timeout(30.0, connect=15.0), "headers": headers}
                if base_url:
                    kwargs["base_url"] = base_url
                async with httpx.AsyncClient(**kwargs) as c:
                    url = path if base_url else f"https://{host}{path}"
                    r = await c.get(url)
                    results.append(f"{r.status_code} ({(time.monotonic() - t0) * 1000:.0f}ms)")
            except Exception as exc:
                results.append(f"{type(exc).__name__} ({(time.monotonic() - t0) * 1000:.0f}ms)")
        good = sum(1 for r in results if r[:1].isdigit())
        mark = "OK   " if good == 3 else ("FLAKY" if good else "FAIL ")
        print(f"  {mark} {label:<28}{'  |  '.join(results)}", flush=True)

    async def go() -> None:
        for label, b, h, p in variants:
            await one(label, b, h, p)

    asyncio.run(go())
    print("  A 401 on /portfolio/balance is a SUCCESS here: it means the request\n"
          "  reached Kalshi. Only a timeout indicates a transport problem.", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--path", default=DEFAULT_PATH)
    args = ap.parse_args()

    print(f"Diagnosing https://{args.host}{args.path}", flush=True)
    check_env()
    addrs = check_dns(args.host)
    if addrs:
        check_tcp(args.host, addrs)
    check_tls(args.host)
    check_urllib(args.host, args.path)
    check_httpx(args.host, args.path)
    check_kima_client(args.host)
    print("\nThe first failing layer is the cause. If every Python layer fails while\n"
          "curl succeeds, suspect a firewall rule scoped to python.exe or a proxy\n"
          "variable that only Python honours.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
