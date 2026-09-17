import logging
import threading
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache

from django.conf import settings
from django.db import connection, transaction
from redis import Redis
from redis.exceptions import RedisError


logger = logging.getLogger(__name__)

_KEY_PREFIX = f"ecpmp:{settings.ENV}:advanced-search"
_CAPACITY_KEY = f"{_KEY_PREFIX}:capacity"
_USER_CAPACITY_KEY = f"{_KEY_PREFIX}:user-capacity:{{user_id}}"
_RATE_KEY = f"{_KEY_PREFIX}:rate:{{user_id}}:{{window}}"

_ACQUIRE_CAPACITY = """
local global_key = KEYS[1]
local user_key = KEYS[2]
local now_ms = tonumber(ARGV[1])
local expires_ms = tonumber(ARGV[2])
local global_limit = tonumber(ARGV[3])
local user_limit = tonumber(ARGV[4])
local token = ARGV[5]

redis.call('ZREMRANGEBYSCORE', global_key, '-inf', now_ms)
redis.call('ZREMRANGEBYSCORE', user_key, '-inf', now_ms)

if redis.call('ZCARD', global_key) >= global_limit then
    return -2
end
if redis.call('ZCARD', user_key) >= user_limit then
    return -1
end

redis.call('ZADD', global_key, expires_ms, token)
redis.call('ZADD', user_key, expires_ms, token)
redis.call('PEXPIRE', global_key, expires_ms - now_ms)
redis.call('PEXPIRE', user_key, expires_ms - now_ms)
return 1
"""

_RELEASE_CAPACITY = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return 1
"""

_INCREMENT_RATE = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


class AdvancedSearchBusyError(RuntimeError):
    def __init__(self, retry_after=2):
        super().__init__("Advanced search is currently at capacity.")
        self.retry_after = retry_after


class AdvancedSearchRateLimitError(RuntimeError):
    def __init__(self, retry_after):
        super().__init__("Advanced search request rate exceeded.")
        self.retry_after = retry_after


@lru_cache(maxsize=1)
def _redis_client():
    return Redis(
        host=settings.REDIS_HOST,
        port=6379,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        decode_responses=True,
    )


class _LocalCapacityGuard:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self._active_by_user = {}

    def acquire(self, user_id):
        global_limit = max(1, settings.ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS)
        user_limit = max(1, settings.ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS_PER_USER)
        with self._lock:
            if self._active >= global_limit:
                return False
            if self._active_by_user.get(user_id, 0) >= user_limit:
                return False
            self._active += 1
            self._active_by_user[user_id] = self._active_by_user.get(user_id, 0) + 1
            return True

    def release(self, user_id):
        with self._lock:
            self._active = max(0, self._active - 1)
            active_for_user = self._active_by_user.get(user_id, 0) - 1
            if active_for_user > 0:
                self._active_by_user[user_id] = active_for_user
            else:
                self._active_by_user.pop(user_id, None)


_local_capacity = _LocalCapacityGuard()


def _wait_for_capacity(acquire):
    wait_seconds = max(
        0,
        float(getattr(settings, "ADVANCED_SEARCH_CAPACITY_WAIT_SECONDS", 3)),
    )
    poll_seconds = max(
        0.01,
        float(
            getattr(
                settings,
                "ADVANCED_SEARCH_CAPACITY_POLL_INTERVAL_MS",
                100,
            )
        )
        / 1000,
    )
    deadline = time.monotonic() + wait_seconds

    while True:
        if acquire():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_seconds, remaining))


def _enforce_rate_limit(client, user_id, now):
    limit = settings.ADVANCED_SEARCH_REQUESTS_PER_MINUTE
    if limit <= 0:
        return
    window = int(now // 60)
    retry_after = max(1, 60 - int(now % 60))
    count = client.eval(
        _INCREMENT_RATE,
        1,
        _RATE_KEY.format(user_id=user_id, window=window),
        61,
    )
    if int(count) > limit:
        raise AdvancedSearchRateLimitError(retry_after)


@contextmanager
def advanced_search_capacity(user_id):
    now = time.time()
    token = f"{user_id}:{uuid.uuid4().hex}"
    lease_ms = max(1, settings.ADVANCED_SEARCH_CAPACITY_LEASE_SECONDS) * 1000
    global_key = _CAPACITY_KEY
    user_key = _USER_CAPACITY_KEY.format(user_id=user_id)
    client = None
    using_local_guard = False
    acquired = False

    try:
        client = _redis_client()
        _enforce_rate_limit(client, user_id, now)

        def acquire_redis_capacity():
            attempt_ms = int(time.time() * 1000)
            return (
                int(
                    client.eval(
                        _ACQUIRE_CAPACITY,
                        2,
                        global_key,
                        user_key,
                        attempt_ms,
                        attempt_ms + lease_ms,
                        max(1, settings.ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS),
                        max(
                            1,
                            settings.ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS_PER_USER,
                        ),
                        token,
                    )
                )
                == 1
            )
        acquired = _wait_for_capacity(acquire_redis_capacity)
        if not acquired:
            raise AdvancedSearchBusyError()
    except AdvancedSearchRateLimitError:
        raise
    except AdvancedSearchBusyError:
        raise
    except RedisError as exc:
        logger.warning("Advanced search Redis guard unavailable; using local capacity guard: %s", exc)
        using_local_guard = True
        acquired = _wait_for_capacity(lambda: _local_capacity.acquire(user_id))
        if not acquired:
            raise AdvancedSearchBusyError() from exc

    try:
        yield
    finally:
        if not acquired:
            return
        if using_local_guard:
            _local_capacity.release(user_id)
        elif client is not None:
            try:
                client.eval(_RELEASE_CAPACITY, 2, global_key, user_key, token)
            except RedisError as exc:
                logger.warning("Could not release advanced search Redis capacity lease: %s", exc)


@contextmanager
def advanced_search_statement_timeout():
    timeout_ms = settings.ADVANCED_SEARCH_STATEMENT_TIMEOUT_MS
    if timeout_ms <= 0 or connection.vendor != "postgresql":
        yield
        return

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
        yield


def is_statement_timeout(exc):
    error = exc
    while error is not None:
        if "statement timeout" in str(error).casefold():
            return True
        error = getattr(error, "__cause__", None)
    return False
