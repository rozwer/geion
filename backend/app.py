import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from scraping import scrape_as_json


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobPayload:
    job_id: str
    email: str
    password: str
    exclude_nickname: str


MAX_WORKERS = max(1, int(os.environ.get("SCRAPER_MAX_CONCURRENCY", "5")))
DEFAULT_HISTORY = max(200, MAX_WORKERS * 20)
MAX_HISTORY = max(0, int(os.environ.get("SCRAPER_MAX_HISTORY", str(DEFAULT_HISTORY))))
QUEUE_LIMIT = max(0, int(os.environ.get("SCRAPER_QUEUE_LIMIT", "50")))

allow_origins_env = os.environ.get("SCRAPER_ALLOWED_ORIGINS", "*")
allow_origins = [origin.strip() for origin in allow_origins_env.split(",") if origin.strip()]
if not allow_origins:
    allow_origins = ["*"]

app = FastAPI(
    title="Bandwith Scraper Service",
    description="Queued scraping microservice for Firebase integration.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins if allow_origins != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

job_queue: asyncio.Queue[JobPayload] = asyncio.Queue()
jobs: Dict[str, Dict[str, Any]] = {}
job_history: List[str] = []
workers: List[asyncio.Task] = []


class ScrapeRequest(BaseModel):
    email: str
    password: str
    excludeNickname: str


def _prune_history() -> None:
    if MAX_HISTORY <= 0:
        return
    while len(job_history) > MAX_HISTORY:
        oldest_id = job_history[0]
        record = jobs.get(oldest_id)
        if record and record.get("status") in {"succeeded", "failed", "cancelled"}:
            job_history.pop(0)
            jobs.pop(oldest_id, None)
        else:
            break


async def worker_loop(worker_index: int) -> None:
    while True:
        try:
            payload = await job_queue.get()
        except asyncio.CancelledError:
            break

        job_id = payload.job_id
        record = jobs.get(job_id)
        if not record:
            job_queue.task_done()
            continue

        record["status"] = "running"
        record["startedAt"] = _now_iso()
        record["assignedWorker"] = worker_index
        record["queueSizeWhenStarted"] = job_queue.qsize()

        try:
            result = await scrape_as_json(
                payload.email,
                payload.password,
                payload.exclude_nickname,
            )
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            record["error"] = "Worker stopped during execution"
            record["finishedAt"] = _now_iso()
            job_queue.task_done()
            raise
        except Exception as exc:  # pylint: disable=broad-except
            record["status"] = "failed"
            record["error"] = str(exc)
            record["finishedAt"] = _now_iso()
        else:
            record["result"] = result
            record["status"] = "succeeded"
            record["error"] = None
            record["finishedAt"] = _now_iso()
        finally:
            start_at = record.get("startedAt")
            finished_at = record.get("finishedAt")
            if start_at and finished_at:
                try:
                    start_dt = datetime.fromisoformat(start_at)
                    finish_dt = datetime.fromisoformat(finished_at)
                    record["durationSeconds"] = round((finish_dt - start_dt).total_seconds(), 3)
                except ValueError:
                    pass
            record["lastUpdated"] = _now_iso()
            job_queue.task_done()
            _prune_history()


@app.on_event("startup")
async def start_workers() -> None:
    if workers:
        return
    for index in range(MAX_WORKERS):
        task = asyncio.create_task(worker_loop(index + 1), name=f"scrape-worker-{index + 1}")
        workers.append(task)


@app.on_event("shutdown")
async def stop_workers() -> None:
    for task in workers:
        task.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
    workers.clear()


@app.post("/api/scrape")
async def enqueue_scrape(payload: ScrapeRequest) -> Dict[str, Any]:
    email = (payload.email or "").strip()
    password = payload.password or ""
    exclude = (payload.excludeNickname or "").strip()

    if not email or not password or not exclude:
        raise HTTPException(status_code=422, detail="Email, password, and nickname are required.")

    pending_jobs = job_queue.qsize()
    if QUEUE_LIMIT and pending_jobs >= QUEUE_LIMIT:
        raise HTTPException(status_code=429, detail="Scraper queue is currently full. Please retry in a moment.")

    job_id = uuid.uuid4().hex
    submitted_at = _now_iso()
    jobs[job_id] = {
        "status": "queued",
        "submittedAt": submitted_at,
        "startedAt": None,
        "finishedAt": None,
        "result": None,
        "error": None,
        "queueSizeOnEnqueue": pending_jobs,
    }
    job_history.append(job_id)
    _prune_history()

    await job_queue.put(JobPayload(job_id, email, password, exclude))

    return {
        "jobId": job_id,
        "status": "queued",
        "submittedAt": submitted_at,
        "queueSize": job_queue.qsize(),
        "maxConcurrency": MAX_WORKERS,
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> Dict[str, Any]:
    record = jobs.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found.")

    snapshot = {key: value for key, value in record.items() if value is not None}
    snapshot["jobId"] = job_id
    snapshot["queueSize"] = job_queue.qsize()
    return snapshot


@app.get("/api/system")
async def system_status() -> Dict[str, Any]:
    running = sum(1 for item in jobs.values() if item.get("status") == "running")
    completed = sum(1 for item in jobs.values() if item.get("status") == "succeeded")
    failed = sum(1 for item in jobs.values() if item.get("status") == "failed")
    return {
        "queueSize": job_queue.qsize(),
        "running": running,
        "completed": completed,
        "failed": failed,
        "maxConcurrency": MAX_WORKERS,
    }


@app.get("/healthz")
async def healthcheck() -> Dict[str, Any]:
    return {"status": "ok", "queueSize": job_queue.qsize()}


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "service": "bandwith-scraper",
        "maxConcurrency": MAX_WORKERS,
        "queueSize": job_queue.qsize(),
        "historyLimit": MAX_HISTORY,
    }
