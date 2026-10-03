"""
Run outloud on local PDF files in worker processes that can be killed.

outloud is synchronous and CPU-bound, and a pathological PDF can make it hang or eat
memory. Each check therefore runs in a long-lived worker process. When a check runs past
its timeout, that one worker is killed and replaced; the other workers carry on. Workers are
also recycled after a number of files so slow memory growth cannot build up over a long run.

Besides outloud's own rules, the worker runs our OCR-001 check: pages that are images with
no extractable text (scans without OCR). outloud has no rule for this yet; see
https://github.com/visionably/outloud/issues/4.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Self

# A page is "image-only" when it has no extractable text and its images cover at least
# this share of the page. Small logos on an otherwise empty page do not count.
IMAGE_COVERAGE_THRESHOLD = 0.5


def image_only_pages(path: str) -> dict[str, Any]:
    """
    Find pages that are images without a text layer (scanned, never OCR'd).

    Returns ``{"pages": [1-based page numbers], "page_count": n}``. Uses PyMuPDF, which
    outloud already depends on.
    """
    import pymupdf

    pages: list[int] = []
    with pymupdf.open(path) as doc:
        for index, page in enumerate(doc):
            if page.get_text("text").strip():
                continue
            area = abs(page.rect)
            if not area:
                continue
            covered = 0.0
            for info in page.get_image_info():
                bbox = pymupdf.Rect(info["bbox"]) & page.rect
                covered += abs(bbox)
            if covered / area >= IMAGE_COVERAGE_THRESHOLD:
                pages.append(index + 1)
        return {"pages": pages, "page_count": doc.page_count}


def check_pdf(path: str, source: str | None = None) -> dict[str, Any]:
    """
    Check one PDF with outloud plus OCR-001. Runs inside a worker process.

    Returns outloud's result as a plain dict, with ``"ocr"`` (image-only pages) and
    ``"engine"`` (versions) added. Per-rule timing is reduced to the list of rules that
    crashed, since that is all the caller needs.
    """
    import outloud

    result = outloud.check(path, source=source)
    data: dict[str, Any] = result.to_dict()
    data["crashed_rules"] = [r["rule"] for r in data.pop("rules", []) if r["status"] == "crashed"]
    data["engine"] = {"outloud": outloud.__version__}
    if (data.get("stats") or {}).get("pages"):  # the file opened, even if outloud stopped later
        try:
            data["ocr"] = image_only_pages(path)
        except Exception as exc:  # a file outloud could read but PyMuPDF cannot
            data["ocr"] = {"pages": [], "page_count": 0, "error": f"{type(exc).__name__}: {exc}"}
    return data


def ocr_task(src: str, dst: str, lang: str) -> dict[str, Any]:
    """OCR job for the worker (see ocr.py). Needs the a11y-ocr extra."""
    from .ocr import ocr_pdf

    return ocr_pdf(src, dst, lang=lang)


TASKS: dict[str, Callable[..., dict[str, Any]]] = {"check": check_pdf, "ocr": ocr_task}


def _worker_main(conn: Connection) -> None:
    """Worker loop: receive (task, args), send back ("ok", result) or ("error", text)."""
    while True:
        try:
            message = conn.recv()
        except EOFError:
            return
        if message is None:
            return
        task, args = message
        try:
            conn.send(("ok", TASKS[task](*args)))
        except Exception as exc:
            conn.send(("error", f"{type(exc).__name__}: {exc}"[:300]))


class _Worker:
    """One worker process and the parent's end of its pipe."""

    def __init__(self, ctx: multiprocessing.context.SpawnContext) -> None:
        parent_conn, child_conn = ctx.Pipe()
        self.conn: Connection = parent_conn
        self.process = ctx.Process(target=_worker_main, args=(child_conn,), daemon=True)
        self.process.start()
        child_conn.close()
        self.tasks = 0

    def stop(self) -> None:
        """Ask the worker to exit; kill it if it does not."""
        with contextlib.suppress(BrokenPipeError, OSError):
            self.conn.send(None)
        self.process.join(timeout=2)
        self.kill()

    def kill(self) -> None:
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=5)
        self.conn.close()


