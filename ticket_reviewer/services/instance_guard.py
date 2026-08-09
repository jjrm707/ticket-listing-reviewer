"""OS-released nonblocking ownership of one local application instance."""

from pathlib import Path
import os
import stat
from typing import BinaryIO

from sqlalchemy.engine import make_url


_DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
_INVALID_DATABASE = "invalid local database configuration"


class InstanceAlreadyRunning(RuntimeError):
    """A generic local ownership conflict without path or process details."""

    def __init__(self) -> None:
        super().__init__("application is already running")


def _normalized_root(allowed_root: Path | None) -> Path:
    try:
        return Path(os.path.abspath(Path(allowed_root or _DEFAULT_DATA_ROOT)))
    except (OSError, TypeError, ValueError):
        raise ValueError(_INVALID_DATABASE) from None


def lock_path_for_database_url(
    database_url: str, *, allowed_root: Path | None = None
) -> Path:
    """Resolve a regular lock file beside the configured local SQLite database."""

    try:
        database = make_url(database_url)
    except Exception:
        raise ValueError(_INVALID_DATABASE) from None
    raw_path = database.database
    if (
        database.get_backend_name() != "sqlite"
        or not isinstance(raw_path, str)
        or not raw_path
        or raw_path == ":memory:"
        or bool(database.query)
        or "\x00" in raw_path
        or raw_path.casefold().startswith("file:")
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_path)
    ):
        raise ValueError(_INVALID_DATABASE)
    try:
        root = _normalized_root(allowed_root)
        raw_candidate = Path(raw_path)
        candidate = Path(
            os.path.abspath(raw_candidate if raw_candidate.is_absolute() else Path.cwd() / raw_candidate)
        )
        relative = candidate.relative_to(root)
    except (OSError, ValueError, TypeError):
        raise ValueError(_INVALID_DATABASE) from None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(_INVALID_DATABASE)
    _validate_data_local_path(root, candidate)
    return candidate.with_suffix(candidate.suffix + ".lock")


def _validate_data_local_path(root: Path, candidate: Path) -> None:
    """Reject existing symlink/special components without resolving through them."""

    try:
        if _is_link_or_reparse(root) or (root.exists() and not root.is_dir()):
            raise ValueError(_INVALID_DATABASE)
        current = root
        relative = candidate.relative_to(root)
        for part in relative.parts[:-1]:
            current /= part
            if _is_link_or_reparse(current) or (
                current.exists() and not current.is_dir()
            ):
                raise ValueError(_INVALID_DATABASE)
        if _is_link_or_reparse(candidate):
            raise ValueError(_INVALID_DATABASE)
        if candidate.exists() and not stat.S_ISREG(candidate.lstat().st_mode):
            raise ValueError(_INVALID_DATABASE)
    except ValueError:
        raise
    except OSError:
        raise ValueError(_INVALID_DATABASE) from None


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    if not path.exists():
        return False
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


class FileInstanceGuard:
    """Hold an advisory one-byte OS lock until explicit application cleanup."""

    def __init__(self, lock_path: Path, *, allowed_root: Path | None = None) -> None:
        self._path = lock_path
        self._allowed_root = _normalized_root(allowed_root or lock_path.parent)
        try:
            self._path.relative_to(self._allowed_root)
        except ValueError:
            raise ValueError(_INVALID_DATABASE) from None
        self._file: BinaryIO | None = None

    @classmethod
    def for_database_url(
        cls, database_url: str, *, allowed_root: Path | None = None
    ) -> "FileInstanceGuard":
        root = _normalized_root(allowed_root)
        return cls(
            lock_path_for_database_url(database_url, allowed_root=root),
            allowed_root=root,
        )

    def acquire(self) -> None:
        if self._file is not None:
            return
        try:
            _validate_data_local_path(self._allowed_root, self._path)
            parent = self._path.parent
            parent.mkdir(parents=True, exist_ok=True)
            _validate_data_local_path(self._allowed_root, self._path)
            handle = _open_regular_lock(self._path, self._allowed_root)
        except ValueError:
            raise
        except OSError:
            raise RuntimeError("application instance guard unavailable") from None
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
            handle.seek(0)
            _lock(handle)
        except BlockingIOError:
            handle.close()
            raise InstanceAlreadyRunning() from None
        except OSError as error:
            handle.close()
            if getattr(error, "winerror", None) in {32, 33, 36} or error.errno in {
                11,
                13,
            }:
                raise InstanceAlreadyRunning() from None
            raise RuntimeError("application instance guard unavailable") from None
        self._file = handle

    def release(self) -> None:
        handle = self._file
        if handle is None:
            return
        self._file = None
        try:
            _unlock(handle)
        finally:
            handle.close()


def _open_regular_lock(path: Path, allowed_root: Path) -> BinaryIO:
    """Open/create without trusting a pre-open path check, then verify identity."""

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOINHERIT"):
        flags |= os.O_NOINHERIT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        _validate_data_local_path(allowed_root, path)
        resolved_root = Path(os.path.realpath(allowed_root))
        resolved_path = Path(os.path.realpath(path))
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            raise ValueError(_INVALID_DATABASE) from None
        metadata = path.lstat()
        if _is_link_or_reparse(path) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("unsafe local lock location")
        if not os.path.samestat(os.fstat(descriptor), metadata):
            raise ValueError("unsafe local lock location")
        return os.fdopen(descriptor, "r+b", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


if os.name == "nt":
    import msvcrt

    def _lock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
