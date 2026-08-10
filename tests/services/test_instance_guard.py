from pathlib import Path

import pytest

from ticket_reviewer.services.instance_guard import (
    FileInstanceGuard,
    InstanceAlreadyRunning,
    lock_path_for_database_url,
)


def test_lock_path_is_deterministic_and_adjacent_to_database(tmp_path):
    database = tmp_path / "private.db"
    lock = lock_path_for_database_url(f"sqlite:///{database}", allowed_root=tmp_path)
    assert lock == database.resolve().with_suffix(".db.lock")


def test_guard_contention_is_generic_and_handle_release_allows_reacquire(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'private.db'}"
    first = FileInstanceGuard.for_database_url(database_url, allowed_root=tmp_path)
    second = FileInstanceGuard.for_database_url(database_url, allowed_root=tmp_path)
    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunning) as caught:
            second.acquire()
        assert str(caught.value) == "application is already running"
        assert str(tmp_path) not in str(caught.value)
    finally:
        first.release()

    second.acquire()
    second.release()
    second.release()


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:", "postgresql://local/db"])
def test_guard_rejects_non_file_database_urls_without_creating_paths(url):
    with pytest.raises(ValueError):
        lock_path_for_database_url(url, allowed_root=Path.cwd() / "data")


@pytest.mark.parametrize(
    "raw_path",
    [
        "../outside.db",
        "file:private.db?mode=rwc",
        "//server/share/private.db",
    ],
)
def test_guard_rejects_outside_uri_and_unc_paths_generically(tmp_path, raw_path):
    database_url = f"sqlite:///{raw_path}"
    with pytest.raises(ValueError) as caught:
        lock_path_for_database_url(database_url, allowed_root=tmp_path)
    exposed = f"{caught.value!s} {caught.value!r}"
    assert raw_path not in exposed
    assert str(tmp_path) not in exposed


def test_guard_rejects_absolute_database_outside_allowed_data_root(tmp_path):
    outside = tmp_path.parent / "outside.db"
    with pytest.raises(ValueError, match="invalid local database configuration"):
        lock_path_for_database_url(
            f"sqlite:///{outside}", allowed_root=tmp_path / "data"
        )


def test_guard_sanitizes_filesystem_failures_without_creating_other_paths(tmp_path, monkeypatch):
    database = tmp_path / "private.db"
    guard = FileInstanceGuard.for_database_url(
        f"sqlite:///{database}", allowed_root=tmp_path
    )

    def fail_mkdir(*_args, **_kwargs):
        raise PermissionError(f"denied at {database}")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    with pytest.raises(RuntimeError) as caught:
        guard.acquire()
    exposed = f"{caught.value!s} {caught.value!r}"
    assert str(database) not in exposed
    assert exposed.count("application instance guard unavailable") == 2


def test_guard_rejects_symlink_database_path_when_supported(tmp_path):
    target = tmp_path / "target.db"
    target.touch()
    link = tmp_path / "linked.db"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("creating symlinks is unavailable")
    with pytest.raises(ValueError):
        lock_path_for_database_url(f"sqlite:///{link}", allowed_root=tmp_path)


def test_guard_rejects_symlink_parent_when_supported(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is unavailable")
    with pytest.raises(ValueError):
        lock_path_for_database_url(
            f"sqlite:///{linked / 'private.db'}", allowed_root=tmp_path
        )


def test_guard_revalidates_every_ancestor_at_acquire_time(tmp_path, monkeypatch):
    intermediate = tmp_path / "intermediate"
    parent = intermediate / "nested"
    parent.mkdir(parents=True)
    guard = FileInstanceGuard.for_database_url(
        f"sqlite:///{parent / 'private.db'}", allowed_root=tmp_path
    )
    original_is_symlink = Path.is_symlink

    def substituted(path):
        if path == intermediate:
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", substituted)
    try:
        with pytest.raises(ValueError, match="invalid local database configuration"):
            guard.acquire()
    finally:
        guard.release()


def test_guard_rejects_intermediate_windows_junction_at_acquire_time(
    tmp_path, monkeypatch
):
    intermediate = tmp_path / "intermediate"
    parent = intermediate / "nested"
    parent.mkdir(parents=True)
    guard = FileInstanceGuard.for_database_url(
        f"sqlite:///{parent / 'private.db'}", allowed_root=tmp_path
    )
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda path: path == intermediate,
        raising=False,
    )
    try:
        with pytest.raises(ValueError, match="invalid local database configuration"):
            guard.acquire()
    finally:
        guard.release()
