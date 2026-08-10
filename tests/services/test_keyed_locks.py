from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from ticket_reviewer.services.keyed_locks import KeyedLockRegistry


def test_same_key_is_serialized_and_entry_is_evicted_after_final_waiter():
    registry = KeyedLockRegistry[int]()
    entered = Event()
    release = Event()
    state_lock = Lock()
    active = 0
    peak = 0

    def work():
        nonlocal active, peak
        with registry.hold(7):
            with state_lock:
                active += 1
                peak = max(peak, active)
            entered.set()
            release.wait(timeout=2)
            with state_lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(work)
        assert entered.wait(timeout=1)
        second = pool.submit(work)
        release.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert peak == 1
    assert registry.retained_count == 0


def test_different_keys_proceed_independently_and_exceptions_evict():
    registry = KeyedLockRegistry[int]()
    both_entered = Event()
    entered = 0
    guard = Lock()

    def work(key):
        nonlocal entered
        with registry.hold(key):
            with guard:
                entered += 1
                if entered == 2:
                    both_entered.set()
            assert both_entered.wait(timeout=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(work, key) for key in (1, 2)]
        for future in futures:
            future.result(timeout=2)

    with pytest.raises(RuntimeError):
        with registry.hold(3):
            raise RuntimeError("synthetic")

    assert registry.retained_count == 0


def test_thousands_of_unique_keys_leave_no_retained_state():
    registry = KeyedLockRegistry[int]()
    for key in range(5000):
        with registry.hold(key):
            pass
    assert registry.retained_count == 0
