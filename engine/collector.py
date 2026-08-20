import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

class AutoCollector:
    def __init__(self, callback):
        self.callback = callback
        self.enabled = True
        self.last_run = None
        self.next_run = None
        self.last_error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def state(self):
        return {
            "enabled": self.enabled,
            "last_run_ist": self.last_run.isoformat() if self.last_run else None,
            "next_run_ist": self.next_run.isoformat() if self.next_run else None,
            "last_error": self.last_error,
            "cadence": "Every 15 minutes, weekdays 09:30–15:30 IST",
        }

    def _inside_window(self, dt):
        if dt.weekday() >= 5:
            return False
        mins = dt.hour * 60 + dt.minute
        return 9 * 60 + 30 <= mins <= 15 * 60 + 30

    def _next_quarter(self, now):
        base = now.replace(second=0, microsecond=0)
        minute = ((base.minute // 15) + 1) * 15
        if minute >= 60:
            candidate = base.replace(minute=0) + timedelta(hours=1)
        else:
            candidate = base.replace(minute=minute)

        while True:
            if candidate.weekday() < 5:
                mins = candidate.hour * 60 + candidate.minute
                if 9 * 60 + 30 <= mins <= 15 * 60 + 30:
                    return candidate
                if mins < 9 * 60 + 30:
                    return candidate.replace(hour=9, minute=30)
            candidate = (candidate + timedelta(days=1)).replace(hour=9, minute=30)

    def _loop(self):
        now = datetime.now(IST)
        if self.enabled and self._inside_window(now):
            try:
                self.callback("auto-startup")
                self.last_run = datetime.now(IST)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)

        while not self._stop.is_set():
            now = datetime.now(IST)
            target = self._next_quarter(now)
            self.next_run = target
            wait = max(1.0, (target - now).total_seconds())
            if self._stop.wait(min(wait, 60.0)):
                break
            if wait > 60:
                continue

            now = datetime.now(IST)
            if self.enabled and self._inside_window(now) and abs((now - target).total_seconds()) <= 90:
                try:
                    self.callback("auto-15m")
                    self.last_run = datetime.now(IST)
                    self.last_error = None
                except Exception as exc:
                    self.last_error = str(exc)
                self._stop.wait(70)
