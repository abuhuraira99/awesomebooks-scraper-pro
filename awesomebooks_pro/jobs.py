"""Job engine: runs catalog extractions and stock checks in the background.

Only one job runs at a time across *both* modules. They share the same IP and
the same politeness budget, so running them together would double the load on
the site. :class:`JobManager` enforces this and exposes live telemetry
(pace, cool-downs, identities) for the UI.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import scan
from .config import DEFAULT_PACE, PACES, Settings
from .net import (
    AdaptiveRateLimiter,
    CancelledError,
    FetchError,
    HttpClient,
    IdentityFactory,
    NotFoundError,
    TrafficStats,
)
from .net.client import default_transport_factory
from .site import AwesomeBooksClient, ProductRecord, SessionStats, section_from_url
from .storage import JOB_ACTIVE_STATES, CatalogStore, InventoryStore, JobStore

log = logging.getLogger(__name__)


class JobBusyError(RuntimeError):
    pass


@dataclass
class ActiveJob:
    kind: str  # "catalog" | "inventory"
    job_id: str
    sessions: int
    cancel: threading.Event
    limiter: AdaptiveRateLimiter
    stats: TrafficStats
    site_stats: SessionStats
    thread: threading.Thread | None = None
    started: float = field(default_factory=time.time)

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class JobManager:
    def __init__(
        self,
        settings: Settings,
        catalog: CatalogStore,
        inventory: InventoryStore,
        *,
        transport_factory: Callable = default_transport_factory,
        limiter_factory: Callable[[float], AdaptiveRateLimiter] | None = None,
        backoff_scale: float = 1.0,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.inventory = inventory
        self.transport_factory = transport_factory
        self.limiter_factory = limiter_factory or (lambda interval: AdaptiveRateLimiter(interval))
        self.backoff_scale = backoff_scale
        self._lock = threading.RLock()
        self._active: ActiveJob | None = None

    # -- state ------------------------------------------------------------
    def store(self, kind: str) -> JobStore:
        return self.catalog if kind == "catalog" else self.inventory

    @property
    def active(self) -> ActiveJob | None:
        with self._lock:
            if self._active and not self._active.alive():
                self._active = None
            return self._active

    def live(self, kind: str, job_id: str) -> dict[str, Any] | None:
        active = self.active
        if not active or active.kind != kind or active.job_id != job_id:
            return None
        traffic = active.stats.as_dict()
        traffic["locale_switches"] = active.site_stats.locale_switches
        return {"sessions": active.sessions, "limiter": active.limiter.snapshot().as_dict(), "traffic": traffic}

    # -- control ----------------------------------------------------------
    def exclusive(self) -> threading.RLock:
        """Hold while checking/creating/starting jobs so two requests can never interleave."""
        return self._lock

    def is_busy(self) -> bool:
        return self.active is not None

    def start(self, kind: str, job_id: str) -> None:
        store = self.store(kind)
        job = store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        with self._lock:
            if self._active and self._active.alive():
                other = "catalog extraction" if self._active.kind == "catalog" else "stock check"
                raise JobBusyError(f"A {other} is already running. Stop it first: only one job runs at a time.")
            pace = PACES.get(job["pace"], PACES[DEFAULT_PACE])
            sessions = max(1, min(int(job["sessions"]), self.settings.max_sessions))
            active = ActiveJob(
                kind=kind,
                job_id=job_id,
                sessions=sessions,
                cancel=threading.Event(),
                limiter=self.limiter_factory(pace.interval),
                stats=TrafficStats(),
                site_stats=SessionStats(),
            )
            target = self._run_catalog if kind == "catalog" else self._run_inventory
            active.thread = threading.Thread(target=self._guard, args=(target, active), name=f"job-{kind}-{job_id}", daemon=True)
            self._active = active
            # No thread works on this job right now, so anything left 'processing' is orphaned: requeue it.
            store.requeue_processing(job_id)
            store.update_job(job_id, status="queued", started_at=time.time(), error=None)
            active.thread.start()

    def stop(self) -> bool:
        active = self.active
        if not active:
            return False
        active.cancel.set()
        store = self.store(active.kind)
        store.mark_stopping(active.job_id)
        store.add_event(active.job_id, "info", "Stop requested. Finishing in-flight requests…")
        return True

    def wait(self, timeout: float | None = None) -> None:
        active = self.active
        if active and active.thread:
            active.thread.join(timeout)

    # -- plumbing ---------------------------------------------------------
    def _client(self, active: ActiveJob, store: JobStore) -> HttpClient:
        return HttpClient(
            active.limiter,
            IdentityFactory(self.settings.load_proxies()),
            stats=active.stats,
            cancel=active.cancel,
            on_event=lambda level, msg: store.add_event(active.job_id, level, msg),
            timeout=self.settings.request_timeout,
            max_attempts=self.settings.max_request_attempts,
            transport_factory=self.transport_factory,
            backoff_scale=self.backoff_scale,
        )

    def _site(self, active: ActiveJob, store: JobStore) -> AwesomeBooksClient:
        return AwesomeBooksClient(
            self._client(active, store),
            self.settings.base_url,
            ship_to=self.settings.ship_to,
            currency=self.settings.currency,
            stats=active.site_stats,
        )

    def _guard(self, target: Callable[[ActiveJob], None], active: ActiveJob) -> None:
        store = self.store(active.kind)
        run_started = time.time()
        finished = threading.Event()

        def heartbeat() -> None:
            beats = 0
            try:
                while not finished.wait(10.0):
                    store.update_job(active.job_id, heartbeat_at=time.time())
                    beats += 1
                    if beats % 3 == 0:  # a progress line in the console window every 30s
                        self._log_progress(active, store)
            finally:
                store.close()  # this thread's own SQLite connection

        threading.Thread(target=heartbeat, name=f"hb-{active.job_id}", daemon=True).start()
        try:
            target(active)
        except CancelledError:
            _quietly(store.update_job, active.job_id, status="paused")
        except Exception as exc:  # the job itself failed (not a single item)
            log.exception("job %s crashed", active.job_id)
            _quietly(store.update_job, active.job_id, status="failed", error=str(exc)[:500])
            _quietly(store.add_event, active.job_id, "error", f"Job stopped: {exc} Press Resume to continue.")
            log.debug(traceback.format_exc())
        finally:
            finished.set()
            try:
                job = store.get_job(active.job_id) or {}
                store.update_job(
                    active.job_id,
                    active_seconds=(job.get("active_seconds") or 0) + time.time() - run_started,
                    traffic=json.dumps(_merge_traffic(job.get("traffic"), active)),
                )
                if active.cancel.is_set() and job.get("status") not in ("failed", "done"):
                    store.update_job(active.job_id, status="paused")
                    store.add_event(active.job_id, "info", "Stopped. Press Resume to continue from where it stopped.")
                elif job.get("status") in JOB_ACTIVE_STATES:
                    store.update_job(active.job_id, status="failed", error="stopped unexpectedly")
            except Exception:
                log.exception("could not record the final state of job %s", active.job_id)
            finally:
                store.close()

    def _run_pool(
        self,
        active: ActiveJob,
        store: JobStore,
        handler: Callable[[AwesomeBooksClient, dict[str, Any]], tuple[dict[str, Any], str]],
    ) -> None:
        crashes: list[str] = []

        def worker(n: int) -> None:
            site = self._site(active, store)
            try:
                self._work(active, store, site, handler)
            except Exception as exc:  # database or programming error outside a single item
                log.exception("worker %s crashed", n)
                crashes.append(f"{type(exc).__name__}: {exc}")
            finally:
                site.http.close()
                store.close()

        threads = [
            threading.Thread(target=worker, args=(n,), name=f"{active.kind}-w{n}", daemon=True) for n in range(active.sessions)
        ]
        for t in threads:
            t.start()
            time.sleep(0.2)  # stagger session start-up like real visitors arriving
        for t in threads:
            t.join()
        if active.cancel.is_set():
            raise CancelledError("stopped")
        if crashes:
            raise FetchError(f"a worker session stopped unexpectedly ({crashes[0]})")

    def _work(
        self,
        active: ActiveJob,
        store: JobStore,
        site: AwesomeBooksClient,
        handler: Callable[[AwesomeBooksClient, dict[str, Any]], tuple[dict[str, Any], str]],
    ) -> None:
        """One session: claim items and process them until none are left or the job is stopped."""
        while not active.cancel.is_set():
            item = store.claim_next(active.job_id)
            if item is None:
                return
            try:
                fields, state = handler(site, item)
                store.complete_item(item["id"], fields, state=state)
            except CancelledError:
                store.release_item(item)
                return
            except FetchError as exc:
                new_state = store.fail_item(item, str(exc), self.settings.max_item_attempts)
                if new_state == "error":
                    store.add_event(active.job_id, "error", f"Gave up on {_label(item)}: {exc}")
            except Exception as exc:  # parsing bug or unexpected payload: keep the run alive
                log.exception("item %s failed", item.get("id"))
                new_state = store.fail_item(item, f"{type(exc).__name__}: {exc}", self.settings.max_item_attempts)
                if new_state == "error":
                    store.add_event(active.job_id, "error", f"Unexpected data for {_label(item)}: {exc}")

    def _log_progress(self, active: ActiveJob, store: JobStore) -> None:
        try:
            counts = store.state_counts(active.job_id)
            total = sum(v for k, v in counts.items() if k != "invalid")
            done = total - counts.get("pending", 0) - counts.get("processing", 0)
            recent = store.completed_since(active.job_id, time.time() - 60)
            eta = f"{(total - done) / recent / 60:.1f}h left" if recent else "estimating"
            pct = done / total * 100 if total else 0
            log.info("[%s] progress %s/%s (%.1f%%) · %s/min · %s", store.LABEL, f"{done:,}", f"{total:,}", pct, recent, eta)
        except Exception:  # progress logging must never disturb the job
            pass

    def _finish(self, active: ActiveJob, store: JobStore) -> None:
        counts = store.state_counts(active.job_id)
        unfinished = counts.get("pending", 0) + counts.get("processing", 0)
        if unfinished:
            raise FetchError(f"{unfinished:,} item(s) were not processed")
        store.update_job(active.job_id, status="done", finished_at=time.time())
        msg = (
            f"Finished: {counts['done']:,} checked"
            if active.kind == "inventory"
            else f"Finished: {counts['done']:,} books extracted"
        )
        if counts["error"]:
            msg += f", {counts['error']:,} failed (use Retry failed)"
        store.add_event(active.job_id, "success", msg + ".")
        self._auto_save(active.kind, store, active.job_id)

    def _auto_save(self, kind: str, store: JobStore, job_id: str) -> None:
        """Save the finished run's Excel file straight into Downloads (no one needs to be at the PC)."""
        from .export import export_filename, export_job, unique_path

        job = store.get_job(job_id)
        if job is None:
            return
        try:
            folder = self.settings.resolve_export_dir()
            folder.mkdir(parents=True, exist_ok=True)
            path = export_job(kind, store, job, unique_path(folder, export_filename(kind, job)))
        except Exception as exc:
            log.exception("auto-save failed")
            store.add_event(job_id, "error", f"Couldn't save the Excel file automatically ({exc}). Use Download instead.")
            return
        store.update_job(job_id, export_path=str(path), exported_at=time.time())
        store.add_event(job_id, "success", f"Excel file saved automatically: {path}")

    def auto_resume(self, kind: str, job_id: str) -> None:
        """Restart a job that a crash / reboot / power cut interrupted."""
        store = self.store(kind)
        try:
            with self._lock:
                job = store.get_job(job_id)
                if job is None or job["status"] != "interrupted" or self.is_busy():
                    return
                store.add_event(job_id, "info", "The app was closed mid-run. Resuming automatically from where it stopped.")
                self.start(kind, job_id)
        except Exception:
            log.exception("auto-resume skipped")

    def _retrying(self, active: ActiveJob, store: JobStore, label: str, fetch: Callable[..., Any], *args: Any) -> Any:
        """Patient, job-level retries for one listing page (on top of the per-request retries)."""
        for round_ in range(1, 4):
            try:
                return fetch(*args)
            except (CancelledError, NotFoundError):
                raise
            except FetchError as exc:
                if round_ == 3:
                    raise
                store.add_event(active.job_id, "warn", f"{label} failed ({exc}). Trying again in a minute.")
                if active.cancel.wait(60 * self.backoff_scale):
                    raise CancelledError("stopped") from exc
        raise AssertionError("unreachable")

    # -- catalog ------------------------------------------------------------
    def _run_catalog(self, active: ActiveJob) -> None:
        store = self.catalog
        job = store.get_job(active.job_id)
        assert job is not None
        if not job["discovery_done"]:
            self._discover(active, job)
        job = store.get_job(active.job_id)
        assert job is not None
        if job["with_details"]:
            _set_live_status(active, store, "running")
            store.add_event(
                active.job_id,
                "info",
                f"Reading New & Used prices and exact stock with {active.sessions} session(s) at "
                f"{PACES.get(job['pace'], PACES[DEFAULT_PACE]).label.lower()} pace (US shipping, USD).",
            )

            def handle(site: AwesomeBooksClient, item: dict[str, Any]) -> tuple[dict[str, Any], str]:
                record = site.product(item["isbn"], item.get("product_url"))
                if not record.found:
                    # Listed a moment ago, gone now: keep the row, mark it out of stock.
                    record.title = record.title or item.get("title")
                    record.notes = ["No longer listed on AwesomeBooks"]
                return {**_catalog_fields(record, item), "checked_at": time.time()}, "done"

            self._run_pool(active, store, handle)
        self._finish(active, store)

    def _discover(self, active: ActiveJob, job: dict[str, Any]) -> None:
        store = self.catalog
        _set_live_status(active, store, "discovering")
        section = section_from_url(job["url"], self.settings.base_url)
        site = self._site(active, store)
        state = json.loads(job["scan_state"]) if job.get("scan_state") else None
        limit = int(job["max_products"]) if job.get("max_products") else None
        if state is None:
            store.add_event(active.job_id, "info", f"Scanning {section.label} as a US shopper (ship to US, prices in USD)…")
        try:
            if section.kind == "bestsellers":
                self._discover_bestsellers(active, job, site, state, limit)
            else:
                self._discover_listing(active, job, site, section, state, limit)
        except NotFoundError as exc:
            if store.item_count(active.job_id):
                raise FetchError(f"A part of the section disappeared mid-scan ({exc}).") from exc
            raise FetchError(f"'{section.label}' does not exist on AwesomeBooks.") from exc
        finally:
            site.http.close()
        total = store.item_count(active.job_id)
        if total == 0:
            raise FetchError("No books were found in this section.")
        job = store.get_job(active.job_id) or job
        listed = job.get("listed_total")
        store.update_job(active.job_id, discovery_done=1, total=total)
        if listed and not limit and total < listed:
            store.add_event(
                active.job_id,
                "warn",
                f"The section lists {listed:,} books; {total:,} different books could be reached through its pages "
                "and filters. The rest are not shown on any listing page the site serves.",
            )
        store.add_event(active.job_id, "success", f"Discovery complete: {total:,} books queued.")

    def _discover_listing(self, active, job, site, section, state, limit) -> None:
        store = self.catalog
        state = state or scan.new_plan(section.path, section.param_dict())
        pages_scanned = int(job["pages_scanned"])
        with_details = bool(job["with_details"])
        while state["queue"]:
            count = store.item_count(active.job_id)
            if limit and count >= limit:
                store.add_event(active.job_id, "info", f"Reached the limit of {limit:,} books. Stopping the scan.")
                break
            piece = state["queue"][0]
            number = piece["page"] + 1
            listing = self._retrying(
                active,
                store,
                f"Page {number} of {scan.describe(piece)}",
                site.listing_page,
                piece["path"],
                piece["params"],
                number,
            )
            if number == 1:
                piece["total"] = listing.total
                if state.get("listed") is None:
                    state["listed"] = listing.total if listing.total is not None else len(listing.cards)
                    store.update_job(active.job_id, listed_total=state["listed"])
                    store.add_event(active.job_id, "info", f"The section lists {state['listed']:,} books.")
                # A limit the site's own pages can satisfy means "the first N books, in the order shown": no slicing.
                needs_slicing = not limit or limit > scan.WINDOW
                if listing.total and listing.total > scan.WINDOW and not piece.get("leaf") and needs_slicing:
                    children, dimension = scan.split(piece, listing)
                    if children:
                        store.add_event(
                            active.job_id,
                            "info",
                            f"{listing.total:,} books in {scan.describe(piece)}: more than the 10,000 the site pages "
                            f"through, so it is read in {len(children)} parts by {dimension}.",
                        )
                    else:
                        children = scan.every_order(piece)
                        store.add_event(
                            active.job_id,
                            "warn",
                            f"{listing.total:,} books in {scan.describe(piece)} can't be narrowed further, so it is read "
                            f"in all {len(children)} sort orders (up to {len(children) * scan.WINDOW:,} books).",
                        )
                    state["queue"][0:1] = children
                    store.update_job(active.job_id, scan_state=json.dumps(state))
                    continue
            records = [_listing_record(card, with_details) for card in listing.cards]
            if limit:
                records = records[: max(0, limit - count)]
            added = store.insert_items(active.job_id, records, state="pending" if with_details else "done")
            pages_scanned += 1
            piece["page"] = number
            if scan.is_last_page(piece, number, listing):
                state["queue"].pop(0)
            total = store.item_count(active.job_id)
            store.update_job(active.job_id, pages_scanned=pages_scanned, total=total, scan_state=json.dumps(state))
            where = (
                f" ({scan.describe(piece)})" if piece["path"] != section.path or piece["params"] != section.param_dict() else ""
            )
            store.add_event(
                active.job_id, "info", f"Page {number}{where}: {len(listing.cards)} books, {added} new · {total:,} total"
            )

    def _discover_bestsellers(self, active, job, site, state, limit) -> None:
        """The Bestsellers page is drawn by the site's search index (top 1,000 by sales rank)."""
        store = self.catalog
        state = state or {"bestsellers": True, "page": 0, "pages": None}
        with_details = bool(job["with_details"])
        while state["pages"] is None or state["page"] < state["pages"]:
            count = store.item_count(active.job_id)
            if limit and count >= limit:
                store.add_event(active.job_id, "info", f"Reached the limit of {limit:,} books.")
                break
            hits, pages = self._retrying(
                active, store, f"Bestsellers page {state['page'] + 1}", site.bestsellers_page, state["page"]
            )
            if state["pages"] is None:
                state["pages"] = pages
                store.update_job(active.job_id, listed_total=pages * 100)
            records = [
                {
                    "isbn": h.isbn,
                    "product_url": h.url,
                    "title": h.title,
                    "author": h.author,
                    "format": h.format,
                    "image_url": h.image_url,
                    "listed_condition": "New" if h.new_available else "Used" if h.used_available else None,
                    # Quick mode keeps the index's availability flags; prices come only from book pages.
                    **(
                        {}
                        if with_details
                        else {"new_available": int(h.new_available), "used_available": int(h.used_available), "found": 1}
                    ),
                }
                for h in hits
            ]
            if limit:
                records = records[: max(0, limit - count)]
            added = store.insert_items(active.job_id, records, state="pending" if with_details else "done")
            state["page"] += 1
            total = store.item_count(active.job_id)
            store.update_job(active.job_id, pages_scanned=state["page"], total=total, scan_state=json.dumps(state))
            store.add_event(
                active.job_id, "info", f"Bestsellers page {state['page']}: {len(hits)} books, {added} new · {total:,} total"
            )
            if not hits:
                break

    # -- inventory ------------------------------------------------------------
    def _run_inventory(self, active: ActiveJob) -> None:
        store = self.inventory
        job = store.get_job(active.job_id)
        assert job is not None
        _set_live_status(active, store, "running")
        store.add_event(
            active.job_id,
            "info",
            f"Checking stock with {active.sessions} session(s) at {PACES.get(job['pace'], PACES[DEFAULT_PACE]).label.lower()} "
            "pace (US shipping, USD).",
        )

        def handle(site: AwesomeBooksClient, item: dict[str, Any]) -> tuple[dict[str, Any], str]:
            cached = store.cached_answer(active.job_id, item["isbn"])
            if cached:
                return {k: cached[k] for k in InventoryStore.ANSWER_COLUMNS}, "done"
            record = site.lookup_stock(item["isbn"])
            quantity = record.quantity if record.in_stock else 0
            return {
                **record.fields(),
                "stock_status": "in_stock" if record.in_stock else "out_of_stock",
                "quantity": quantity,
                "checked_at": time.time(),
            }, "done"

        self._run_pool(active, store, handle)
        self._finish(active, store)


