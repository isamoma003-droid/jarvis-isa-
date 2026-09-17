"""Background task registry for delegated sub-agent work."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from .memory import iso, utcnow


@dataclass
class Task:
    """One delegated unit of work."""

    id: str
    description: str
    profile: str
    status: str = "running"  # running | done | failed
    created_at: str = field(default_factory=lambda: iso(utcnow()))
    finished_at: str | None = None
    result: str = ""
    error: str = ""
    log: list[str] = field(default_factory=list)

    @property
    def duration(self) -> str:
        end = datetime.fromisoformat(self.finished_at) if self.finished_at else utcnow()
        seconds = int((end - datetime.fromisoformat(self.created_at)).total_seconds())
        return f"{seconds}s"

    def summary(self) -> str:
        head = f"[{self.id}] {self.status} after {self.duration}: {self.description}"
        if self.status == "failed":
            return f"{head}\nerror: {self.error}"
        if self.status == "done":
            return f"{head}\n{self.result}"
        steps = ", ".join(self.log[-3:]) or "starting"
        return f"{head}\nprogress: {steps}"


class TaskRegistry:
    """Runs delegated work on a small thread pool and remembers the results."""

    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="jarvis-task")
        self._tasks: dict[str, Task] = {}
        self._futures: dict[str, Future] = {}
        self._lock = threading.RLock()

    def submit(self, description: str, profile: str, work: Callable[[Task], str]) -> Task:
        task = Task(id=f"task_{uuid.uuid4().hex[:6]}", description=description, profile=profile)
        with self._lock:
            self._tasks[task.id] = task

        def runner() -> str:
            try:
                task.result = work(task)
                task.status = "done"
                return task.result
            except Exception as exc:
                task.status = "failed"
                task.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                task.finished_at = iso(utcnow())

        with self._lock:
            self._futures[task.id] = self._pool.submit(runner)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> list[Task]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.created_at)

    def wait(self, task_id: str, timeout: float | None = None) -> Task | None:
        with self._lock:
            future = self._futures.get(task_id)
        if future is not None:
            try:
                future.result(timeout=timeout)
            except Exception:
                pass  # the failure is already recorded on the task
        return self.get(task_id)

    def running(self) -> list[Task]:
        return [task for task in self.all() if task.status == "running"]

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
