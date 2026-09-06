import math
import threading
import time
from app.notion_client import NotionOpusAPI


class AccountPool:
    def __init__(self, accounts):
        if not accounts:
            raise ValueError('At least one account is required.')
        self.clients = [NotionOpusAPI(account) for account in accounts]
        self.cooldown_until = [0.0] * len(self.clients)
        self._current_index = 0
        self._lock = threading.Lock()

    def get_client(self, wait_if_cooling=True, preferred_owner=None):
        deadline = time.monotonic() + 15
        while True:
            with self._lock:
                now = time.time()
                usable = [i for i, until in enumerate(self.cooldown_until) if math.isfinite(until)]
                if not usable:
                    raise RuntimeError('All accounts are disabled; refresh their credentials and restart.')
                ready = [i for i in usable if self.cooldown_until[i] <= now]
                preferred = next((i for i in ready if f'{self.clients[i].user_id}:{self.clients[i].space_id}' == preferred_owner), None)
                if preferred is not None:
                    return self.clients[preferred]
                for offset in range(len(self.clients)):
                    index = (self._current_index + offset) % len(self.clients)
                    if index in ready:
                        self._current_index = (index + 1) % len(self.clients)
                        return self.clients[index]
                wait = max(0.05, min(self.cooldown_until[i] for i in usable) - now)
            if not wait_if_cooling or time.monotonic() + wait > deadline:
                raise RuntimeError('Accounts are cooling down; retry later.')
            # Never sleep while holding the pool lock. Async callers use a worker thread.
            time.sleep(wait)

    def mark_failed(self, client, cooldown_seconds=5):
        with self._lock:
            if client in self.clients:
                index = self.clients.index(client)
                self.cooldown_until[index] = max(self.cooldown_until[index], time.time() + max(0, cooldown_seconds))

    def get_status_summary(self):
        with self._lock:
            now = time.time()
            disabled = sum(not math.isfinite(ts) for ts in self.cooldown_until)
            active = sum(ts <= now for ts in self.cooldown_until)
            return {'total': len(self.clients), 'active': active, 'disabled': disabled,
                    'cooling': len(self.clients) - active - disabled}
