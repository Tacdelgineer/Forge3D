"""In-process job handling so the browser never blocks on a multi-minute request.

One worker thread, a dict of job records, no broker. There is exactly one GPU,
so serial execution is not a limitation — it is the correct behaviour, and it
also serialises work sent to the Hunyuan worker container.
"""
import logging
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config, engine, memory

log = logging.getLogger("trellis.jobs")

QUEUED = "queued"
LOADING_MODEL = "loading_model"
GENERATING = "generating"
EXPORTING = "exporting"
COMPLETE = "complete"
ERROR = "error"

TERMINAL = (COMPLETE, ERROR)

# Keep the most recent N jobs in memory. Job records are ephemeral status only;
# the assets themselves live on disk and are listed from there, so losing this
# on restart costs nothing.
MAX_JOBS = 200

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="forge3d-gen")
_jobs: "OrderedDict[str, Job]" = OrderedDict()
_lock = threading.Lock()


@dataclass
class Job:
    id: str
    generator: str
    mode: str
    seed: int
    filename: str
    texture_size: int
    views: tuple[str, ...] = ()
    state: str = QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    asset_id: str | None = None
    error: str | None = None
    error_kind: str | None = None
    error_data: dict | None = None
    result: dict | None = None

    def elapsed_s(self) -> float:
        end = self.finished_at or time.time()
        return round(end - self.created_at, 1)

    def as_dict(self) -> dict:
        gen = config.generator(self.generator) or {}
        d = {
            "job_id": self.id,
            "state": self.state,
            "generator": self.generator,
            "generator_name": gen.get("name", self.generator),
            "mode": self.mode,
            "mode_label": config.MODE_LABELS.get(self.mode, self.mode),
            "seed": self.seed,
            "texture_size": self.texture_size,
            "views": list(self.views),
            "filename": self.filename,
            "elapsed_s": self.elapsed_s(),
            "created_at": datetime.fromtimestamp(self.created_at, timezone.utc).isoformat(),
            "asset_id": self.asset_id,
        }
        if self.state == ERROR:
            d["error"] = self.error
            d["error_kind"] = self.error_kind
            if self.error_data:
                d["error_data"] = self.error_data
        if self.state == COMPLETE and self.result:
            d["result"] = {
                "id": self.result.get("id"),
                "glb_mb": self.result.get("glb_mb"),
                "glb_bytes": self.result.get("glb_bytes"),
                "timings_s": self.result.get("timings_s"),
                "validation": self.result.get("validation"),
                "memory_gb": self.result.get("memory_gb"),
            }
        return d


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def active_job() -> Job | None:
    with _lock:
        for job in reversed(_jobs.values()):
            if job.state not in TERMINAL:
                return job
    return None


def _set(job: Job, state: str) -> None:
    job.state = state
    if state == GENERATING and job.started_at is None:
        job.started_at = time.time()
    log.info("job %s -> %s", job.id, state)


def _run(job: Job, images: dict[str, bytes]) -> None:
    try:
        job.started_at = time.time()
        # engine.generate re-runs the backend and memory checks inside the
        # generation lock, covering anything that changed while this job queued.
        result = engine.generate(
            images,
            job.filename,
            job.seed,
            job.mode,
            generator=job.generator,
            texture_size=job.texture_size,
            on_state=lambda state: _set(job, state),
        )
        job.result = result
        job.asset_id = result.get("id")
        _set(job, COMPLETE)
    except memory.InsufficientMemory as exc:
        job.error_kind = "insufficient_memory"
        job.error_data = exc.as_dict()
        job.error = exc.reason
        _set(job, ERROR)
    except engine.BackendUnavailable as exc:
        job.error_kind = "backend_unavailable"
        job.error = str(exc)
        _set(job, ERROR)
    except engine.Busy:
        job.error_kind = "generation_in_progress"
        job.error = "Another generation is already running."
        _set(job, ERROR)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
        log.exception("job %s failed", job.id)
        job.error_kind = type(exc).__name__
        job.error = f"{type(exc).__name__}: {exc}"
        _set(job, ERROR)
    finally:
        job.finished_at = time.time()


def submit(
    images: dict[str, bytes],
    filename: str,
    seed: int,
    mode: str,
    generator: str,
    texture_size: int,
) -> Job:
    """Queue a generation. Returns immediately; poll GET /jobs/{id}."""
    if generator not in config.GENERATOR_IDS:
        raise ValueError(f"invalid generator: {generator}")
    if mode not in config.valid_modes(generator):
        raise ValueError(f"invalid mode for {generator}: {mode}")

    job = Job(
        id=uuid.uuid4().hex[:12],
        generator=generator,
        mode=mode,
        seed=seed,
        filename=filename,
        texture_size=texture_size,
        views=tuple(sorted(images.keys())),
    )
    with _lock:
        _jobs[job.id] = job
        while len(_jobs) > MAX_JOBS:
            oldest, rec = next(iter(_jobs.items()))
            if rec.state not in TERMINAL:
                break
            _jobs.pop(oldest)
    _executor.submit(_run, job, images)
    return job
