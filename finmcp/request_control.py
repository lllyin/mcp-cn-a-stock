"""Cooperative cancellation and monotonic budgets for synchronous I/O.

Budgets belong to one logical request, including its nested sends and retries.
They do not create threads or timers. An active blocking send must still finish
within its transport timeout; cancellation prevents waiting or sending again.
"""

import contextlib
import threading
import time
from urllib3.util import Timeout
from urllib3.exceptions import TimeoutStateError


class RequestCancelled(BaseException):
    """Control flow, not a provider failure that should start another fallback."""


class BudgetExceeded(TimeoutError):
    """The current logical HTTP request has used its total budget."""


_local = threading.local()


def current_cancel_event():
    return getattr(_local, "cancel_event", None)


def set_cancel_event(event):
    previous = current_cancel_event()
    _local.cancel_event = event
    return previous


def check_cancelled(event=None):
    event = event if event is not None else current_cancel_event()
    if event is not None and event.is_set():
        raise RequestCancelled()


def current_budget():
    return getattr(_local, "budget", None)


class Budget:
    def __init__(self, seconds):
        self.started = time.monotonic()
        self.deadline = self.started + max(0.0, seconds)
        parent = current_budget()
        if parent is not None:
            self.deadline = min(self.deadline, parent.deadline)

    def remaining(self):
        check_cancelled()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded("HTTP request budget exhausted")
        return remaining


@contextlib.contextmanager
def budget_scope(seconds, *, cancel_event=None):
    previous = current_budget()
    previous_event = current_cancel_event()
    if cancel_event is not None:
        set_cancel_event(cancel_event)
    budget = Budget(seconds)
    _local.budget = budget
    try:
        budget.remaining()
        yield budget
    finally:
        _local.budget = previous
        set_cancel_event(previous_event)


def wait(event, timeout):
    """Wait without holding a lock, stopping promptly after request cancellation."""
    deadline = time.monotonic() + max(0.0, timeout)
    budget = current_budget()
    if budget is not None:
        deadline = min(deadline, budget.deadline)
    while True:
        check_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if event.wait(min(0.05, remaining)):
            check_cancelled()
            return True


def sleep(seconds):
    event = current_cancel_event() or threading.Event()
    wait(event, seconds)
    budget = current_budget()
    if budget is not None:
        budget.remaining()


def capped_timeout(value, limit):
    """Keep tighter caller timeouts; never inherit an unbounded/180s default."""
    def cap(part):
        return limit if part is None or part is Timeout.DEFAULT_TIMEOUT else min(float(part), limit)

    if isinstance(value, (tuple, list)):
        return tuple(cap(part) for part in value)
    # urllib3 Timeout, accepted by requests as well.
    if isinstance(value, Timeout):
        value = value.clone()
        try:
            read = value.read_timeout
        except TimeoutStateError:
            read = None
        total = value.total
        if total is None or total is Timeout.DEFAULT_TIMEOUT:
            total = 2 * limit
        return Timeout(total=min(total, 2 * limit),
                       connect=cap(value.connect_timeout), read=cap(read))
    return cap(value)


def timeout_seconds(value):
    if isinstance(value, Timeout):
        return min(value.total, value.connect_timeout + value.read_timeout)
    return sum(value) if isinstance(value, (tuple, list)) else 2 * value
