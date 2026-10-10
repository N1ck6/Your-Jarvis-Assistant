"""Browser tasks on a schedule: "каждый день в 9 проверяй цену на AirPods на озоне и скажи, если подешевеют".

Kept in data/web_jobs.json and survive restarts. At the time the agent runs the task quietly (no window, no
questions); a model compares the result with the previous one and Jarvis speaks only if the user's condition holds
(or always, when there is no condition).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable

from assistant.nlu import sleep_until
from assistant.paths import DATA_DIR
from assistant.when import next_time

log = logging.getLogger("web")
FILE = DATA_DIR / "web_jobs.json"


@dataclass
class WebJob:
    id: str
    task: str                 # what the agent does each time
    hour: int
    minute: int
    repeat: str = "daily"     # "" (once) | daily | weekdays | weekends | weekly:<0-6>
    due: float = 0.0          # unix time of the next run
    last_summary: str = ""    # key numbers of the last result, for comparison
    last_run: float = 0.0


class Scheduler:
    def __init__(self, run: Callable[[WebJob], Awaitable[None]], path: Path = FILE) -> None:
        self.path = path
        self.run = run
        self.jobs: dict[str, WebJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        try:
            for item in json.loads(path.read_text(encoding="utf-8")):
                job = WebJob(**item)
                self.jobs[job.id] = job
        except (OSError, ValueError, TypeError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(j) for j in self.jobs.values()], ensure_ascii=False, indent=1),
                             encoding="utf-8")

    def start(self) -> None:
        now = time.time()
        for job in list(self.jobs.values()):
            if job.due < now - 3600 and job.repeat:
                job.due = self._next(job, dt.datetime.now()).timestamp()   # missed while the PC was off
            self._arm(job)
        self.save()

    def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    @staticmethod
    def _next(job: WebJob, after: dt.datetime) -> dt.datetime:
        return next_time(after, job.hour, job.minute, job.repeat)

    def add(self, task: str, at: dt.datetime, repeat: str) -> WebJob:
        job = WebJob(uuid.uuid4().hex[:8], task, at.hour, at.minute, repeat, at.timestamp())
        self.jobs[job.id] = job
        self.save()
        self._arm(job)
        log.info("Задача в браузере по расписанию: %s, %s", task, dt.datetime.fromtimestamp(job.due))
        return job

    def remove(self, job_id: str) -> WebJob | None:
        job = self.jobs.pop(job_id, None)
        task = self._tasks.pop(job_id, None)
        if task is not None:
            task.cancel()
        if job is not None:
            self.save()
        return job

    def _arm(self, job: WebJob) -> None:
        old = self._tasks.pop(job.id, None)
        if old is not None:
            old.cancel()
        try:
            self._tasks[job.id] = asyncio.get_running_loop().create_task(self._wait(job))
        except RuntimeError:
            pass   # no loop yet (tests, startup): start() arms it

    async def _wait(self, job: WebJob) -> None:
        await sleep_until(job.due)
        if job.id not in self.jobs:
            return
        try:
            await self.run(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Задача по расписанию %s сломалась", job.id)
        job.last_run = time.time()
        if job.repeat:
            job.due = self._next(job, dt.datetime.now()).timestamp()
            self.save()
            self._tasks[job.id] = asyncio.get_running_loop().create_task(self._wait(job))
        else:
            self.jobs.pop(job.id, None)
            self._tasks.pop(job.id, None)
            self.save()
