"""Bounded retries for replay-safe requests and atomic local checkpoints."""
import json
import logging
import os
import random
import tempfile
import time
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

READ_METHODS = frozenset({'read', 'search', 'search_read', 'search_count', 'fields_get', 'context_get', 'default_get'})
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_cooldowns = {}
_cooldown_lock = threading.Lock()


def _open_circuit(host):
    if host:
        with _cooldown_lock:
            _cooldowns[host] = time.monotonic() + 60


class NetworkRequestError(RuntimeError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def retry_delay(response, attempt):
    delay = min(5 * 2 ** attempt, 60) + random.uniform(0, 1)
    raw = response.headers.get('Retry-After') if response is not None else None
    if raw:
        try:
            server_delay = float(raw)
        except (ValueError, TypeError):
            try:
                server_delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                server_delay = 0
        delay = max(delay, server_delay)
    return delay


def request_with_retry(send, *, replay_safe, operation, logger=None,
                       attempts=6, budget_seconds=300, decode=None, timeout=(10, 90), **kwargs):
    """Retry reads/token acquisition; never replay an uncertain business mutation.

    The time budget bounds retries and each socket timeout. Requests' read timeout
    is an inactivity timeout, so a continuously streaming response may take longer.
    No URLs, credentials, response bodies or payloads are included in errors.
    """
    logger = logger or logging.getLogger(__name__)
    host = urlsplit(kwargs.get('url', '')).netloc
    with _cooldown_lock:
        remaining_cooldown = _cooldowns.get(host, 0) - time.monotonic()
    if remaining_cooldown > 0:
        raise NetworkRequestError(f'{operation}: connection recovery cooldown; request was not sent')
    deadline = time.monotonic() + budget_seconds
    max_attempts = max(1, attempts) if replay_safe else 1
    for attempt in range(max_attempts):
        response = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NetworkRequestError(f'{operation}: recovery budget exhausted')
        connect, read = timeout if isinstance(timeout, tuple) else (10, timeout)
        effective_timeout = (min(connect, max(0.1, remaining / 2)), min(read, max(0.1, remaining / 2)))
        try:
            response = send(timeout=effective_timeout, **kwargs)
            status = response.status_code
            if 200 <= status < 300:
                return decode(response) if decode else response
            reason = f'HTTP {status}'
            if status not in TRANSIENT_STATUS:
                raise NetworkRequestError(f'{operation}: {reason}')
        except requests.exceptions.SSLError as exc:
            raise NetworkRequestError(f'{operation}: TLS verification failed', uncertain=not replay_safe) from exc
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError,
                ValueError) as exc:
            reason = type(exc).__name__
        except requests.exceptions.RequestException as exc:
            raise NetworkRequestError(f'{operation}: {type(exc).__name__}', uncertain=not replay_safe) from exc
        finally:
            if response is not None and decode is not None:
                response.close()
        if not replay_safe:
            _open_circuit(host)
            raise NetworkRequestError(f'{operation}: {reason}; result uncertain, request was not replayed', uncertain=True)
        if attempt + 1 >= max_attempts:
            _open_circuit(host)
            raise NetworkRequestError(f'{operation}: {reason}; exhausted {max_attempts} attempts')
        delay = retry_delay(response, attempt)
        if response is not None:
            response.close()
        if delay >= deadline - time.monotonic():
            _open_circuit(host)
            raise NetworkRequestError(f'{operation}: {reason}; retry deferred to next scheduled run')
        logger.warning('%s: %s; retry %s/%s in %.1fs', operation, reason, attempt + 2, max_attempts, delay)
        time.sleep(delay)
    raise NetworkRequestError(f'{operation}: retry budget exhausted')


def atomic_write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(path, value):
    atomic_write_bytes(path, json.dumps(value, indent=2, ensure_ascii=True).encode('utf-8'))
