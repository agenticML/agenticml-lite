"""Hourly refresh of today's row in ``stats_daily``, inside the server process."""
import logging
import threading

from .db.stats import StatsRepo

logger = logging.getLogger('agenticml.server.stats')
STATS_REFRESH = 3600


class StatsRefresher:
    def __init__(self, db, interval=STATS_REFRESH):
        self.db = db
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='agenticml-stats', daemon=True)
            self._thread.start()

    def stop(self, timeout=None):
        """Wait for a snapshot in progress (the database timeouts bound it); True when the thread ended."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def _run(self):
        while not self._stop.is_set():
            try:
                StatsRepo(self.db).snapshot(min_age=self.interval / 2)
            except Exception as exc:
                logger.warning('could not refresh usage history: %s', str(exc) or type(exc).__name__)
            self._stop.wait(self.interval)
