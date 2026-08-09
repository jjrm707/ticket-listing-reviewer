"""Bounded keyed lock ownership for per-record single-flight operations."""

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Generic, Hashable, Iterator, TypeVar


Key = TypeVar("Key", bound=Hashable)


@dataclass(slots=True)
class _Entry:
    lock: Lock
    users: int = 0


class KeyedLockRegistry(Generic[Key]):
    """Serialize equal keys while retaining state only for active users/waiters."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._entries: dict[Key, _Entry] = {}

    @contextmanager
    def hold(self, key: Key) -> Iterator[None]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(Lock())
                self._entries[key] = entry
            entry.users += 1
        try:
            with entry.lock:
                yield
        finally:
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    del self._entries[key]

    @property
    def retained_count(self) -> int:
        with self._guard:
            return len(self._entries)