def _listing_record(card: Any, with_details: bool) -> dict[str, Any]:
    record = {
        "isbn": card.isbn,
        "product_url": card.url,
        "title": card.title,
        "author": card.author,
        "format": card.format,
        "image_url": card.image_url,
        "listed_condition": card.condition,
        "listed_price": card.price,
    }
    if not with_details:
        # Quick mode: only what the listing card shows. The other condition stays unknown (empty), never "No".
        is_new, is_used = card.condition == "New", card.condition == "Used"
        record.update(
            found=1,
            rrp=card.rrp,
            new_available=1 if is_new else None,
            new_price=card.price if is_new else None,
            used_available=1 if is_used else None,
            used_price=card.price if is_used else None,
            checked_at=time.time(),
        )
    return record


def _catalog_fields(record: ProductRecord, item: dict[str, Any]) -> dict[str, Any]:
    fields = record.fields()
    for key in ("title", "author", "format", "image_url", "product_url"):
        if not fields.get(key):
            fields[key] = item.get(key)  # keep what the listing showed when the page lacks it
    return fields


def _merge_traffic(previous: str | None, active: ActiveJob) -> dict[str, Any]:
    """Accumulate traffic counters across resumes so a job keeps its full anti-block report."""
    try:
        totals = json.loads(previous) if previous else {}
    except ValueError:
        totals = {}
    current = active.stats.as_dict()
    current.pop("offline_for", None)
    current["locale_switches"] = active.site_stats.locale_switches
    merged = {k: int(totals.get(k, 0)) + v for k, v in current.items()}
    merged["breaker_trips"] = int(totals.get("breaker_trips", 0)) + active.limiter.snapshot().breaker_trips
    merged["sessions"] = active.sessions
    return merged


def _set_live_status(active: ActiveJob, store: JobStore, status: str) -> None:
    """Advance a running job's status unless a Stop has already been requested."""
    if not active.cancel.is_set():
        store.update_job(active.job_id, status=status)


def _quietly(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:
        log.exception("state write failed")


def _label(item: dict[str, Any]) -> str:
    return item.get("isbn") or item.get("raw_sku") or item.get("title") or f"item {item.get('id')}"
