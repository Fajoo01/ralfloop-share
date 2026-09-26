"""Persistent power gate shared by the frontend and timer-driven shepherd."""
from contextlib import contextmanager
import fcntl
from functools import wraps

from .gpt_browser_cdp import CdpError


@contextmanager
def browser_operation(queue):
    # Separate open descriptions serialize threads as well as processes.
    with queue.path.with_suffix(".power.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class PowerGuardedCdp:
    def __init__(self, cdp, queue):
        self.raw = cdp
        self.queue = queue

    def __getattr__(self, name):
        value = getattr(self.raw, name)
        if not callable(value):
            return value

        @wraps(value)
        def guarded(*args, **kwargs):
            with browser_operation(self.queue):
                if not self.queue.power_enabled():
                    raise CdpError("gpt_browser_power_off")
                return value(*args, **kwargs)
        return guarded
