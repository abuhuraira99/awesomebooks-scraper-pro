"""``python -m awesomebooks_pro`` — start the dashboard."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import urllib.request
import webbrowser
from dataclasses import replace

from . import __version__
from .config import Settings
from .net.client import CURL_CFFI_ERROR, HAS_CURL_CFFI


def _disable_console_quickedit() -> None:
    """Stop an accidental click in the console window from freezing the scraper.

    With Windows' QuickEdit mode on, clicking inside a console selects text and
    blocks every thread that writes to it until Enter is pressed. That would
    silently stall an unattended job for hours.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            enable_extended_flags, enable_quick_edit = 0x0080, 0x0040
            kernel32.SetConsoleMode(handle, (mode.value | enable_extended_flags) & ~enable_quick_edit)
    except Exception:
        pass  # not a real console (e.g. output redirected): nothing to do


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host if host not in ("0.0.0.0", "::") else "0.0.0.0", port))
            return True
        except OSError:
            return False


def _already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
            return json.loads(resp.read()).get("ok") is True
    except Exception:
        return False


PORT_SEARCH = 20  # when the default port is taken, try this many ports after it


def _pick_port(host: str, preferred: int, *, explicit: bool) -> tuple[int, bool]:
    """Return ``(port, already_running)``.

    Another copy of the dashboard on any candidate port is reused. Otherwise the
    preferred port is used, or (unless the user asked for that exact port) the
    next free one, so a clash with other software never stops the app starting.
    """
    candidates = [preferred] if explicit else list(range(preferred, min(preferred + PORT_SEARCH, 65535) + 1))
    for port in candidates:
        if not _port_free(host, port) and _already_running(port):
            return port, True
    for port in candidates:
        if _port_free(host, port):
            return port, False
    return preferred, False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="awesomebooks_pro", description="AwesomeBooks Scraper Pro dashboard")
    parser.add_argument("--host", help="interface to bind (default 127.0.0.1; use 0.0.0.0 on a VPS)")
    parser.add_argument("--port", type=int, help="port (default 7642; the next free port is used if it is busy)")
    parser.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args(argv)

    settings = Settings()
    if args.host:
        settings = replace(settings, host=args.host)
    explicit = bool(args.port or os.getenv("ABP_PORT"))
    if args.port:
        settings = replace(settings, port=args.port)

    preferred = settings.port
    port, running = _pick_port(settings.host, preferred, explicit=explicit)
    url = f"http://127.0.0.1:{port}"
    if running:
        print(f"\n  AwesomeBooks Scraper Pro is already running. Opening {url}\n")
        if args.open:
            webbrowser.open(url)
        return
    if not _port_free(settings.host, port):
        print(
            f"\n  Port {preferred} is being used by another program.\n"
            f"  Close that program, or start on another port:  run.bat --port {preferred + 1}\n"
        )
        sys.exit(1)
    if port != preferred:
        print(f"\n  Port {preferred} is used by another program, so port {port} is used instead.")
    settings = replace(settings, port=port)

    _disable_console_quickedit()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("waitress", "werkzeug", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not args.debug:
        # Per-request details (identity rotations, retries) stay out of the console; job events stay in.
        logging.getLogger("awesomebooks_pro.net").setLevel(logging.WARNING)

    from .web import create_app

    app = create_app(settings)
    transport = "real browser fingerprints (curl_cffi)" if HAS_CURL_CFFI else "requests (install curl_cffi for best results)"
    print(
        f"\n  AwesomeBooks Scraper Pro v{__version__}\n"
        f"  Dashboard   {url}\n"
        f"  Excel files {settings.resolve_export_dir()}\n"
        f"  Prices      ship to {settings.ship_to}, in {settings.currency}\n"
        f"  Transport   {transport}\n\n"
        "  Keep this window open while a job runs. Closing it stops the scraper;\n"
        "  the job resumes automatically the next time you start it.\n"
    )
    if not HAS_CURL_CFFI:
        print(
            "  NOTE: browser fingerprints are unavailable, so plain requests are used instead.\n"
            f"        Reason: {CURL_CFFI_ERROR}\n"
            "        Fix: move this folder to a short path such as C:\\AwesomeBooks Scraper Pro, then run setup.bat.\n"
        )
    if settings.host in ("0.0.0.0", "::") and not settings.auth_user:
        print("  WARNING: reachable from the network without a password. Set ABP_AUTH_USER / ABP_AUTH_PASSWORD.\n")
    if args.open:
        webbrowser.open(url)

    from waitress import serve

    serve(app, host=settings.host, port=settings.port, threads=8, ident="awesomebooks-scraper-pro")


if __name__ == "__main__":
    main()
