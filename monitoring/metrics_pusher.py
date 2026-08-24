"""
MetricsPusher — pushes audit session metrics to Prometheus Pushgateway.

One instance per session, mirrors DatabaseManager lifecycle:
    pusher = MetricsPusher(account_id='probe_01', session_id='...')
    pusher.connect()          # verifies the gateway is reachable (fails loudly)
    ...
    pusher.record_posts_collected(42)
    pusher.record_error('timeout')
    pusher.push()             # strategic mid-session push
    ...
    pusher.finalize(duration_seconds=1800, status='completed')

Uses job='instagram_audit' with grouping_key={account_id, session_id} so each
session's metrics are isolated in the gateway. pushadd_to_gateway accumulates
counters across multiple pushes within the same session — use push() freely.
"""
import logging
import os
import socket
import urllib.error
from typing import Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, Summary
from prometheus_client import pushadd_to_gateway, push_to_gateway


DEFAULT_PUSHGATEWAY_URL = os.environ.get('PUSHGATEWAY_URL', '127.0.0.1:9091')
JOB_NAME = 'instagram_audit'


class MetricsPusher:
    def __init__(
        self,
        account_id: str,
        session_id: str,
        pushgateway_url: str = DEFAULT_PUSHGATEWAY_URL,
    ):
        self.account_id = account_id
        self.session_id = session_id
        self.pushgateway_url = pushgateway_url
        self.logger = logging.getLogger(f'metrics_pusher_{account_id}')

        self.registry = CollectorRegistry()

        # Counters — accumulate over the session
        self.posts_collected = Counter(
            'instagram_posts_collected_total',
            'Total posts collected per session',
            registry=self.registry,
        )
        self.posts_suggested = Counter(
            'instagram_posts_suggested_total',
            'Suggested posts encountered per session',
            registry=self.registry,
        )
        self.posts_followed = Counter(
            'instagram_posts_followed_total',
            'Posts from followed accounts per session',
            registry=self.registry,
        )
        self.api_requests = Counter(
            'instagram_api_requests_total',
            'Instagram API responses intercepted per session',
            ['endpoint'],
            registry=self.registry,
        )
        self.errors = Counter(
            'instagram_errors_total',
            'Collection errors per session',
            ['error_type'],
            registry=self.registry,
        )

        # Gauges — single current value
        self.account_health = Gauge(
            'instagram_account_health',
            'Health score for the account (1.0 = healthy, 0.0 = blocked/suspended)',
            registry=self.registry,
        )
        self.session_duration = Gauge(
            'instagram_session_duration_seconds',
            'Duration of the session in seconds',
            registry=self.registry,
        )
        self.session_status = Gauge(
            'instagram_session_status',
            'Session status (1 = completed, 0 = errored, 0.5 = running)',
            registry=self.registry,
        )

        self.account_health.set(1.0)  # default healthy until proven otherwise
        self.session_status.set(0.5)  # running

    @property
    def _grouping_key(self) -> dict:
        return {'account_id': self.account_id, 'session_id': self.session_id}

    def connect(self) -> None:
        """Verify the gateway is reachable by doing an initial empty push. Fails loudly."""
        # Simple TCP probe — push_to_gateway does its own HTTP but we want fast-fail
        # before the session spends 30 minutes collecting data it can't report on.
        host, _, port = self.pushgateway_url.partition(':')
        port = int(port) if port else 9091
        try:
            with socket.create_connection((host, port), timeout=5):
                pass
        except (OSError, ValueError) as e:
            raise RuntimeError(
                f"Pushgateway unreachable at {self.pushgateway_url}: {e}"
            ) from e

        # Initial push registers the session in the gateway with zero counters + default gauges.
        self.push()

    def record_posts_collected(self, count: int) -> None:
        self.posts_collected.inc(count)

    def record_posts_suggested(self, count: int) -> None:
        self.posts_suggested.inc(count)

    def record_posts_followed(self, count: int) -> None:
        self.posts_followed.inc(count)

    def record_api_intercept(self, endpoint: str, count: int = 1) -> None:
        self.api_requests.labels(endpoint=endpoint).inc(count)

    def record_error(self, error_type: str) -> None:
        self.errors.labels(error_type=error_type).inc()

    def set_account_health(self, score: float) -> None:
        """0.0 = blocked/suspended, 1.0 = healthy. Values in between for degraded states."""
        self.account_health.set(score)

    def push(self) -> None:
        """Strategic mid-session push. Uses pushadd so counters accumulate."""
        try:
            pushadd_to_gateway(
                self.pushgateway_url,
                job=JOB_NAME,
                registry=self.registry,
                grouping_key=self._grouping_key,
            )
        except (urllib.error.URLError, OSError) as e:
            # push() during session is advisory — log and continue.
            # connect() and finalize() are the ones that fail loudly.
            self.logger.warning(f"Mid-session metric push failed: {e}")

    def finalize(self, duration_seconds: float, status: str) -> None:
        """
        Push final state at session close. Fails loudly — the thesis needs this
        data to be observable even if the collector crashed partway.

        status: 'completed' | 'errored'
        """
        self.session_duration.set(duration_seconds)
        self.session_status.set(1.0 if status == 'completed' else 0.0)

        pushadd_to_gateway(
            self.pushgateway_url,
            job=JOB_NAME,
            registry=self.registry,
            grouping_key=self._grouping_key,
        )
