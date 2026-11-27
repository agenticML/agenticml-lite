"""Rate-limited warnings for failures that can repeat on every request."""
import threading
import time

DEFAULT_INTERVAL = 10.0


class RateLimitedLog:
    """Log at most once per interval; suppressed calls are counted into the next line."""

    def __init__(self, interval=DEFAULT_INTERVAL):
        self.interval = interval
        self._lock = threading.Lock()
        self._last = None
        self._suppressed = 0

    def take(self):
        """None while rate limited, else the number of calls suppressed since the last line."""
        now = time.monotonic()
        with self._lock:
            if self._last is not None and now - self._last < self.interval:
                self._suppressed += 1
                return None
            self._last = now
            suppressed, self._suppressed = self._suppressed, 0
            return suppressed

    def warning(self, log, message, *args):
        suppressed = self.take()
        if suppressed is None:
            return
        if suppressed:
            message += ' (%d more since last log)'
            args += (suppressed,)
        log.warning(message, *args)


_unavailable = {}
_unavailable_lock = threading.Lock()


def log_unavailable(log, what, exc, method, path):
    """Warn (at most every 10 seconds per ``what``) about a 503 for an unavailable store.

    Names only ``what``, the exception class, the method and the path without
    its query: no cookies, keys, query strings or driver text.
    """
    with _unavailable_lock:
        limiter = _unavailable.setdefault(what, RateLimitedLog())
    path = str(path).split('?', 1)[0][:200].encode('ascii', 'backslashreplace').decode('ascii')
    limiter.warning(log, what + ' (%s) %s %s', type(exc).__name__, str(method)[:16], path)
