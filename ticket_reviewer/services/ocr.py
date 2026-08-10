"""Local-only OCR boundary and conservative listing-text suggestions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import stat
import unicodedata
import warnings
from typing import Protocol

from PIL import Image, ImageOps, UnidentifiedImageError
import pytesseract


_MAX_TEXT_CHARS = 32_768
_MAX_LINES = 200
_MAX_LINE_CHARS = 512
_MAX_FIELD_CHARS = 160
_MAX_MESSAGES = 12
_MAX_PIXELS = 40_000_000
_MAX_DIMENSION = 12_000
_MAX_METADATA_BYTES = 64_000
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_TESSERACT_CONFIG = "--oem 3 --psm 6"
_MONEY_VALUE = r"(?:\d{1,3}(?:,\d{3})+|\d{1,10})(?:\.\d{1,2})?"
_SECTION = re.compile(
    r"\bsection\s*:?[ \t]*([A-Za-z0-9][A-Za-z0-9 -]{0,31}?)(?=[ \t]+row\b|$)",
    re.IGNORECASE,
)
_ROW = re.compile(r"\brow\s*:?[ \t]*([A-Za-z0-9][A-Za-z0-9 -]{0,31})$", re.IGNORECASE)
_QUANTITY = re.compile(r"^\s*(\d{1,3})\s+(?:adjacent\s+)?tickets?\s*$", re.IGNORECASE)
_QUANTITY_RANGE = re.compile(r"\b\d{1,3}\s*(?:-|–|—|to)\s*\d{1,3}\s+tickets?\b", re.IGNORECASE)
_MONEY_PATTERNS = {
    "per_ticket_price": re.compile(
        rf"^\s*(?:(?:(?:price|ticket\s+price)\s+each)\s*:?[ \t]*\$\s*({_MONEY_VALUE})|\$\s*({_MONEY_VALUE})\s*(?:each|per\s+ticket))\s*$",
        re.IGNORECASE,
    ),
    "fees": re.compile(rf"^\s*(?:buyer\s+)?fees?\s*:?[ \t]*\$\s*({_MONEY_VALUE})\s*$", re.IGNORECASE),
    "tax": re.compile(rf"^\s*(?:estimated\s+)?tax\s*:?[ \t]*\$\s*({_MONEY_VALUE})\s*$", re.IGNORECASE),
    "total": re.compile(rf"^\s*(?:all[- ]in\s+)?total(?:\s+cost|\s+price)?\s*:?[ \t]*\$\s*({_MONEY_VALUE})\s*$", re.IGNORECASE),
}
_MARKETPLACE = re.compile(r"^\s*(?:marketplace|source)\s*:\s*([^\s].{0,79})$", re.IGNORECASE)
_KICKOFF = re.compile(r"^\s*(?:kickoff|date(?:\s+and\s+time)?|starts?)\s*:\s*([^\s].{0,127})$", re.IGNORECASE)
_VENUE = re.compile(r"^\s*venue\s*:\s*([^\s].{0,127})$", re.IGNORECASE)
_CONTROL_OR_SURROGATE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_UNSAFE_PRICE_WORDS = re.compile(
    r"\b(?:from|starting\s+at|installments?|payments?|per\s+month)\b|~~|̶",
    re.IGNORECASE,
)
_NON_TICKET_PRODUCT = re.compile(r"\b(?:parking|parkade|lot\s+pass|parking\s+pass)\b", re.IGNORECASE)
_AWAY_OR_NEUTRAL = re.compile(r"\b(?:at|away|neutral(?:\s+site)?)\b", re.IGNORECASE)


class OcrEngine(Protocol):
    def extract_text(self, image_path: Path) -> str: ...


class OcrError(Exception):
    """A deliberately generic public-safe OCR failure."""

    def __init__(self) -> None:
        super().__init__("Unable to read that local image")


@dataclass(frozen=True, slots=True)
class ManualReviewDraft:
    event: str | None = None
    team: str | None = None
    opponent: str | None = None
    marketplace: str | None = None
    kickoff_text: str | None = None
    venue: str | None = None
    section: str | None = None
    row: str | None = None
    quantity: int | None = None
    per_ticket_price: Decimal | None = None
    fees: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    missing_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def normalize_ocr_text(value: object) -> str:
    """Return bounded printable text suitable for local parsing and storage."""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value[: _MAX_TEXT_CHARS * 2])
    value = _CONTROL_OR_SURROGATE.sub("", value)
    value = "".join(
        character
        for character in value
        if character in "\n\r\t"
        or not unicodedata.category(character).startswith("C")
    )
    lines: list[str] = []
    for raw in value.splitlines()[:_MAX_LINES]:
        line = " ".join(raw[:_MAX_LINE_CHARS].split())
        if line:
            lines.append(line)
    return "\n".join(lines)[:_MAX_TEXT_CHARS]


def _append_message(messages: list[str], message: str) -> None:
    if message not in messages and len(messages) < _MAX_MESSAGES:
        messages.append(message[:120])


def _unique_candidate(values: list[object], field: str, warnings_: list[str]):
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if len(unique) > 1:
        _append_message(warnings_, f"Conflicting {field} values were not used.")
        return None
    return unique[0] if unique else None


def _money(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or not Decimal("0") < value <= Decimal("9999999999.99"):
        return None
    return value


def _event_candidate(lines: list[str], warnings_: list[str]) -> tuple[str | None, str | None, str | None]:
    joined = " ".join(lines)
    if _NON_TICKET_PRODUCT.search(joined):
        _append_message(warnings_, "Parking and pass products are not supported.")
        return None, None, None
    candidates: list[tuple[str, str, str]] = []
    for line in lines:
        if len(line) > _MAX_FIELD_CHARS or _AWAY_OR_NEUTRAL.search(line):
            continue
        match = re.fullmatch(
            r"(?:Houston\s+)?Texans\s+(?:vs\.?|versus)\s+(.{1,80})",
            line,
            re.IGNORECASE,
        )
        team = "texans"
        if match is None:
            match = re.fullmatch(
                r"(?:Texas\s+A\s*&\s*M|Texas\s+A\s+M|Aggies)\s+(?:vs\.?|versus)\s+(.{1,80})",
                line,
                re.IGNORECASE,
            )
            team = "aggies"
        if match is None:
            continue
        opponent = match.group(1).strip(" .:-")
        if not opponent or _NON_TICKET_PRODUCT.search(opponent):
            continue
        candidates.append((line[:_MAX_FIELD_CHARS], team, opponent[:80]))
    unique: list[tuple[str, str, str]] = []
    for value in candidates:
        identity = (value[1], value[2].casefold())
        if not any((item[1], item[2].casefold()) == identity for item in unique):
            unique.append(value)
    if len(unique) != 1:
        if unique:
            _append_message(warnings_, "Conflicting event values were not used.")
        else:
            _append_message(warnings_, "A supported home event was not clear.")
        return None, None, None
    return unique[0]


def parse_listing_text(text: str) -> ManualReviewDraft:
    """Extract only explicit, non-conflicting suggestions from bounded OCR text."""
    normalized = normalize_ocr_text(text)
    lines = normalized.splitlines()
    warnings_: list[str] = []
    missing: list[str] = []
    if isinstance(text, str) and len(text) > _MAX_TEXT_CHARS:
        _append_message(warnings_, "OCR text was truncated to safe limits.")

    event, team, opponent = _event_candidate(lines, warnings_)
    sections: list[str] = []
    rows: list[str] = []
    quantities: list[int] = []
    money_values: dict[str, list[Decimal]] = {key: [] for key in _MONEY_PATTERNS}
    marketplaces: list[str] = []
    kickoffs: list[str] = []
    venues: list[str] = []
    invalid_money_fields: set[str] = set()
    saw_unsafe_price = False
    saw_quantity_range = False

    for line in lines:
        section = _SECTION.search(line)
        if section is not None:
            sections.append(section.group(1).strip())
        row = _ROW.search(line)
        if row is not None:
            rows.append(row.group(1).strip())
        if _QUANTITY_RANGE.search(line):
            saw_quantity_range = True
        else:
            quantity = _QUANTITY.fullmatch(line)
            if quantity is not None:
                quantities.append(int(quantity.group(1)))
        marketplace = _MARKETPLACE.fullmatch(line)
        if marketplace is not None:
            marketplaces.append(marketplace.group(1).strip()[:80])
        kickoff = _KICKOFF.fullmatch(line)
        if kickoff is not None:
            kickoffs.append(kickoff.group(1).strip()[:128])
        venue = _VENUE.fullmatch(line)
        if venue is not None:
            venues.append(venue.group(1).strip()[:128])
        if re.match(r"^\s*(?:all[- ]in\s+)?total\b", line, re.IGNORECASE):
            if _MONEY_PATTERNS["total"].fullmatch(line) is None:
                invalid_money_fields.add("total")
                saw_unsafe_price = True
        if _UNSAFE_PRICE_WORDS.search(line) or re.search(r"\$\s*-|-\s*\$", line):
            if "$" in line or "total" in line.casefold():
                saw_unsafe_price = True
            continue
        for field, pattern in _MONEY_PATTERNS.items():
            match = pattern.fullmatch(line)
            if match is not None:
                raw_value = next(group for group in match.groups() if group is not None)
                value = _money(raw_value)
                if value is None:
                    invalid_money_fields.add(field)
                    saw_unsafe_price = True
                else:
                    money_values[field].append(value)

    section = _unique_candidate(sections, "section", warnings_)
    row = _unique_candidate(rows, "row", warnings_)
    quantity = None if saw_quantity_range else _unique_candidate(quantities, "quantity", warnings_)
    marketplace = _unique_candidate(marketplaces, "marketplace", warnings_)
    kickoff_text = _unique_candidate(kickoffs, "kickoff", warnings_)
    if kickoff_text is not None and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?[+-]\d{2}:\d{2}",
        kickoff_text,
    ) is None:
        kickoff_text = None
        _append_message(warnings_, "An incomplete kickoff value was not used.")
    venue = _unique_candidate(venues, "venue", warnings_)
    parsed_money = {
        field: (
            None
            if field in invalid_money_fields
            else _unique_candidate(values, field.replace("_", " "), warnings_)
        )
        for field, values in money_values.items()
    }
    if saw_quantity_range:
        _append_message(warnings_, "Quantity ranges are not accepted.")
    if saw_unsafe_price:
        _append_message(warnings_, "A non-authoritative price was not used.")
    if parsed_money["total"] is None and any("$" in line for line in lines):
        _append_message(warnings_, "An unlabeled or incomplete price was not used as the total.")
    if event is None:
        missing.append("supported home event")
    if quantity is None:
        missing.append("quantity")
    if parsed_money["total"] is None:
        missing.append("total cost")
    if kickoff_text is None:
        missing.append("kickoff with timezone offset")
    return ManualReviewDraft(
        event=event,
        team=team,
        opponent=opponent,
        marketplace=marketplace,
        kickoff_text=kickoff_text,
        venue=venue,
        section=section,
        row=row,
        quantity=quantity,
        per_ticket_price=parsed_money["per_ticket_price"],
        fees=parsed_money["fees"],
        tax=parsed_money["tax"],
        total=parsed_money["total"],
        missing_fields=tuple(missing),
        warnings=tuple(warnings_),
    )


class TesseractOcrEngine:
    """Validate and normalize a local image before invoking local Tesseract."""

    def extract_text(self, image_path: Path) -> str:
        try:
            path = Path(image_path)
            details = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISREG(details.st_mode)
                or details.st_size <= 0
                or details.st_size > _MAX_IMAGE_BYTES
            ):
                raise OcrError()
            with path.open("rb") as handle:
                prefix = handle.read(8)
                handle.seek(max(0, details.st_size - 12))
                suffix = handle.read(12)
            png = prefix == b"\x89PNG\r\n\x1a\n" and suffix == b"\x00\x00\x00\x00IEND\xaeB`\x82"
            jpeg = prefix.startswith(b"\xff\xd8\xff") and suffix.endswith(b"\xff\xd9")
            if not (png or jpeg):
                raise OcrError()
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as source:
                    if source.format not in {"PNG", "JPEG"}:
                        raise OcrError()
                    if getattr(source, "n_frames", 1) != 1:
                        raise OcrError()
                    width, height = source.size
                    metadata_size = sum(
                        len(str(key)) + len(str(value))
                        for key, value in source.info.items()
                    )
                    if (
                        width <= 0
                        or height <= 0
                        or width > _MAX_DIMENSION
                        or height > _MAX_DIMENSION
                        or width * height > _MAX_PIXELS
                        or metadata_size > _MAX_METADATA_BYTES
                    ):
                        raise OcrError()
                    source.load()
                    oriented = ImageOps.exif_transpose(source)
                    try:
                        grayscale = oriented.convert("L")
                    finally:
                        if oriented is not source:
                            oriented.close()
                    try:
                        normalized = ImageOps.autocontrast(grayscale, cutoff=1)
                    finally:
                        grayscale.close()
                    try:
                        value = pytesseract.image_to_string(
                            normalized,
                            lang="eng",
                            config=_TESSERACT_CONFIG,
                            timeout=15,
                        )
                    finally:
                        normalized.close()
        except OcrError:
            raise
        except (
            FileNotFoundError,
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            pytesseract.TesseractError,
            pytesseract.TesseractNotFoundError,
        ):
            raise OcrError() from None
        except Exception:
            raise OcrError() from None
        return normalize_ocr_text(value)
