"""HTTP API + static dashboard."""

from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from . import __version__
from .config import DEFAULT_PACE, PACES, Settings
from .export import export_filename, export_job
from .inputs import UploadError, parse_upload
from .jobs import JobBusyError, JobManager
from .net.client import HAS_CURL_CFFI
from .site import InvalidSectionUrl, section_from_url
from .storage import JOB_ACTIVE_STATES, JOB_RESUMABLE_STATES, CatalogStore, InventoryStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
KINDS = ("catalog", "inventory")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

QUICK_SECTIONS = [
    ("New & Trending", "/new-books"),
    ("Bestsellers", "/books/bestsellers"),
    ("Used", "/used-books"),
    ("Bargain", "/books/bargain"),
    ("Coming Soon", "/books/pre-order"),
    ("Fiction", "/books/category/303/fiction"),
    ("Children's", "/books/category/2766/childrens-books"),
    ("Rare Books", "/rare-books"),
]
CSRF_HEADER = "awesomebooks-pro"


def _json(value: str | None) -> Any:
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _sessions(value: Any, settings: Settings) -> int:
    try:
        return max(1, min(int(value), settings.max_sessions))
    except (TypeError, ValueError):
        return 3


def _pace(value: Any) -> str:
    return value if value in PACES else DEFAULT_PACE


