"""Read-only server-rendered dashboard routes."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hmac
import io
import ipaddress
import os
from pathlib import Path
import re
import secrets
import stat
from threading import Lock
import unicodedata
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit
import warnings
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased
from starlette.datastructures import UploadFile
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException, MultiPartParser

from ticket_reviewer.data.repositories import (
    EventRepository,
    ObservationRepository,
    OpportunityRepository,
    OutcomeRepository,
    SettingRepository,
    ALLOWED_SETTING_FIELDS,
)
from ticket_reviewer.data.schema import (
    ConnectorRunRow,
    EventRow,
    ManualReviewRow,
    ObservationRow,
    OpportunityRow,
    OutcomeRow,
    SourceEventRow,
)
from ticket_reviewer.domain.enums import (
    Confidence,
    ObservationKind,
    OpportunityStatus,
    Source,
    Team,
)
from ticket_reviewer.domain.matching import event_match_score, normalize_label
from ticket_reviewer.domain.models import ExternalEvent, SourceObservation
from ticket_reviewer.domain.scoring import estimate_opportunity
from ticket_reviewer.services.ocr import (
    ManualReviewDraft,
    normalize_ocr_text,
    parse_listing_text,
)

from .viewmodels import (
    EventEstimate,
    OpportunityCard,
    clean_text,
    local_time,
    money_text,
    observation_kind_text,
    safe_error,
    freshness_text,
    sanitize_public_url,
    seating_key,
    source_label,
    utc_iso,
)


router = APIRouter()
_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_ROOT / "templates"))
_MAX_OPPORTUNITIES = 200
_MAX_EVENT_OBSERVATIONS = 5000
_MAX_EVENT_ESTIMATES = 500
_MAX_SOURCE_EVENTS = 100
_FILTER_NAMES = frozenset(
    {"team", "event_id", "source", "confidence", "min_profit", "max_cost", "status"}
)
_MONEY = re.compile(r"-?(?:0|[1-9]\d{0,8})(?:\.\d{1,2})?")
_POSITIVE_ID = re.compile(r"[1-9]\d{0,8}")
_UPLOAD_LIMIT = 10 * 1024 * 1024
_UPLOAD_CHUNK = 64 * 1024
_MAX_IMAGE_PIXELS = 40_000_000
_MAX_IMAGE_DIMENSION = 12_000
_MAX_METADATA_BYTES = 64_000
_MAX_REFERENCE_URL = 2048
_STAGED_TTL = timedelta(hours=24)
_REVIEW_FILE = re.compile(r"[0-9a-f]{32}\.(?:png|jpg)")
_FORM_MONEY = re.compile(r"(?:0|[1-9]\d{0,9})(?:\.\d{1,2})?")
_SAFE_TEXT = re.compile(r"[^\x00-\x1f\x7f-\x9f\ud800-\udfff]+")
_EVENT_MATCH_THRESHOLD = Decimal("0.85")
_HOME_VENUES = {
    Team.TEXANS: frozenset({"nrg stadium", "reliant stadium"}),
    Team.AGGIES: frozenset({"kyle field"}),
}
_FORM_BODY_LIMIT = 16 * 1024
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_OUTCOME_FORM_FIELDS = frozenset(
    {"status", "actual_acquisition", "actual_proceeds", "actual_fees", "notes"}
)
_SETTINGS_RESULTS = {
    "updated": "Settings updated",
    "dry-run": "Dry run - no push sent",
    "sent": "Test notification sent",
    "missing-topic": "Notification topic is missing",
    "temporarily-unavailable": "Notification service is temporarily unavailable",
    "rejected": "Notification service rejected the test",
}


class _OversizeUpload(MultiPartException):
    pass


class _BoundedMultiPartParser(MultiPartParser):
    """Stop file spooling as soon as one upload crosses the local limit."""

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_file_bytes += end - start
            if self._current_file_bytes > _UPLOAD_LIMIT:
                raise _OversizeUpload("Upload exceeded the local size limit")
        super().on_part_data(data, start, end)


def _bad_filter() -> HTTPException:
    return HTTPException(status_code=400, detail="Invalid dashboard filters")


def _parse_filters(request: Request) -> dict[str, str | int | Decimal]:
    values: dict[str, list[str]] = defaultdict(list)
    for name, value in request.query_params.multi_items():
        if name not in _FILTER_NAMES:
            raise _bad_filter()
        values[name].append(value)
    if any(len(items) != 1 or items[0] == "" for items in values.values()):
        raise _bad_filter()
    parsed: dict[str, str | int | Decimal] = {}
    enum_types = {
        "team": Team,
        "source": Source,
        "confidence": Confidence,
        "status": OpportunityStatus,
    }
    for name, enum_type in enum_types.items():
        if name in values:
            try:
                parsed[name] = enum_type(values[name][0]).value
            except ValueError:
                raise _bad_filter() from None
    if "event_id" in values:
        raw = values["event_id"][0]
        if _POSITIVE_ID.fullmatch(raw) is None:
            raise _bad_filter()
        parsed["event_id"] = int(raw)
    for name in ("min_profit", "max_cost"):
        if name not in values:
            continue
        raw = values[name][0]
        if _MONEY.fullmatch(raw) is None:
            raise _bad_filter()
        value = Decimal(raw)
        if not value.is_finite() or (name == "max_cost" and value < 0):
            raise _bad_filter()
        parsed[name] = value
    return parsed


def _now(request: Request) -> datetime:
    clock = getattr(request.app.state, "clock", None)
    value = clock() if callable(clock) else datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("invalid dashboard clock")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class DashboardContext:
    session: Session
    timezone: str
    freshness_minutes: int
    dry_run: bool
    now: datetime


def dashboard_context(request: Request):
    services = getattr(request.app.state, "services", None)
    session_factory = getattr(services, "session_factory", None)
    if not callable(session_factory):
        raise HTTPException(status_code=503, detail="Dashboard data is unavailable")
    session = session_factory()
    try:
        settings = request.app.state.settings
        effective = SettingRepository(session).effective(settings)
        yield DashboardContext(
            session=session,
            timezone=settings.timezone,
            freshness_minutes=effective.observation_freshness_minutes,
            dry_run=bool(settings.dry_run),
            now=_now(request),
        )
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _manual_error(status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail="Invalid manual review request")


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _fixed_result(request: Request, allowed: dict[str, str]) -> str | None:
    items = request.query_params.multi_items()
    if not items:
        return None
    if len(items) != 1 or items[0][0] != "result" or items[0][1] not in allowed:
        raise _manual_error()
    return allowed[items[0][1]]


def _secret_text(value: object, *, strip: bool = True) -> str:
    getter = getattr(value, "get_secret_value", None)
    if not callable(getter):
        return ""
    secret = getter()
    if not isinstance(secret, str):
        return ""
    return secret.strip() if strip else secret


def _configuration_statuses(settings: object) -> tuple[dict[str, object], ...]:
    stubhub_id = _secret_text(getattr(settings, "stubhub_client_id", None))
    stubhub_secret = _secret_text(getattr(settings, "stubhub_client_secret", None))
    return (
        {
            "label": "Ticketmaster credential",
            "configured": bool(_secret_text(getattr(settings, "ticketmaster_api_key", None))),
        },
        {
            "label": "SeatGeek credential",
            "configured": bool(_secret_text(getattr(settings, "seatgeek_client_id", None))),
        },
        {
            "label": "StubHub credential pair",
            "configured": bool(stubhub_id and stubhub_secret),
        },
        {
            "label": "Notification topic",
            "configured": bool(_secret_text(getattr(settings, "ntfy_topic", None), strip=False)),
        },
        {
            "label": "Optional notification access token",
            "configured": bool(_secret_text(getattr(settings, "ntfy_access_token", None), strip=False)),
        },
    )


def _exact_names(
    values: dict[str, list[object]],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
) -> None:
    names = set(values)
    if not required.issubset(names) or not names.issubset(allowed | {"csrf_token"}):
        raise _manual_error()
    if any(len(items) != 1 for items in values.values()):
        raise _manual_error()


def _optional_money(values: dict[str, list[object]], name: str) -> Decimal | None:
    raw = _single_text(values, name, required=False, max_length=32)
    if not raw:
        return None
    if _FORM_MONEY.fullmatch(raw) is None:
        raise _manual_error(422)
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise _manual_error(422) from None
    if not parsed.is_finite():
        raise _manual_error(422)
    return parsed


def _csrf(request: Request, values: dict[str, list[object]]) -> None:
    supplied = values.get("csrf_token", [])
    expected = getattr(request.app.state, "manual_csrf_token", "")
    if (
        len(supplied) != 1
        or not isinstance(supplied[0], str)
        or len(supplied[0]) > 128
        or not supplied[0].isascii()
        or not isinstance(expected, str)
        or not expected.isascii()
        or not hmac.compare_digest(supplied[0], expected)
    ):
        raise _manual_error()


async def _form_values(request: Request, *, multipart: bool) -> dict[str, list[object]]:
    try:
        if multipart:
            form = await _BoundedMultiPartParser(
                request.headers,
                request.stream(),
                max_files=1,
                max_fields=32,
                max_part_size=4096,
            ).parse()
        else:
            return await _urlencoded_form_values(request)
    except _OversizeUpload:
        raise _manual_error(413) from None
    except HTTPException:
        raise
    except Exception:
        raise _manual_error() from None
    values: dict[str, list[object]] = defaultdict(list)
    try:
        for name, value in form.multi_items():
            if not isinstance(name, str) or len(name) > 80:
                raise _manual_error()
            values[name].append(value)
    except Exception:
        await _close_uploads(value for _name, value in form.multi_items())
        raise
    return values


async def _urlencoded_form_values(request: Request) -> dict[str, list[object]]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise _manual_error()
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _FORM_BODY_LIMIT:
                raise _manual_error(413)
        except ValueError:
            raise _manual_error() from None
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > _FORM_BODY_LIMIT:
            raise _manual_error(413)
    if any(value > 127 for value in raw):
        raise _manual_error()
    encoded = raw.decode("ascii")
    if _BAD_PERCENT_ESCAPE.search(encoded) is not None:
        raise _manual_error()
    try:
        pairs = parse_qsl(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=32,
        )
    except (UnicodeError, ValueError):
        raise _manual_error() from None
    values: dict[str, list[object]] = defaultdict(list)
    for name, value in pairs:
        if not name or len(name) > 80:
            raise _manual_error()
        values[name].append(value)
    return values


async def _close_uploads(items) -> None:
    closed: set[int] = set()
    for item in items:
        identity = id(item)
        if isinstance(item, UploadFile) and identity not in closed:
            closed.add(identity)
            await item.close()


def _single_text(
    values: dict[str, list[object]],
    name: str,
    *,
    required: bool = True,
    max_length: int = 256,
) -> str:
    items = values.get(name, [])
    if len(items) != 1 or not isinstance(items[0], str):
        if not required and not items:
            return ""
        raise _manual_error()
    value = items[0].strip()
    if (
        len(value) > max_length
        or (value and _SAFE_TEXT.fullmatch(value) is None)
        or _contains_control(value)
    ):
        raise _manual_error()
    if required and not value:
        raise _manual_error()
    return value


def _reference_url(value: str) -> str:
    if not value:
        return ""
    if (
        len(value) > _MAX_REFERENCE_URL
        or _SAFE_TEXT.fullmatch(value) is None
        or _contains_control(value)
    ):
        raise _manual_error()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError):
        raise _manual_error() from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.netloc
    ):
        raise _manual_error()
    decoded_components = unquote(unquote(f"{parsed.path}?{parsed.query}"))
    if decoded_components and (
        _SAFE_TEXT.fullmatch(decoded_components) is None
        or _contains_control(decoded_components)
    ):
        raise _manual_error()
    folded_host = host.rstrip(".").casefold()
    if (
        folded_host in {"localhost", "localhost.localdomain"}
        or folded_host.endswith(".local")
        or folded_host.startswith(("0x", "+", "-"))
    ):
        raise _manual_error()
    try:
        address = ipaddress.ip_address(folded_host.strip("[]"))
    except ValueError:
        address = None
    if address is None and re.fullmatch(r"[0-9.]+", folded_host) is not None:
        raise _manual_error()
    if address is not None and not address.is_global:
        raise _manual_error()
    safe_host = f"[{folded_host}]" if ":" in folded_host else folded_host
    netloc = safe_host if port is None else f"{safe_host}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", parsed.query, ""))


def _decode_upload(data: bytes, mime: str) -> tuple[bytes, str]:
    expected = {"image/png": ("PNG", ".png"), "image/jpeg": ("JPEG", ".jpg")}.get(mime)
    if expected is None:
        raise _manual_error(415)
    if mime == "image/png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or not data.endswith(
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        ):
            raise _manual_error()
    elif not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
        raise _manual_error()
    normalized: Image.Image | None = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != expected[0] or getattr(image, "n_frames", 1) != 1:
                    raise _manual_error()
                width, height = image.size
                metadata_size = sum(
                    len(str(key)) + len(str(value)) for key, value in image.info.items()
                )
                if (
                    width <= 0
                    or height <= 0
                    or width > _MAX_IMAGE_DIMENSION
                    or height > _MAX_IMAGE_DIMENSION
                    or width * height > _MAX_IMAGE_PIXELS
                    or metadata_size > _MAX_METADATA_BYTES
                ):
                    raise _manual_error()
                image.load()
                oriented = ImageOps.exif_transpose(image)
                try:
                    normalized = oriented.convert("RGB")
                finally:
                    if oriented is not image:
                        oriented.close()
        with io.BytesIO() as output:
            if expected[0] == "PNG":
                normalized.save(output, format="PNG", optimize=False, compress_level=6)
            else:
                normalized.save(output, format="JPEG", quality=90, optimize=False, progressive=False)
            safe_bytes = output.getvalue()
        if len(safe_bytes) > _UPLOAD_LIMIT:
            raise _manual_error(413)
        return safe_bytes, expected[1]
    except HTTPException:
        raise
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise _manual_error() from None
    except Exception:
        raise _manual_error() from None
    finally:
        if normalized is not None:
            normalized.close()


def _screenshot_root(request: Request) -> Path:
    configured = Path(request.app.state.settings.screenshot_directory)
    if configured.exists() and (configured.is_symlink() or not configured.is_dir()):
        raise _manual_error(500)
    try:
        configured.mkdir(exist_ok=True)
        root = configured.resolve(strict=True)
    except OSError:
        raise _manual_error(500) from None
    if root.is_symlink() or not root.is_dir():
        raise _manual_error(500)
    return root


def _write_screenshot(request: Request, data: bytes, suffix: str) -> tuple[str, Path]:
    root = _screenshot_root(request)
    name = f"{secrets.token_hex(16)}{suffix}"
    path = root / name
    if path.parent != root:
        raise _manual_error(500)
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise OSError
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise _manual_error(500) from None
    return name, path


def _draft_payload(draft: ManualReviewDraft, reference_url: str, staged_at: datetime) -> dict:
    return {
        "state": "unconfirmed",
        "staged_at": staged_at.isoformat(),
        "reference_url": reference_url,
        "draft": {
            "event": draft.event,
            "team": draft.team,
            "opponent": draft.opponent,
            "marketplace": draft.marketplace,
            "kickoff": draft.kickoff_text,
            "venue": draft.venue,
            "section": draft.section,
            "row": draft.row,
            "quantity": draft.quantity,
            "per_ticket_price": str(draft.per_ticket_price) if draft.per_ticket_price is not None else None,
            "fees": str(draft.fees) if draft.fees is not None else None,
            "tax": str(draft.tax) if draft.tax is not None else None,
            "total": str(draft.total) if draft.total is not None else None,
        },
    }


def _manual_session(request: Request) -> Session:
    services = getattr(request.app.state, "services", None)
    factory = getattr(services, "session_factory", None)
    if not callable(factory):
        raise _manual_error(503)
    return factory()


def _safe_review_path(request: Request, stored: object) -> Path:
    if not isinstance(stored, str) or _REVIEW_FILE.fullmatch(stored) is None:
        raise _manual_error(500)
    root = _screenshot_root(request)
    path = root / stored
    if path.parent != root or path.is_symlink():
        raise _manual_error(500)
    return path


@dataclass(frozen=True, slots=True)
class _ReviewSnapshot:
    id: int
    screenshot_path: str
    ocr_text: str | None
    corrected_payload: dict | None
    confirmed_at: datetime | None


def _review_snapshot(row: ManualReviewRow) -> _ReviewSnapshot:
    return _ReviewSnapshot(
        id=row.id,
        screenshot_path=row.screenshot_path,
        ocr_text=row.ocr_text,
        corrected_payload=deepcopy(row.corrected_payload),
        confirmed_at=row.confirmed_at,
    )


def _restore_review(session: Session, snapshot: _ReviewSnapshot) -> None:
    session.add(
        ManualReviewRow(
            id=snapshot.id,
            screenshot_path=snapshot.screenshot_path,
            ocr_text=snapshot.ocr_text,
            corrected_payload=deepcopy(snapshot.corrected_payload),
            confirmed_at=snapshot.confirmed_at,
        )
    )
    session.commit()


def _delete_private_review(request: Request, session: Session, row: ManualReviewRow) -> None:
    """Delete DB state first, restoring it if the still-private file cannot be unlinked."""
    snapshot = _review_snapshot(row)
    original = _safe_review_path(request, row.screenshot_path)
    if original.exists():
        details = original.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or original.is_symlink()
        ):
            raise _manual_error(500)
    session.delete(row)
    session.commit()
    if not original.exists():
        return
    try:
        original.unlink()
    except OSError:
        try:
            _restore_review(session, snapshot)
        except Exception as restore_error:
            session.rollback()
            raise RuntimeError("manual review compensation failed") from restore_error
        raise _manual_error(500) from None


def _purge_stale_reviews(request: Request, now: datetime) -> None:
    session: Session | None = None
    try:
        session = _manual_session(request)
        rows = list(
            session.scalars(
                select(ManualReviewRow)
                .where(ManualReviewRow.confirmed_at.is_(None))
                .order_by(ManualReviewRow.id)
                .limit(50)
            )
        )
        for row in rows:
            payload = row.corrected_payload
            if not isinstance(payload, dict) or payload.get("state") != "unconfirmed":
                continue
            staged_raw = payload.get("staged_at")
            if not isinstance(staged_raw, str) or len(staged_raw) > 64:
                continue
            try:
                staged_at = datetime.fromisoformat(staged_raw)
                if staged_at.tzinfo is None or staged_at.utcoffset() is None:
                    continue
                expired = now - staged_at.astimezone(timezone.utc) > _STAGED_TTL
            except (ValueError, OverflowError):
                continue
            if not expired:
                continue
            try:
                lock = _confirmation_lock(request, row.id)
                with lock:
                    session.refresh(row)
                    if row.confirmed_at is None:
                        _delete_private_review(request, session, row)
            except Exception:
                session.rollback()
    except Exception:
        if session is not None:
            session.rollback()
    finally:
        if session is not None:
            session.close()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, context: DashboardContext = Depends(dashboard_context)
):
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            request,
            context.session,
            result=_fixed_result(request, _SETTINGS_RESULTS),
        ),
    )


def _settings_context(
    request: Request,
    session: Session,
    *,
    result: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    effective = SettingRepository(session).effective(request.app.state.settings)
    values = {
        key: str(getattr(effective, key)) for key in sorted(ALLOWED_SETTING_FIELDS)
    }
    return {
        "csrf_token": request.app.state.manual_csrf_token,
        "values": values,
        "configuration_statuses": _configuration_statuses(request.app.state.settings),
        "result": result,
        "error": error,
    }


def _settings_error_response(
    request: Request, session: Session, status_code: int
):
    error = {
        400: "Settings were not saved. Refresh the page and try again.",
        413: "Settings were not saved because the request was too large.",
        422: "Settings were not saved. Check each nonsecret value.",
        503: "Settings could not be saved. Try again later.",
    }.get(status_code, "Settings could not be saved. Try again later.")
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(request, session, error=error),
        status_code=status_code,
    )


@router.post("/settings")
async def update_settings(
    request: Request, context: DashboardContext = Depends(dashboard_context)
):
    try:
        values = await _form_values(request, multipart=False)
        _csrf(request, values)
        _exact_names(
            values,
            allowed=ALLOWED_SETTING_FIELDS,
            required=ALLOWED_SETTING_FIELDS,
        )
        submitted = {
            key: _single_text(values, key, max_length=32)
            for key in ALLOWED_SETTING_FIELDS
        }
        SettingRepository(context.session).set_all(submitted)
        context.session.commit()
    except HTTPException as error:
        context.session.rollback()
        return _settings_error_response(request, context.session, error.status_code)
    except ValueError:
        context.session.rollback()
        return _settings_error_response(request, context.session, 422)
    except Exception:
        context.session.rollback()
        return _settings_error_response(request, context.session, 503)
    return RedirectResponse("/settings?result=updated", status_code=303)


@router.post("/notifications/test")
async def test_notification(request: Request):
    try:
        values = await _form_values(request, multipart=False)
        _csrf(request, values)
        _exact_names(values, allowed=frozenset(), required=frozenset())
    except HTTPException as error:
        message = (
            "Notification test was not sent because the request was too large."
            if error.status_code == 413
            else "Notification test was not sent. Refresh the page and try again."
        )
        services = getattr(request.app.state, "services", None)
        session_factory = getattr(services, "session_factory", None)
        if not callable(session_factory):
            raise
        with session_factory() as session:
            session.rollback()
            return templates.TemplateResponse(
                request,
                "settings.html",
                _settings_context(request, session, error=message),
                status_code=error.status_code,
            )
    settings = request.app.state.settings
    if settings.dry_run:
        code = "dry-run"
    else:
        services = getattr(request.app.state, "services", None)
        service = getattr(services, "alert_service", None)
        method = getattr(service, "test_notification", None)
        if callable(method):
            try:
                result = await run_in_threadpool(method)
                code = result.code
            except Exception:
                code = "temporarily-unavailable"
        elif _secret_text(settings.ntfy_topic, strip=False):
            code = "temporarily-unavailable"
        else:
            code = "missing-topic"
    if code not in _SETTINGS_RESULTS:
        code = "temporarily-unavailable"
    return RedirectResponse(f"/settings?result={code}", status_code=303)


def _outcome_lock(request: Request, opportunity_id: int) -> Lock:
    with request.app.state.outcome_guard:
        locks = request.app.state.outcome_locks
        lock = locks.get(opportunity_id)
        if lock is None:
            lock = Lock()
            locks[opportunity_id] = lock
        return lock


def _outcome_form_values(row: OutcomeRow | None) -> dict[str, str]:
    if row is None:
        return {}
    return {
        "status": row.status if row.status in {item.value for item in OpportunityStatus} else "",
        "actual_acquisition": str(row.actual_acquisition)
        if row.actual_acquisition is not None
        else "",
        "actual_proceeds": str(row.actual_proceeds)
        if row.actual_proceeds is not None
        else "",
        "actual_fees": str(row.actual_fees) if row.actual_fees is not None else "",
        "notes": clean_text(row.notes, maximum=2000, fallback=""),
    }


def _outcome_error_response(
    request: Request,
    context: DashboardContext,
    opportunity: OpportunityRow,
    status_code: int,
):
    error = {
        400: "Outcome was not saved. Refresh the page and try again.",
        413: "Outcome was not saved because the request was too large.",
        422: "Outcome was not saved. Check the fields required for that state.",
        503: "Outcome could not be saved. Try again later.",
    }.get(status_code, "Outcome could not be saved. Try again later.")
    existing = OutcomeRepository(context.session).get(opportunity.id)
    return templates.TemplateResponse(
        request,
        "outcome_error.html",
        {
            "estimate": {"id": opportunity.id},
            "event_id": opportunity.event_id,
            "outcome": _outcome_form_values(existing),
            "csrf_token": request.app.state.manual_csrf_token,
            "error": error,
        },
        status_code=status_code,
    )


@router.post("/opportunities/{opportunity_id}/status")
async def update_outcome(
    request: Request,
    opportunity_id: str,
    context: DashboardContext = Depends(dashboard_context),
):
    if _POSITIVE_ID.fullmatch(opportunity_id) is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    numeric_id = int(opportunity_id)
    opportunity = context.session.get(OpportunityRow, numeric_id)
    if opportunity is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    try:
        values = await _form_values(request, multipart=False)
        _csrf(request, values)
        _exact_names(
            values,
            allowed=_OUTCOME_FORM_FIELDS,
            required=frozenset({"status"}),
        )
        status = _single_text(values, "status", max_length=16)
        actual_acquisition = _optional_money(values, "actual_acquisition")
        actual_proceeds = _optional_money(values, "actual_proceeds")
        actual_fees = _optional_money(values, "actual_fees")
        notes = _single_text(values, "notes", required=False, max_length=2000) or None
    except HTTPException as error:
        context.session.rollback()
        return _outcome_error_response(
            request, context, opportunity, error.status_code
        )
    with _outcome_lock(request, numeric_id):
        event_id = opportunity.event_id
        try:
            OutcomeRepository(context.session).save(
                numeric_id,
                status,
                actual_acquisition=actual_acquisition,
                actual_proceeds=actual_proceeds,
                actual_fees=actual_fees,
                notes=notes,
            )
            context.session.commit()
        except ValueError:
            context.session.rollback()
            return _outcome_error_response(request, context, opportunity, 422)
        except Exception:
            context.session.rollback()
            return _outcome_error_response(request, context, opportunity, 503)
    return RedirectResponse(
        f"/events/{event_id}?result=outcome-updated", status_code=303
    )


@router.get("/manual", response_class=HTMLResponse)
def manual_review(request: Request):
    return templates.TemplateResponse(
        request,
        "manual_review.html",
        {"csrf_token": request.app.state.manual_csrf_token},
    )


@router.post("/manual/extract", response_class=HTMLResponse)
async def manual_extract(request: Request):
    values = await _form_values(request, multipart=True)
    try:
        _csrf(request, values)
        uploads = values.get("screenshot", [])
        if len(uploads) != 1 or not isinstance(uploads[0], UploadFile):
            raise _manual_error()
        upload = uploads[0]
        if upload.content_type not in {"image/png", "image/jpeg"}:
            raise _manual_error(415)
        reference = _reference_url(
            _single_text(values, "reference_url", required=False, max_length=_MAX_REFERENCE_URL)
        )
        _purge_stale_reviews(request, _now(request))
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await upload.read(min(_UPLOAD_CHUNK, _UPLOAD_LIMIT + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _UPLOAD_LIMIT:
                raise _manual_error(413)
        safe_bytes, suffix = _decode_upload(b"".join(chunks), upload.content_type)
    finally:
        await _close_uploads(
            item for items in values.values() for item in items
        )
    stored_name = ""
    stored_path: Path | None = None
    session: Session | None = None
    try:
        stored_name, stored_path = _write_screenshot(request, safe_bytes, suffix)
        engine = getattr(request.app.state, "ocr_engine", None)
        if engine is None or not callable(getattr(engine, "extract_text", None)):
            raise _manual_error(503)
        ocr_text = normalize_ocr_text(engine.extract_text(stored_path))
        draft = parse_listing_text(ocr_text)
        staged_at = _now(request)
        session = _manual_session(request)
        review = ManualReviewRow(
            screenshot_path=stored_name,
            ocr_text=ocr_text,
            corrected_payload=_draft_payload(draft, reference, staged_at),
            confirmed_at=None,
        )
        session.add(review)
        session.flush()
        review_id = review.id
        session.commit()
    except HTTPException:
        if session is not None:
            session.rollback()
        if stored_path is not None:
            try:
                stored_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    except Exception:
        if session is not None:
            session.rollback()
        if stored_path is not None:
            try:
                stored_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise _manual_error(422) from None
    finally:
        if session is not None:
            session.close()
    return templates.TemplateResponse(
        request,
        "manual_confirm.html",
        {
            "csrf_token": request.app.state.manual_csrf_token,
            "review_id": review_id,
            "draft": draft,
            "reference_url": reference,
            "result": None,
        },
    )


def _money_form(value: str, *, required: bool) -> Decimal | None:
    if not value:
        if required:
            raise _manual_error()
        return None
    if _FORM_MONEY.fullmatch(value) is None:
        raise _manual_error()
    try:
        parsed = Decimal(value).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise _manual_error() from None
    if not parsed.is_finite() or parsed <= 0:
        raise _manual_error()
    return parsed


def _kickoff(value: str, now: datetime, timezone_name: str) -> datetime:
    if re.search(r"[+-]\d{2}:\d{2}$", value) is None:
        raise _manual_error()
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, OverflowError):
        raise _manual_error() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _manual_error()
    try:
        wall = parsed.replace(tzinfo=None)
        local_zone = ZoneInfo(timezone_name)
        possible_offsets = {
            wall.replace(tzinfo=local_zone, fold=0).utcoffset(),
            wall.replace(tzinfo=local_zone, fold=1).utcoffset(),
        }
        if len(possible_offsets) != 1 or parsed.utcoffset() not in possible_offsets:
            raise _manual_error()
        utc_value = parsed.astimezone(timezone.utc)
        if utc_value <= now or utc_value > now + timedelta(days=366):
            raise _manual_error()
    except HTTPException:
        raise
    except (OverflowError, ValueError):
        raise _manual_error() from None
    return utc_value


@dataclass(frozen=True, slots=True)
class _ConfirmedInput:
    event: str
    team: Team
    opponent: str
    marketplace: str
    reference_url: str
    kickoff: datetime
    venue: str
    section: str | None
    row: str | None
    per_ticket_price: Decimal | None
    fees: Decimal | None
    tax: Decimal | None
    total: Decimal


def _confirmed_input(
    request: Request,
    values: dict[str, list[object]],
    effective_budget: Decimal,
    now: datetime,
) -> _ConfirmedInput:
    event = _single_text(values, "event", max_length=160)
    team_raw = _single_text(values, "team", max_length=16)
    try:
        team = Team(team_raw)
    except ValueError:
        raise _manual_error() from None
    opponent = _single_text(values, "opponent", max_length=80)
    venue = _single_text(values, "venue", max_length=128)
    normalized_event = normalize_label(event)
    normalized_opponent = normalize_label(opponent)
    event_pattern = (
        r"(?:houston )?texans vs (.+)"
        if team is Team.TEXANS
        else r"(?:texas a m(?: aggies)?|aggies) vs (.+)"
    )
    event_match = re.fullmatch(event_pattern, normalized_event)
    exact_opponent = event_match.group(1) if event_match is not None else None
    opponent_is_supported = bool(
        re.search(r"\b(?:houston )?texans\b|\b(?:texas a m(?: aggies)?|aggies)\b", normalized_opponent)
    )
    if (
        exact_opponent != normalized_opponent
        or opponent_is_supported
        or re.search(r"\b(?:parking|pass|away|neutral| at )\b", event, re.IGNORECASE)
        or normalize_label(venue) not in _HOME_VENUES[team]
    ):
        raise _manual_error()
    quantity = _single_text(values, "quantity", max_length=8)
    if quantity != "2":
        raise _manual_error()
    total = _money_form(_single_text(values, "total", max_length=32), required=True)
    assert total is not None
    cap = min(Decimal("400.00"), effective_budget)
    if total > cap:
        raise _manual_error()
    marketplace = _single_text(values, "marketplace", required=False, max_length=80)
    reference = _reference_url(
        _single_text(values, "reference_url", required=False, max_length=_MAX_REFERENCE_URL)
    )
    section = _single_text(values, "section", required=False, max_length=64) or None
    row = _single_text(values, "row", required=False, max_length=64) or None
    kickoff = _kickoff(
        _single_text(values, "kickoff", max_length=64),
        now,
        request.app.state.settings.timezone,
    )
    return _ConfirmedInput(
        event=event,
        team=team,
        opponent=opponent,
        marketplace=marketplace,
        reference_url=reference,
        kickoff=kickoff,
        venue=venue,
        section=section,
        row=row,
        per_ticket_price=_money_form(
            _single_text(values, "per_ticket_price", required=False, max_length=32),
            required=False,
        ),
        fees=_money_form(
            _single_text(values, "fees", required=False, max_length=32),
            required=False,
        ),
        tax=_money_form(
            _single_text(values, "tax", required=False, max_length=32),
            required=False,
        ),
        total=total,
    )


def _event_from_row(row: EventRow) -> ExternalEvent:
    return ExternalEvent(
        source=Source.MANUAL,
        external_id=f"canonical:{row.id}",
        team=Team(row.team),
        opponent=row.opponent,
        venue=row.venue,
        starts_at=row.starts_at,
        is_home=row.is_home,
        is_parking=False,
        url=None,
    )


def _manual_event(
    repositories: EventRepository,
    review_id: int,
    corrected: _ConfirmedInput,
) -> tuple[int, str]:
    external_id = f"manual-review:{review_id}"
    incoming = ExternalEvent(
        source=Source.MANUAL,
        external_id=external_id,
        team=corrected.team,
        opponent=corrected.opponent,
        venue=corrected.venue,
        starts_at=corrected.kickoff,
        is_home=True,
        is_parking=False,
        url=None,
    )
    existing = repositories.find_by_source(Source.MANUAL, external_id)
    if existing is not None:
        return repositories.upsert(incoming), external_id
    matches = [
        row
        for row in repositories.list_for_team(corrected.team)
        if event_match_score(incoming, _event_from_row(row)) >= _EVENT_MATCH_THRESHOLD
    ]
    if len(matches) > 1:
        raise _manual_error()
    event_id = repositories.upsert(
        incoming,
        canonical_event_id=matches[0].id if matches else None,
    )
    return event_id, external_id


def _observation(row: ObservationRow, event_external_id: str) -> SourceObservation:
    return SourceObservation(
        source=Source(row.source),
        event_external_id=event_external_id,
        observed_at=row.observed_at,
        kind=ObservationKind(row.kind),
        currency=row.currency,
        pair_price=row.pair_price,
        buyer_fees=row.buyer_fees,
        estimated_tax=row.estimated_tax,
        section=row.section,
        row=row.row,
        quantity_available=row.quantity_available,
        can_buy_pair=row.can_buy_pair,
        listing_id=row.listing_id,
        listing_url=row.listing_url,
        listing_count=row.listing_count,
        popularity=row.popularity,
        observation_id=row.id,
    )


def _confirmation_lock(request: Request, review_id: int) -> Lock:
    guard = request.app.state.manual_confirmation_guard
    with guard:
        locks = request.app.state.manual_confirmation_locks
        lock = locks.get(review_id)
        if lock is None:
            lock = Lock()
            locks[review_id] = lock
        return lock


def _result_context(request: Request, review_id: int, payload: dict, *, repeated: bool):
    return templates.TemplateResponse(
        request,
        "manual_confirm.html",
        {
            "csrf_token": request.app.state.manual_csrf_token,
            "review_id": review_id,
            "result": {
                "message": (
                    "This review was already confirmed; no duplicate estimate was created."
                    if repeated
                    else "The corrected pair was scored and saved."
                ),
                "event_id": payload.get("event_id"),
                "opportunity_id": payload.get("opportunity_id"),
            },
        },
    )


@router.post("/manual/confirm", response_class=HTMLResponse)
async def manual_confirm(request: Request):
    values = await _form_values(request, multipart=False)
    _csrf(request, values)
    review_raw = _single_text(values, "review_id", max_length=9)
    if _POSITIVE_ID.fullmatch(review_raw) is None:
        raise _manual_error()
    review_id = int(review_raw)
    lock = _confirmation_lock(request, review_id)
    with lock:
        session = _manual_session(request)
        opportunity_id: int | None = None
        try:
            review = session.get(ManualReviewRow, review_id)
            if review is None:
                raise _manual_error(404)
            if review.confirmed_at is not None:
                payload = review.corrected_payload if isinstance(review.corrected_payload, dict) else {}
                return _result_context(request, review_id, payload, repeated=True)
            now = _now(request)
            effective = SettingRepository(session).effective(request.app.state.settings)
            corrected = _confirmed_input(request, values, effective.budget_cap, now)
            events = EventRepository(session)
            observations = ObservationRepository(session)
            opportunities_repo = OpportunityRepository(session)
            event_id, external_id = _manual_event(events, review_id, corrected)
            candidate = SourceObservation(
                source=Source.MANUAL,
                event_external_id=external_id,
                observed_at=now,
                kind=ObservationKind.LISTING,
                currency="USD",
                pair_price=corrected.total,
                buyer_fees=Decimal("0.00"),
                estimated_tax=Decimal("0.00"),
                section=corrected.section,
                row=corrected.row,
                quantity_available=2,
                can_buy_pair=True,
                listing_id=f"manual-review:{review_id}",
                listing_url=None,
            )
            saved = observations.add_with_status(event_id, candidate)
            candidate_row = observations.get(saved.observation_id)
            if candidate_row is None:
                raise RuntimeError
            persisted_candidate = _observation(candidate_row, external_id)
            freshness = timedelta(minutes=effective.observation_freshness_minutes)
            comparable_rows = list(
                session.scalars(
                    select(ObservationRow)
                    .where(
                        ObservationRow.event_id == event_id,
                        ObservationRow.observed_at >= now - freshness,
                        ObservationRow.observed_at <= now + timedelta(minutes=5),
                    )
                    .order_by(ObservationRow.observed_at, ObservationRow.id)
                )
            )
            comparables = tuple(
                _observation(row, external_id)
                for row in comparable_rows
            )
            estimate = estimate_opportunity(
                persisted_candidate,
                comparables,
                {
                    Source.STUBHUB: effective.stubhub_seller_fee_rate,
                    Source.TICKETMASTER: effective.ticketmaster_seller_fee_rate,
                    Source.SEATGEEK: effective.seatgeek_seller_fee_rate,
                },
                now,
                effective.budget_cap,
                kickoff_at=corrected.kickoff,
            )
            opportunity_id = opportunities_repo.save_estimate(
                event_id, saved.observation_id, estimate
            )
            payload = {
                "state": "confirmed",
                "event": corrected.event,
                "team": corrected.team.value,
                "opponent": corrected.opponent,
                "marketplace": corrected.marketplace,
                "reference_url": corrected.reference_url,
                "kickoff": corrected.kickoff.isoformat(),
                "venue": corrected.venue,
                "section": corrected.section,
                "row": corrected.row,
                "quantity": 2,
                "per_ticket_price": str(corrected.per_ticket_price) if corrected.per_ticket_price is not None else None,
                "fees": str(corrected.fees) if corrected.fees is not None else None,
                "tax": str(corrected.tax) if corrected.tax is not None else None,
                "total": str(corrected.total),
                "event_id": event_id,
                "opportunity_id": opportunity_id,
            }
            review.corrected_payload = payload
            review.confirmed_at = now
            session.commit()
        except HTTPException:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise _manual_error(500) from None
        finally:
            session.close()
    services = getattr(request.app.state, "services", None)
    alert_service = getattr(services, "alert_service", None)
    if estimate.actionable and alert_service is not None:
        try:
            alert_service.evaluate_and_send(opportunity_id, now)
        except Exception:
            pass
    return _result_context(request, review_id, payload, repeated=False)


@router.post("/manual/{review_id}/delete", response_class=HTMLResponse)
async def manual_delete(request: Request, review_id: str):
    if _POSITIVE_ID.fullmatch(review_id) is None:
        raise _manual_error(404)
    values = await _form_values(request, multipart=False)
    _csrf(request, values)
    numeric_id = int(review_id)
    lock = _confirmation_lock(request, numeric_id)
    with lock:
        session = _manual_session(request)
        try:
            review = session.get(ManualReviewRow, numeric_id)
            if review is None:
                raise _manual_error(404)
            _delete_private_review(request, session, review)
        except HTTPException:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise _manual_error(500) from None
        finally:
            session.close()
    return templates.TemplateResponse(
        request,
        "manual_confirm.html",
        {
            "csrf_token": request.app.state.manual_csrf_token,
            "review_id": numeric_id,
            "result": {
                "message": "Private screenshot and corrected review deleted; scoring and opportunity history remains.",
                "event_id": None,
                "opportunity_id": None,
            },
        },
    )


@router.get("/", response_class=HTMLResponse)
def opportunities(
    request: Request, context: DashboardContext = Depends(dashboard_context)
):
    session = context.session
    filters = _parse_filters(request)
    lineage = aliased(ObservationRow)
    first_seen = (
        select(func.min(lineage.observed_at))
        .where(
            lineage.source == ObservationRow.source,
            lineage.event_external_id == ObservationRow.event_external_id,
            lineage.listing_identity == ObservationRow.listing_identity,
        )
        .correlate(ObservationRow)
        .scalar_subquery()
    )
    last_seen = (
        select(func.max(lineage.freshness_at))
        .where(
            lineage.source == ObservationRow.source,
            lineage.event_external_id == ObservationRow.event_external_id,
            lineage.listing_identity == ObservationRow.listing_identity,
        )
        .correlate(ObservationRow)
        .scalar_subquery()
    )
    statement = (
        select(OpportunityRow, EventRow, ObservationRow, first_seen, last_seen)
        .join(EventRow, OpportunityRow.event_id == EventRow.id)
        .join(ObservationRow, OpportunityRow.observation_id == ObservationRow.id)
        .where(
            ObservationRow.kind == "listing",
            ObservationRow.can_buy_pair.is_(True),
            ObservationRow.quantity_available >= 2,
        )
    )
    if "team" in filters:
        statement = statement.where(EventRow.team == filters["team"])
    if "event_id" in filters:
        statement = statement.where(EventRow.id == filters["event_id"])
    if "source" in filters:
        statement = statement.where(ObservationRow.source == filters["source"])
    if "confidence" in filters:
        statement = statement.where(OpportunityRow.confidence == filters["confidence"])
    if "status" in filters:
        statement = statement.where(OpportunityRow.status == filters["status"])
    if "min_profit" in filters:
        statement = statement.where(
            OpportunityRow.estimated_net_profit >= filters["min_profit"]
        )
    if "max_cost" in filters:
        statement = statement.where(
            OpportunityRow.acquisition_total <= filters["max_cost"]
        )
    finite_profit = case(
        (
            OpportunityRow.estimated_net_profit.between(
                Decimal("-9999999999.99"), Decimal("9999999999.99")
            ),
            OpportunityRow.estimated_net_profit,
        ),
        else_=None,
    )
    rows = session.execute(
        statement.order_by(
            finite_profit.desc().nullslast(),
            OpportunityRow.updated_at.desc(),
            OpportunityRow.id.desc(),
        ).limit(_MAX_OPPORTUNITIES)
    ).all()
    cards: list[OpportunityCard] = []
    for row in rows:
        try:
            cards.append(
                OpportunityCard.from_row(
                    tuple(row[:3]),
                    context.timezone,
                    now=context.now,
                    freshness_minutes=context.freshness_minutes,
                    first_seen_at=row[3],
                    last_seen_at=row[4],
                )
            )
        except (TypeError, ValueError):
            continue
    card_ids = [card.id for card in cards]
    outcome_rows = (
        list(
            session.scalars(
                select(OutcomeRow).where(OutcomeRow.opportunity_id.in_(card_ids))
            )
        )
        if card_ids
        else []
    )
    outcomes = {
        row.opportunity_id: {
            "status": row.status,
            "actual_acquisition": str(row.actual_acquisition)
            if row.actual_acquisition is not None
            else "",
            "actual_proceeds": str(row.actual_proceeds)
            if row.actual_proceeds is not None
            else "",
            "actual_fees": str(row.actual_fees)
            if row.actual_fees is not None
            else "",
            "notes": clean_text(row.notes, maximum=2000, fallback=""),
        }
        for row in outcome_rows
    }
    return templates.TemplateResponse(
        request,
        "opportunities.html",
        {
            "cards": cards,
            "filters": {key: str(value) for key, value in filters.items()},
            "outcomes": outcomes,
            "csrf_token": request.app.state.manual_csrf_token,
        },
    )


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(
    request: Request,
    event_id: int,
    context: DashboardContext = Depends(dashboard_context),
):
    session = context.session
    result = _fixed_result(request, {"outcome-updated": "Outcome updated"})
    if type(event_id) is not int or event_id <= 0:
        raise HTTPException(status_code=404, detail="Event not found")
    event = session.get(EventRow, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    observation_total = session.scalar(
        select(func.count(ObservationRow.id)).where(ObservationRow.event_id == event_id)
    ) or 0
    observations = list(
        session.scalars(
            select(ObservationRow)
            .where(ObservationRow.event_id == event_id)
            .order_by(ObservationRow.observed_at.desc(), ObservationRow.id.desc())
            .limit(_MAX_EVENT_OBSERVATIONS)
        )
    )
    observations.sort(key=lambda row: (row.observed_at, row.id))
    series: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    history: list[dict[str, str]] = []
    for observation in observations:
        timestamp = utc_iso(observation.observed_at)
        price = observation.pair_price
        finite_price = (
            price if isinstance(price, Decimal) and price.is_finite() and price >= 0 else None
        )
        kind_text = observation_kind_text(observation.kind)
        seat = seating_key(observation)
        history.append(
            {
                "id": str(observation.id),
                "source": source_label(observation.source),
                "kind": kind_text,
                "seat": seat,
                "observed": local_time(observation.observed_at, context.timezone),
                "price": money_text(finite_price),
            }
        )
        if timestamp is not None and finite_price is not None:
            key = (source_label(observation.source), seat, observation.kind)
            series[key].append({"x": timestamp, "y": str(finite_price)})
    datasets = []
    for (source, seat, kind), points in sorted(series.items()):
        wording = observation_kind_text(kind)
        datasets.append(
            {
                "label": f"{source} — {seat} — {wording}",
                "data": sorted(points, key=lambda point: point["x"]),
                "borderDash": [7, 5] if kind == "event_floor" else [],
            }
        )
    source_event_total = session.scalar(
        select(func.count(SourceEventRow.id)).where(SourceEventRow.event_id == event_id)
    ) or 0
    source_events = list(
        session.scalars(
            select(SourceEventRow)
            .where(SourceEventRow.event_id == event_id)
            .order_by(SourceEventRow.last_seen.desc(), SourceEventRow.id.desc())
            .limit(_MAX_SOURCE_EVENTS)
        )
    )
    source_events.sort(key=lambda row: (row.source, row.id))
    estimate_total = session.scalar(
        select(func.count(OpportunityRow.id)).where(OpportunityRow.event_id == event_id)
    ) or 0
    estimate_rows = list(
        session.scalars(
            select(OpportunityRow)
            .where(OpportunityRow.event_id == event_id)
            .order_by(OpportunityRow.created_at.desc(), OpportunityRow.id.desc())
            .limit(_MAX_EVENT_ESTIMATES)
        )
    )
    estimates = []
    for item in estimate_rows:
        try:
            estimates.append(EventEstimate.from_row(item))
        except (TypeError, ValueError):
            continue
    outcome_rows = list(
        session.scalars(
            select(OutcomeRow)
            .join(OpportunityRow, OutcomeRow.opportunity_id == OpportunityRow.id)
            .where(OpportunityRow.event_id == event_id)
        )
    )
    outcomes = {}
    for row in outcome_rows:
        outcomes[row.opportunity_id] = {
            "status": row.status
            if row.status in {item.value for item in OpportunityStatus}
            else "",
            "actual_acquisition": str(row.actual_acquisition)
            if isinstance(row.actual_acquisition, Decimal)
            and row.actual_acquisition.is_finite()
            else "",
            "actual_proceeds": str(row.actual_proceeds)
            if isinstance(row.actual_proceeds, Decimal) and row.actual_proceeds.is_finite()
            else "",
            "actual_fees": str(row.actual_fees)
            if isinstance(row.actual_fees, Decimal) and row.actual_fees.is_finite()
            else "",
            "notes": clean_text(row.notes, maximum=2000, fallback=""),
        }
    history_notices = []
    if observation_total > _MAX_EVENT_OBSERVATIONS:
        history_notices.append(
            f"Showing newest {_MAX_EVENT_OBSERVATIONS} of {observation_total} observations."
        )
    if estimate_total > _MAX_EVENT_ESTIMATES:
        history_notices.append(
            f"Showing newest {_MAX_EVENT_ESTIMATES} of {estimate_total} estimates."
        )
    if source_event_total > _MAX_SOURCE_EVENTS:
        history_notices.append(
            f"Showing newest {_MAX_SOURCE_EVENTS} of {source_event_total} source references."
        )
    return templates.TemplateResponse(
        request,
        "event_detail.html",
        {
            "event": {
                "team": clean_text(event.team).title(),
                "opponent": clean_text(event.opponent),
                "venue": clean_text(event.venue),
                "kickoff": local_time(event.starts_at, context.timezone),
            },
            "source_events": [
                {
                    "source": source_label(item.source),
                    "external_id": clean_text(item.external_id),
                    "url": sanitize_public_url(item.url, item.source),
                }
                for item in source_events
            ],
            "history": history,
            "history_notices": history_notices,
            "chart": {"datasets": datasets},
            "estimates": estimates,
            "outcomes": outcomes,
            "csrf_token": request.app.state.manual_csrf_token,
            "result": result,
        },
    )


@router.get("/health", response_class=HTMLResponse)
def health(request: Request, context: DashboardContext = Depends(dashboard_context)):
    session = context.session
    sources = []
    for source in (Source.SEATGEEK, Source.STUBHUB, Source.TICKETMASTER):
        row = session.scalar(
            select(ConnectorRunRow)
            .where(ConnectorRunRow.source == source.value)
            .order_by(ConnectorRunRow.started_at.desc(), ConnectorRunRow.id.desc())
            .limit(1)
        )
        if row is None:
            sources.append(
                {
                    "source": source_label(source),
                    "state": "Not run yet",
                    "started": "Unknown",
                    "finished": "Unknown",
                    "count": "0",
                    "error": None,
                }
            )
            continue
        state = "In progress" if row.success is None else ("Success" if row.success else "Failure")
        sources.append(
            {
                "source": source_label(source),
                "state": state,
                    "started": local_time(row.started_at, context.timezone),
                    "finished": local_time(row.finished_at, context.timezone),
                "count": str(row.observation_count)
                if type(row.observation_count) is int and row.observation_count >= 0
                else "Unknown",
                "error": safe_error(row.redacted_error)
                if row.success is False
                else None,
                "freshness": freshness_text(
                    row.finished_at or row.started_at,
                    context.now,
                    context.freshness_minutes,
                )[0],
            }
        )
    return templates.TemplateResponse(
        request,
        "health.html",
        {"sources": sources, "dry_run": context.dry_run},
    )