@dataclass
class CheckOutcome:
    """What happened to one check.

    ``status`` is ``"ok"`` (``raw`` holds outloud's result), ``"timeout"`` (the worker was
    killed), ``"crashed"`` (the worker died, e.g. out of memory) or ``"error"`` (Python
    exception in the worker; ``error`` has the text).
    """

    status: str
    raw: dict[str, Any] | None = None
    error: str | None = None
    seconds: float = 0.0


@dataclass
class EngineConfig:
    workers: int = field(default_factory=lambda: max(1, min(4, (os.cpu_count() or 2) - 1)))
    timeout_seconds: float = 120.0
    recycle_after: int = 200


class CheckEngine:
    """
    A pool of killable outloud workers, used from asyncio.

        async with CheckEngine(EngineConfig(workers=3)) as engine:
            outcome = await engine.check("/tmp/file.pdf")
    """

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        self._ctx = multiprocessing.get_context("spawn")
        self._idle: asyncio.Queue[_Worker] = asyncio.Queue()
        self._all: list[_Worker] = []

    async def __aenter__(self) -> Self:
        for _ in range(self.config.workers):
            worker = await asyncio.to_thread(_Worker, self._ctx)
            self._all.append(worker)
            self._idle.put_nowait(worker)
        return self

    async def __aexit__(self, *exc: object) -> None:
        for worker in self._all:
            await asyncio.to_thread(worker.stop)
        self._all.clear()

    async def _replace(self, worker: _Worker) -> _Worker:
        await asyncio.to_thread(worker.kill)
        self._all.remove(worker)
        fresh = await asyncio.to_thread(_Worker, self._ctx)
        self._all.append(fresh)
        return fresh

    async def check(self, path: str | Path, source: str | Path | None = None) -> CheckOutcome:
        """Check one file; never raises for a bad file."""
        return await self.run("check", (str(path), str(source) if source else None))

    async def ocr(
        self, src: str | Path, dst: str | Path, lang: str, timeout: float
    ) -> CheckOutcome:
        """OCR the image-only pages of ``src`` into ``dst`` (see ocr.py)."""
        return await self.run("ocr", (str(src), str(dst), lang), timeout=timeout)

    async def run(
        self, task: str, args: tuple[Any, ...], timeout: float | None = None
    ) -> CheckOutcome:
        """Run one task in a worker; a task that overruns its timeout has its worker killed."""
        limit = timeout or self.config.timeout_seconds
        loop = asyncio.get_running_loop()
        worker = await self._idle.get()
        start = loop.time()
        settled = False  # False while a request is in flight on this worker's pipe
        try:
            worker.conn.send((task, args))
            ready = await asyncio.to_thread(worker.conn.poll, limit)
            if not ready:
                worker = await self._replace(worker)
                settled = True
                return CheckOutcome(
                    "timeout",
                    error=f"{task} took longer than {limit:.0f} s",
                    seconds=loop.time() - start,
                )
            try:
                kind, payload = await asyncio.to_thread(worker.conn.recv)
            except (EOFError, OSError):
                worker = await self._replace(worker)
                settled = True
                return CheckOutcome(
                    "crashed",
                    error="the worker process died (out of memory or a crash in PDF parsing)",
                    seconds=loop.time() - start,
                )
            settled = True
            worker.tasks += 1
            if worker.tasks >= self.config.recycle_after:
                worker = await self._replace(worker)
            if kind == "ok":
                return CheckOutcome("ok", raw=payload, seconds=loop.time() - start)
            return CheckOutcome("error", error=payload, seconds=loop.time() - start)
        finally:
            if not settled:
                # Cancelled mid-check: the worker may still answer later, which would be
                # read as the answer to the next file. Replace it before reuse.
                worker.kill()
                self._all.remove(worker)
                worker = _Worker(self._ctx)
                self._all.append(worker)
            self._idle.put_nowait(worker)
