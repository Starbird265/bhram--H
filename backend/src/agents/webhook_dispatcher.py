"""
Webhook Dispatcher — Retry Queue for Outbound Webhooks

Provides reliable delivery of webhook payloads to external agents with:
  - Exponential backoff: 1s → 2s → 4s → 8s → 16s (max 5 retries)
  - Dead-letter logging after max retries
  - Thread-safe queue for non-blocking dispatch
  - Background worker started during app lifespan

Usage:
    dispatcher = WebhookDispatcher()
    dispatcher.start()
    dispatcher.enqueue(url, payload, headers, task_id, agent_id)
    # ... on shutdown:
    dispatcher.stop()
"""

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List

import requests

logger = logging.getLogger(__name__)


@dataclass
class WebhookJob:
    """A single webhook delivery attempt."""
    url: str
    payload: dict
    headers: dict
    task_id: str
    agent_id: str
    attempt: int = 0
    max_retries: int = 5
    created_at: float = field(default_factory=time.time)


@dataclass
class DeadLetterEntry:
    """Record of a permanently failed webhook delivery."""
    job: WebhookJob
    last_error: str
    failed_at: float = field(default_factory=time.time)


class WebhookDispatcher:
    """
    Thread-safe webhook dispatcher with exponential backoff retry.

    Start the background worker with .start(), enqueue jobs with .enqueue(),
    and stop cleanly with .stop().
    """

    def __init__(self, max_retries: int = 5, base_delay: float = 1.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._queue: queue.Queue[WebhookJob] = queue.Queue()
        self._dead_letters: List[DeadLetterEntry] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._on_failure_callback = None

    def start(self):
        """Start the background worker thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name="WebhookDispatcher",
        )
        self._thread.start()
        logger.info("WebhookDispatcher started")

    def stop(self):
        """Stop the background worker. Waits for current job to finish."""
        self._running = False
        # Unblock the worker if it's waiting on the queue
        self._queue.put(None)  # type: ignore
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("WebhookDispatcher stopped")

    def set_failure_callback(self, callback):
        """
        Set a callback for permanent failures.
        Signature: callback(task_id: str, agent_id: str, error: str)
        """
        self._on_failure_callback = callback

    def enqueue(
        self,
        url: str,
        payload: dict,
        headers: dict,
        task_id: str,
        agent_id: str,
    ):
        """
        Queue a webhook for delivery.

        Args:
            url: The external agent's webhook URL.
            payload: JSON payload to send.
            headers: Pre-built headers (from webhook_security.build_authenticated_headers).
            task_id: Task ID for tracking.
            agent_id: Target agent ID for logging.
        """
        job = WebhookJob(
            url=url,
            payload=payload,
            headers=headers,
            task_id=task_id,
            agent_id=agent_id,
            max_retries=self.max_retries,
        )
        self._queue.put(job)
        logger.info(
            f"[Dispatcher] Queued webhook for agent '{agent_id}' "
            f"task '{task_id}' → {url}"
        )

    def _worker(self):
        """Background worker: processes the queue with retry logic."""
        while self._running:
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if job is None:
                # Shutdown signal
                break

            self._attempt_delivery(job)

    def _attempt_delivery(self, job: WebhookJob):
        """Try to deliver a webhook, retry on failure."""
        while job.attempt <= job.max_retries:
            try:
                resp = requests.post(
                    job.url,
                    json=job.payload,
                    headers=job.headers,
                    timeout=10,
                )

                if resp.status_code < 400:
                    logger.info(
                        f"[Dispatcher] ✓ Delivered to '{job.agent_id}' "
                        f"task '{job.task_id}' (attempt {job.attempt + 1}, "
                        f"status {resp.status_code})"
                    )
                    return  # Success

                error_msg = (
                    f"HTTP {resp.status_code}: {resp.text[:200]}"
                )
                logger.warning(
                    f"[Dispatcher] Agent '{job.agent_id}' returned "
                    f"{resp.status_code} for task '{job.task_id}' "
                    f"(attempt {job.attempt + 1})"
                )

            except requests.exceptions.RequestException as e:
                error_msg = str(e)
                logger.warning(
                    f"[Dispatcher] Network error for agent '{job.agent_id}' "
                    f"task '{job.task_id}': {e} (attempt {job.attempt + 1})"
                )

            job.attempt += 1

            if job.attempt <= job.max_retries:
                delay = self.base_delay * (2 ** (job.attempt - 1))
                logger.info(
                    f"[Dispatcher] Retrying in {delay:.1f}s "
                    f"(attempt {job.attempt + 1}/{job.max_retries + 1})"
                )
                time.sleep(delay)

        # All retries exhausted → dead letter
        entry = DeadLetterEntry(job=job, last_error=error_msg)
        self._dead_letters.append(entry)
        logger.error(
            f"[Dispatcher] ✗ DEAD LETTER: agent '{job.agent_id}' "
            f"task '{job.task_id}' after {job.max_retries + 1} attempts. "
            f"Last error: {error_msg}"
        )

        if self._on_failure_callback:
            try:
                self._on_failure_callback(
                    job.task_id, job.agent_id, error_msg
                )
            except Exception as cb_err:
                logger.error(
                    f"[Dispatcher] Failure callback error: {cb_err}"
                )

    def get_dead_letters(self, limit: int = 50) -> List[Dict]:
        """Get recent dead-lettered webhook deliveries for dashboard."""
        entries = self._dead_letters[-limit:]
        return [
            {
                "task_id": e.job.task_id,
                "agent_id": e.job.agent_id,
                "url": e.job.url,
                "attempts": e.job.attempt,
                "last_error": e.last_error,
                "failed_at": e.failed_at,
            }
            for e in entries
        ]

    def queue_size(self) -> int:
        """Current number of pending webhook jobs."""
        return self._queue.qsize()