def _limit(value: Any) -> int | None:
    """Optional "max products" box: empty, 0 or nonsense means no limit."""
    try:
        number = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def create_app(settings: Settings | None = None, manager: JobManager | None = None, *, resume_on_start: bool = True) -> Flask:
    settings = settings or Settings()
    if manager is None:
        manager = JobManager(settings, CatalogStore(settings.catalog_db), InventoryStore(settings.inventory_db))
    interrupted = [(kind, job_id) for kind in KINDS for job_id in manager.store(kind).mark_interrupted_jobs()]
    if interrupted and settings.auto_resume and resume_on_start:
        kind, job_id = interrupted[-1]
        timer = threading.Timer(4.0, manager.auto_resume, args=(kind, job_id))
        timer.daemon = True
        timer.start()

    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.extensions["abp_manager"] = manager

    # -- security -------------------------------------------------------
    loopback = settings.host in ("127.0.0.1", "localhost", "::1")
    extra_hosts = {h.strip().lower() for h in settings.allowed_hosts.split(",") if h.strip()}
    allowed_hosts = {"127.0.0.1", "localhost", "::1"} | extra_hosts

    @app.before_request
    def guard():
        # DNS-rebinding protection: while bound to this PC only, the Host header must name this PC.
        raw_host = (request.host or "").lower()
        host = raw_host[1:].split("]", 1)[0] if raw_host.startswith("[") else raw_host.rsplit(":", 1)[0]
        if (loopback or extra_hosts) and host not in allowed_hosts:
            return jsonify({"error": "Host not allowed"}), 403
        # CSRF protection: state-changing calls must come from the dashboard's own scripts.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("X-Requested-With") != CSRF_HEADER:
                return jsonify({"error": "Missing request header"}), 403
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/").split("://", 1)[-1].lower() != raw_host:
                return jsonify({"error": "Cross-origin request blocked"}), 403
        return None

    @app.after_request
    def security_headers(resp: Response) -> Response:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; connect-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https://d32vymxhv9fq6b.cloudfront.net https://www.awesomebooks.com; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'",
        )
        if request.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # -- auth -----------------------------------------------------------
    def requires_auth(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if settings.auth_user:
                auth = request.authorization
                ok = (
                    auth is not None
                    and hmac.compare_digest(auth.username or "", settings.auth_user)
                    and hmac.compare_digest(auth.password or "", settings.auth_password)
                )
                if not ok:
                    return Response(
                        "Authentication required", 401, {"WWW-Authenticate": 'Basic realm="AwesomeBooks Scraper Pro"'}
                    )
            return view(*args, **kwargs)

        return wrapper

    def error(message: str, status: int = 400):
        return jsonify({"error": message}), status

    def store_for(kind: str):
        if kind not in KINDS:
            return None
        return manager.store(kind)

    # -- presentation helpers --------------------------------------------
    def progress(kind: str, job: dict[str, Any]) -> dict[str, Any]:
        store = manager.store(kind)
        job_id = job["id"]
        counts = store.state_counts(job_id)
        stock = store.stock_counts(job_id)
        live = manager.live(kind, job_id)
        running = live is not None
        # Invalid upload rows need no work: keep them out of progress maths.
        total = sum(counts.values()) - counts.get("invalid", 0)
        processed = total - counts.get("pending", 0) - counts.get("processing", 0)
        now = time.time()
        elapsed = float(job.get("active_seconds") or 0)
        if running and job.get("started_at"):
            elapsed += now - job["started_at"]
        # Throughput over the last two minutes of *this* run (never across a pause/restart).
        window_start = max(now - 120.0, job.get("started_at") or now)
        recent = store.completed_since(job_id, window_start) if running else 0
        rate = recent / max(now - window_start, 5.0) if recent else 0.0
        remaining = total - processed
        eta = remaining / rate if rate > 0 and remaining > 0 else None
        status = job["status"]
        if not running and status in (*JOB_ACTIVE_STATES, "stopping"):
            status = "interrupted"  # no thread behind it any more: offer Resume
        if running and status in ("queued",):
            status = "running"
        return {
            "kind": kind,
            "job": job,
            "status": status,
            "running": running,
            "counts": counts,
            "stock": stock,
            "total": total,
            "processed": processed,
            "percent": round(processed / total * 100, 1) if total else 0.0,
            "elapsed": elapsed,
            "eta": eta,
            "rate_per_min": round(rate * 60, 1),
            "live": live,
            "traffic": _json(job.get("traffic")),
            "events": store.events(job_id, 30),
        }

    # -- static -----------------------------------------------------------
    @app.get("/")
    @requires_auth
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:name>")
    @requires_auth
    def static_files(name: str):
        return send_from_directory(STATIC_DIR, name)

    # -- meta -------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "version": __version__, "transport": "curl_cffi" if HAS_CURL_CFFI else "requests"})

    @app.get("/api/config")
    @requires_auth
    def config():
        return jsonify(
            {
                "version": __version__,
                "base_url": settings.base_url,
                "ship_to": settings.ship_to,
                "currency": settings.currency,
                "max_sessions": settings.max_sessions,
                "default_pace": DEFAULT_PACE,
                "paces": [
                    {"key": p.key, "label": p.label, "interval": p.interval, "description": p.description} for p in PACES.values()
                ],
                "quick_sections": [{"label": label, "url": f"{settings.base_url}{path}"} for label, path in QUICK_SECTIONS],
                "transport": "curl_cffi" if HAS_CURL_CFFI else "requests",
                "proxies": len(settings.load_proxies()),
            }
        )

    @app.get("/api/status")
    @requires_auth
    def status():
        active = manager.active
        if not active:
            return jsonify({"active": None})
        job = manager.store(active.kind).get_job(active.job_id)
        return jsonify({"active": progress(active.kind, job) if job else None})

    @app.post("/api/stop")
    @requires_auth
    def stop():
        return jsonify({"stopping": manager.stop()})

    # -- catalog ------------------------------------------------------------
    @app.post("/api/catalog/jobs")
    @requires_auth
    def create_catalog_job():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        try:
            section = section_from_url(url, settings.base_url)
        except InvalidSectionUrl as exc:
            return error(str(exc))
        with manager.exclusive():
            if manager.is_busy():
                return error("Another job is running. Stop it first: only one job runs at a time.", 409)
            job_id = manager.catalog.create_job(
                url=section.url + (f"?{urlencode(section.params)}" if section.params else ""),
                section=section.label,
                kind=section.kind,
                sessions=_sessions(data.get("sessions", 3), settings),
                pace=_pace(data.get("pace")),
                with_details=1 if _bool(data.get("with_details"), True) else 0,
                max_products=_limit(data.get("max_products")),
            )
            try:
                manager.start("catalog", job_id)
            except JobBusyError as exc:
                manager.catalog.delete_job(job_id)
                return error(str(exc), 409)
            manager.catalog.purge_except(job_id)  # only now is the previous run safe to drop
        return jsonify(progress("catalog", manager.catalog.get_job(job_id))), 201

    # -- inventory ------------------------------------------------------------
    @app.post("/api/inventory/uploads")
    @requires_auth
    def upload_inventory():
        file = request.files.get("file")
        if file is None or not file.filename:
            return error("Choose a CSV or Excel file to upload.")
        filename = re.sub(r"[^\w.\- ]+", "_", Path(file.filename).name)[:120]
        data = file.read()
        try:
            parsed = parse_upload(filename, data)
        except UploadError as exc:
            return error(str(exc))
        with manager.exclusive():
            active = manager.active
            if active and active.kind == "inventory":
                return error("A stock check is running. Stop it before uploading a new file.", 409)
            job_id = manager.inventory.create_job(
                status="ready",
                filename=filename,
                sessions=3,
                pace=DEFAULT_PACE,
                valid_rows=parsed.valid,
                invalid_rows=parsed.invalid,
                duplicate_rows=parsed.duplicates,
            )
            manager.inventory.insert_items(
                job_id,
                [
                    {
                        "state": "invalid" if row.isbn is None else "pending",
                        "row_no": row.row_no,
                        "raw_sku": row.raw_sku,
                        "isbn": row.isbn,
                        "error": row.problem,
                    }
                    for row in parsed.rows
                ],
            )
            manager.inventory.update_job(job_id, total=len(parsed.rows))
            manager.inventory.purge_except(job_id)  # the previous stock check is no longer needed
        manager.inventory.add_event(
            job_id,
            "info",
            f"Loaded {filename}: {parsed.valid:,} ISBNs from '{parsed.column_label}'"
            + (f", {parsed.invalid:,} invalid" if parsed.invalid else "")
            + (f", {parsed.duplicates:,} duplicates (checked once)" if parsed.duplicates else "")
            + ".",
        )
        payload = progress("inventory", manager.inventory.get_job(job_id))
        payload["upload"] = {
            "column": parsed.column_label,
            "valid": parsed.valid,
            "invalid": parsed.invalid,
            "duplicates": parsed.duplicates,
            "unique": parsed.unique,
            "preview": [{"row": r.row_no, "sku": r.raw_sku, "isbn": r.isbn, "problem": r.problem} for r in parsed.rows[:8]],
        }
        return jsonify(payload), 201

    # -- shared job routes ----------------------------------------------------
    @app.get("/api/<kind>/jobs")
    @requires_auth
    def list_jobs(kind: str):
        store = store_for(kind)
        if store is None:
            return error("unknown module", 404)
        jobs = store.list_jobs(25)
        out = []
        for job in jobs:
            counts = store.state_counts(job["id"])
            total = sum(counts.values())
            processed = total - counts["pending"] - counts["processing"]
            live = manager.live(kind, job["id"]) is not None
            out.append({**job, "processed": processed, "items": total, "running": live})
        return jsonify({"jobs": out})

    @app.get("/api/<kind>/jobs/<job_id>")
    @requires_auth
    def get_job(kind: str, job_id: str):
        store = store_for(kind)
        job = store.get_job(job_id) if store else None
        if job is None:
            return error("Job not found", 404)
        return jsonify(progress(kind, job))

    @app.get("/api/<kind>/jobs/<job_id>/rows")
    @requires_auth
    def job_rows(kind: str, job_id: str):
        store = store_for(kind)
        if store is None or store.get_job(job_id) is None:
            return error("Job not found", 404)
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = max(1, min(int(request.args.get("limit", 25)), 200))
        except ValueError:
            offset, limit = 0, 25
        total, rows = store.page(
            job_id, offset=offset, limit=limit, query=request.args.get("q") or None, status=request.args.get("status") or None
        )
        return jsonify({"total": total, "rows": rows})

    @app.post("/api/<kind>/jobs/<job_id>/start")
    @requires_auth
    def start_job(kind: str, job_id: str):
        store = store_for(kind)
        job = store.get_job(job_id) if store else None
        if job is None:
            return error("Job not found", 404)
        data = request.get_json(silent=True) or {}
        with manager.exclusive():
            if manager.live(kind, job_id):
                return error("This job is already running.", 409)
            if manager.is_busy():
                return error("Another job is running. Stop it first: only one job runs at a time.", 409)
            startable = (*JOB_RESUMABLE_STATES, *JOB_ACTIVE_STATES, "stopping", "ready", "done")
            if job["status"] not in startable:
                return error(f"This job can't be started from status '{job['status']}'.", 409)
            counts = store.state_counts(job_id)
            work_left = counts["pending"] + counts["processing"] + (counts["error"] if data.get("retry_failed") else 0)
            if kind == "catalog" and not job.get("discovery_done"):
                work_left = max(work_left, 1)
            if job["status"] == "done" and not work_left:
                return error("Nothing left to do: every item is finished.", 409)
            # All checks passed: now it is safe to change the job.
            updates = {}
            if "sessions" in data:
                updates["sessions"] = _sessions(data["sessions"], settings)
            if "pace" in data:
                updates["pace"] = _pace(data["pace"])
            store.update_job(job_id, **updates)
            if data.get("retry_failed"):
                retried = store.retry_errors(job_id)
                store.add_event(job_id, "info", f"Re-queued {retried:,} failed item(s).")
            try:
                manager.start(kind, job_id)
            except JobBusyError as exc:
                return error(str(exc), 409)
        return jsonify(progress(kind, store.get_job(job_id)))

    @app.delete("/api/<kind>/jobs/<job_id>")
    @requires_auth
    def delete_job(kind: str, job_id: str):
        store = store_for(kind)
        if store is None or store.get_job(job_id) is None:
            return error("Job not found", 404)
        if manager.live(kind, job_id):
            return error("Stop the job before deleting it.", 409)
        store.delete_job(job_id)
        return jsonify({"deleted": job_id})

    @app.get("/api/<kind>/jobs/<job_id>/export")
    @requires_auth
    def export_job_file(kind: str, job_id: str):
        store = store_for(kind)
        job = store.get_job(job_id) if store else None
        if job is None:
            return error("Job not found", 404)
        saved = Path(job["export_path"]) if job.get("export_path") else None
        if saved and saved.is_file() and job["status"] == "done":
            return send_file(saved, as_attachment=True, download_name=saved.name)
        # Unfinished run: build a snapshot of what has been collected so far, then delete it.
        fd, tmp = tempfile.mkstemp(prefix="abp-", suffix=".xlsx")
        os.close(fd)
        export_job(kind, store, job, Path(tmp))
        name = export_filename(kind, job)
        if job["status"] != "done":
            name = name.replace(".xlsx", "-partial.xlsx")
        resp = send_file(tmp, as_attachment=True, download_name=name)
        resp.call_on_close(lambda: _remove_quietly(tmp))
        return resp

    @app.post("/api/<kind>/jobs/<job_id>/reveal")
    @requires_auth
    def reveal_export(kind: str, job_id: str):
        store = store_for(kind)
        job = store.get_job(job_id) if store else None
        if job is None or not job.get("export_path"):
            return error("No saved file for this run", 404)
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return error("Only available on the PC running the scraper", 403)
        path = Path(job["export_path"])
        if not path.is_file():
            return error("The saved file was moved or deleted. Use Download again.", 404)
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
        return jsonify({"revealed": str(path)})

    @app.errorhandler(413)
    def too_large(_exc):
        return error(f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.", 413)

    return app


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
