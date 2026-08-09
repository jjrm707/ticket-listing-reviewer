import io
from pathlib import Path
import shutil

from PIL import Image
import pytest

from ticket_reviewer.services.ocr import OcrError, TesseractOcrEngine


def _save_image(path: Path, *, image_format: str = "PNG") -> None:
    Image.new("RGB", (40, 30), "white").save(path, format=image_format)


def test_constructor_has_no_file_or_process_side_effect(monkeypatch):
    monkeypatch.setattr("pytesseract.image_to_string", lambda *_a, **_k: pytest.fail("called"))

    TesseractOcrEngine()


def test_extract_normalizes_image_and_bounds_returned_text(tmp_path, monkeypatch):
    path = tmp_path / "listing.png"
    _save_image(path)
    calls = []

    def fake_ocr(image, *, lang, config, timeout):
        calls.append((image.mode, image.size, lang, config, timeout))
        return "\x00  Texans   vs Colts  \n\n Total $244 "

    monkeypatch.setattr("pytesseract.image_to_string", fake_ocr)

    result = TesseractOcrEngine().extract_text(path)

    assert result == "Texans vs Colts\nTotal $244"
    assert calls == [("L", (40, 30), "eng", "--oem 3 --psm 6", 15)]


@pytest.mark.parametrize("failure", [FileNotFoundError("secret-path"), RuntimeError("secret")])
def test_extract_maps_failures_to_generic_nonsecret_error(tmp_path, monkeypatch, failure):
    path = tmp_path / "private-name.png"
    _save_image(path)
    monkeypatch.setattr("pytesseract.image_to_string", lambda *_a, **_k: (_ for _ in ()).throw(failure))

    with pytest.raises(OcrError) as caught:
        TesseractOcrEngine().extract_text(path)

    public = str(caught.value)
    assert public == "Unable to read that local image"
    assert "secret" not in public
    assert "private-name" not in public


def test_extract_rejects_animation_before_ocr(tmp_path, monkeypatch):
    path = tmp_path / "animation.png"
    frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
    frames[0].save(path, format="PNG", save_all=True, append_images=frames[1:])
    monkeypatch.setattr("pytesseract.image_to_string", lambda *_a, **_k: pytest.fail("called"))

    with pytest.raises(OcrError):
        TesseractOcrEngine().extract_text(path)


def test_extract_rejects_trailing_polyglot_bytes_before_ocr(tmp_path, monkeypatch):
    path = tmp_path / "polyglot.png"
    _save_image(path)
    with path.open("ab") as handle:
        handle.write(b"<script>private</script>")
    monkeypatch.setattr("pytesseract.image_to_string", lambda *_a, **_k: pytest.fail("called"))

    with pytest.raises(OcrError):
        TesseractOcrEngine().extract_text(path)


@pytest.mark.integration
def test_live_tesseract_smoke(tmp_path):
    if shutil.which("tesseract") is None:
        pytest.skip("local Tesseract executable is not installed")
    path = tmp_path / "smoke.png"
    image = Image.new("RGB", (500, 120), "white")
    image.save(path, format="PNG")

    assert isinstance(TesseractOcrEngine().extract_text(path), str)
