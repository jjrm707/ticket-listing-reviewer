import io
from pathlib import Path
import re
import socket
import struct
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from PIL import Image
import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select
from starlette.datastructures import UploadFile

from ticket_reviewer.data.schema import (
    EventRow,
    ManualReviewRow,
    ObservationRow,
    OpportunityRow,
    SourceEventRow,
)


def _png_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), "white").save(stream, format="PNG")
    return stream.getvalue()


def _padded_png(size: int) -> bytes:
    source = _png_bytes()
    iend = source[-12:]
    payload_size = size - len(source) - 12
    assert payload_size >= 0
    kind = b"paDd"
    payload = b"x" * payload_size
    chunk = struct.pack(">I", payload_size) + kind + payload
    chunk += struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    result = source[:-12] + chunk + iend
    assert len(result) == size
    return result


def _token(client) -> str:
    page = client.get("/manual")
    found = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert found is not None
    return found.group(1)


def _stage(client, tmp_path, text="Texans vs Colts\n2 tickets\nTotal $244"):
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(extract_text=lambda _path: text)
    token = _token(client)
    response = client.post(
        "/manual/extract",
        data={"csrf_token": token, "reference_url": "https://www.tickpick.com/a#x"},
        files={"screenshot": ("listing.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 200
    found = re.search(r'name="review_id" value="([1-9]\d*)"', response.text)
    assert found is not None
    return int(found.group(1)), token, response


def _confirmation(review_id: int, token: str, **overrides):
    data = {
        "csrf_token": token,
        "review_id": str(review_id),
        "event": "Houston Texans vs Colts",
        "team": "texans",
        "opponent": "Colts",
        "marketplace": "TickPick",
        "reference_url": "https://www.tickpick.com/a?private=1",
        "kickoff": "2026-09-13T12:00:00-05:00",
        "venue": "NRG Stadium",
        "section": "123",
        "row": "G",
        "quantity": "2",
        "per_ticket_price": "110.00",
        "fees": "24.00",
        "tax": "",
        "total": "244.00",
    }
    data.update(overrides)
    return data


def test_manual_review_page_has_accessible_local_upload_form(client):
    response = client.get("/manual")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'enctype="multipart/form-data"' in response.text
    assert 'type="file"' in response.text
    assert 'name="screenshot"' in response.text
    assert 'name="reference_url"' in response.text
    assert 'name="csrf_token"' in response.text
    assert "Manual review" in response.text


def test_extract_uses_injected_ocr_once_and_only_stages_a_review(
    client, session_factory, tmp_path
):
    calls = []

    def extract_text(path):
        calls.append(path)
        return "Texans vs Colts\nSection 123 Row G\n2 tickets\nTotal $244"

    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(extract_text=extract_text)
    token = _token(client)

    response = client.post(
        "/manual/extract",
        data={"csrf_token": token, "reference_url": "https://www.tickpick.com/a#x"},
        files={"screenshot": ("../../secret.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    assert len(calls) == 1
    assert "Screenshot details are suggestions" in response.text
    assert "Texans vs Colts" in response.text
    assert "../../secret.png" not in response.text
    assert "#x" not in response.text
    with session_factory() as session:
        assert session.scalar(select(func.count(ManualReviewRow.id))) == 1
        assert session.scalar(select(func.count(ObservationRow.id))) == 0
        assert session.scalar(select(func.count(OpportunityRow.id))) == 0


def test_manual_posts_require_exact_csrf_before_ocr(client, tmp_path):
    calls = []
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(extract_text=lambda path: calls.append(path))

    response = client.post(
        "/manual/extract",
        files={"screenshot": ("secret.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 400
    assert calls == []
    assert "secret.png" not in response.text


@pytest.mark.parametrize("failure", ["csrf", "field-name"])
def test_extract_explicitly_closes_parsed_upload_on_application_rejection(
    client, tmp_path, monkeypatch, failure
):
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(extract_text=lambda _path: "unused")
    original_close = UploadFile.close
    closed = []

    async def recording_close(upload):
        await original_close(upload)
        closed.append(upload)

    monkeypatch.setattr(UploadFile, "close", recording_close)
    if failure == "csrf":
        data = {"csrf_token": "stale"}
        field_name = "screenshot"
    else:
        data = {"csrf_token": _token(client)}
        field_name = "x" * 81

    response = client.post(
        "/manual/extract",
        data=data,
        files={field_name: ("secret.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 400
    assert len(closed) == 1
    assert closed[0].file.closed


def test_extract_rejects_invalid_and_mismatched_mime_without_ocr(client, tmp_path):
    calls = []
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(extract_text=lambda path: calls.append(path))
    token = _token(client)

    invalid = client.post(
        "/manual/extract",
        data={"csrf_token": token},
        files={"screenshot": ("x.gif", b"GIF89a", "image/gif")},
    )
    mismatch = client.post(
        "/manual/extract",
        data={"csrf_token": token},
        files={"screenshot": ("x.jpg", _png_bytes(), "image/jpeg")},
    )

    assert invalid.status_code == 415
    assert mismatch.status_code == 400
    assert calls == []


def test_extract_rejects_more_than_ten_mib_without_ocr(client, tmp_path):
    calls = []
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(extract_text=lambda path: calls.append(path))
    token = _token(client)

    response = client.post(
        "/manual/extract",
        data={"csrf_token": token},
        files={"screenshot": ("x.png", b"x" * (10 * 1024 * 1024 + 1), "image/png")},
    )

    assert response.status_code == 413
    assert calls == []


def test_multipart_parser_stops_oversize_file_before_route_rereads_spool(
    client, tmp_path, monkeypatch
):
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(extract_text=lambda _path: "unused")
    original_read = UploadFile.read
    reads = []

    async def recording_read(upload, size=-1):
        reads.append(size)
        return await original_read(upload, size)

    monkeypatch.setattr(UploadFile, "read", recording_read)

    response = client.post(
        "/manual/extract",
        data={"csrf_token": _token(client)},
        files={"screenshot": ("x.png", b"x" * (10 * 1024 * 1024 + 1), "image/png")},
    )

    assert response.status_code == 413
    assert reads == []


def test_extract_accepts_exactly_ten_mib_when_image_is_valid(client, tmp_path):
    calls = []
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(
        extract_text=lambda path: calls.append(path) or "Texans vs Colts\n2 tickets\nTotal $244"
    )

    response = client.post(
        "/manual/extract",
        data={"csrf_token": _token(client)},
        files={"screenshot": ("x.png", _padded_png(10 * 1024 * 1024), "image/png")},
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_extract_bounds_injected_ocr_text_before_storage(client, session_factory, tmp_path):
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(
        extract_text=lambda _path: "\x00secret\n" * 10_000
    )

    response = client.post(
        "/manual/extract",
        data={"csrf_token": _token(client)},
        files={"screenshot": ("x.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    with session_factory() as session:
        row = session.scalar(select(ManualReviewRow))
        assert len(row.ocr_text) <= 32_768
        assert "\x00" not in row.ocr_text


def test_extract_rejects_private_or_credential_reference_without_fetching(client, tmp_path):
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(extract_text=lambda _path: "unused")
    token = _token(client)

    response = client.post(
        "/manual/extract",
        data={"csrf_token": token, "reference_url": "http://user:pass@127.0.0.1/private"},
        files={"screenshot": ("x.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 400
    assert "user" not in response.text
    assert "pass" not in response.text


def test_public_reference_remains_inert_through_extract_and_confirm(
    client, tmp_path, monkeypatch
):
    def fail_network(*_args, **_kwargs):
        raise AssertionError("manual review attempted network access")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(urllib.request, "urlopen", fail_network)
    review_id, token, extracted = _stage(client, tmp_path)

    confirmed = client.post(
        "/manual/confirm", data=_confirmation(review_id, token)
    )

    assert extracted.status_code == 200
    assert confirmed.status_code == 200


def test_extract_rejects_percent_encoded_reference_controls(client, tmp_path):
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(extract_text=lambda _path: "unused")

    response = client.post(
        "/manual/extract",
        data={
            "csrf_token": _token(client),
            "reference_url": "https://example.com/listing%0Aprivate",
        },
        files={"screenshot": ("x.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    "reference",
    [
        "http://127.1/private",
        "http://2130706433/private",
        "https://example.com/%E2%80%AEprivate",
    ],
)
def test_extract_rejects_obscured_local_or_unicode_control_references(
    client, tmp_path, reference
):
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(extract_text=lambda _path: "unused")

    response = client.post(
        "/manual/extract",
        data={"csrf_token": _token(client), "reference_url": reference},
        files={"screenshot": ("x.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 400


def test_extract_escapes_reference_content_and_never_renders_raw_ocr(client, tmp_path):
    client.app.state.settings.screenshot_directory = tmp_path / "screenshots"
    client.app.state.ocr_engine = SimpleNamespace(
        extract_text=lambda _path: "<script>secret()</script>\nTexans vs Colts\n2 tickets\nTotal $244"
    )

    response = client.post(
        "/manual/extract",
        data={
            "csrf_token": _token(client),
            "reference_url": "https://example.com/?q=<script>alert(1)</script>",
        },
        files={"screenshot": ("x.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "secret()" not in response.text
    assert "&lt;script&gt;" in response.text


def test_confirm_revalidates_corrections_scores_once_and_is_idempotent(
    client, session_factory, tmp_path
):
    review_id, token, _response = _stage(client, tmp_path)

    first = client.post("/manual/confirm", data=_confirmation(review_id, token))
    second = client.post("/manual/confirm", data=_confirmation(review_id, token))

    assert first.status_code == 200
    assert second.status_code == 200
    assert "Review confirmed" in first.text
    assert "already confirmed" in second.text
    with session_factory() as session:
        review = session.get(ManualReviewRow, review_id)
        assert review.confirmed_at is not None
        assert review.corrected_payload["reference_url"].endswith("?private=1")
        assert session.scalar(select(func.count(EventRow.id))) == 1
        assert session.scalar(select(func.count(SourceEventRow.id))) == 1
        assert session.scalar(select(func.count(ObservationRow.id))) == 1
        assert session.scalar(select(func.count(OpportunityRow.id))) == 1
        observation = session.scalar(select(ObservationRow))
        assert observation.source == "manual"
        assert observation.pair_price == 244
        assert observation.buyer_fees == 0
        assert observation.estimated_tax == 0
        assert observation.quantity_available == 2
        assert observation.can_buy_pair is True
        assert observation.listing_url is None


def test_confirm_rejects_over_budget_and_leaves_review_editable(
    client, session_factory, tmp_path
):
    review_id, token, _response = _stage(client, tmp_path)

    response = client.post(
        "/manual/confirm",
        data=_confirmation(review_id, token, total="400.01"),
    )

    assert response.status_code == 400
    with session_factory() as session:
        assert session.get(ManualReviewRow, review_id).confirmed_at is None
        assert session.scalar(select(func.count(ObservationRow.id))) == 0
        assert session.scalar(select(func.count(OpportunityRow.id))) == 0


def test_confirm_accepts_exact_four_hundred_dollar_runtime_boundary(
    client, session_factory, tmp_path
):
    review_id, token, _response = _stage(client, tmp_path)

    response = client.post(
        "/manual/confirm", data=_confirmation(review_id, token, total="400.00")
    )

    assert response.status_code == 200
    with session_factory() as session:
        assert session.scalar(select(ObservationRow.pair_price)) == Decimal("400.00")


def test_actionable_confirmation_commits_before_alert_and_persists_comparable_ids(
    client, session_factory, tmp_path
):
    with session_factory() as session:
        event = EventRow(
            team="texans",
            opponent="Colts",
            venue="NRG Stadium",
            starts_at=client.app.state.clock() + timedelta(days=36, hours=1, minutes=30),
            is_home=True,
        )
        session.add(event)
        session.flush()
        comparable_ids = []
        for index in range(3):
            row = ObservationRow(
                event_id=event.id,
                source="stubhub",
                event_external_id="stubhub-colts",
                observed_at=client.app.state.clock() - timedelta(minutes=index),
                kind="listing",
                currency="USD",
                pair_price=Decimal("400.00"),
                buyer_fees=Decimal("0.00"),
                estimated_tax=Decimal("0.00"),
                section="123",
                row=f"{index + 1}",
                quantity_available=2,
                can_buy_pair=True,
                listing_id=f"comp-{index}",
                listing_identity=f"listing:comp-{index}",
                freshness_at=client.app.state.clock() - timedelta(minutes=index),
            )
            session.add(row)
            session.flush()
            comparable_ids.append(row.id)
        session.commit()
    calls = []

    def evaluate(opportunity_id, now):
        with session_factory() as session:
            review = session.scalar(
                select(ManualReviewRow).where(ManualReviewRow.confirmed_at.is_not(None))
            )
            assert review is not None
            assert session.get(OpportunityRow, opportunity_id) is not None
        calls.append((opportunity_id, now))
        raise RuntimeError("notification stays isolated")

    client.app.state.services.alert_service = SimpleNamespace(evaluate_and_send=evaluate)
    review_id, token, _response = _stage(client, tmp_path)

    response = client.post("/manual/confirm", data=_confirmation(review_id, token))

    assert response.status_code == 200
    assert len(calls) == 1
    with session_factory() as session:
        opportunity = session.scalar(
            select(OpportunityRow)
            .join(ObservationRow, OpportunityRow.observation_id == ObservationRow.id)
            .where(ObservationRow.source == "manual")
        )
        assert opportunity.confidence == "medium"
        assert "candidate source is manually corrected OCR" in opportunity.risk_reasons
        assert opportunity.scenarios[0]["comparable_observation_ids"] == sorted(comparable_ids)


def test_confirm_scoring_failure_rolls_back_every_scoring_row(
    client, session_factory, tmp_path, monkeypatch
):
    review_id, token, _response = _stage(client, tmp_path)
    monkeypatch.setattr(
        "ticket_reviewer.web.routes.estimate_opportunity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private")),
    )

    response = client.post("/manual/confirm", data=_confirmation(review_id, token))

    assert response.status_code == 500
    assert "private" not in response.text
    with session_factory() as session:
        assert session.get(ManualReviewRow, review_id).confirmed_at is None
        assert session.scalar(select(func.count(EventRow.id))) == 0
        assert session.scalar(select(func.count(SourceEventRow.id))) == 0
        assert session.scalar(select(func.count(ObservationRow.id))) == 0
        assert session.scalar(select(func.count(OpportunityRow.id))) == 0


def test_overlapping_confirms_create_one_observation_and_opportunity(
    client, session_factory, tmp_path
):
    review_id, token, _response = _stage(client, tmp_path)
    payload = _confirmation(review_id, token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(lambda _index: client.post("/manual/confirm", data=payload), range(2))
        )

    assert [response.status_code for response in responses] == [200, 200]
    assert sum("already confirmed" in response.text for response in responses) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count(ObservationRow.id))) == 1
        assert session.scalar(select(func.count(OpportunityRow.id))) == 1


def test_confirm_requires_single_fresh_csrf_value(client, tmp_path):
    review_id, token, _response = _stage(client, tmp_path)

    duplicate = client.post(
        "/manual/confirm",
        content=f"csrf_token={token}&csrf_token={token}&review_id={review_id}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    stale = client.post(
        "/manual/confirm",
        data={"csrf_token": "stale", "review_id": str(review_id)},
    )

    assert duplicate.status_code == 400
    assert stale.status_code == 400


def test_confirm_rejects_non_ascii_csrf_without_server_error(client, tmp_path):
    review_id, _token_value, _response = _stage(client, tmp_path)

    response = client.post(
        "/manual/confirm",
        data={"csrf_token": "\u00e9", "review_id": str(review_id)},
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": "2.0"},
        {"team": "aggies"},
        {"venue": "AT&T Stadium"},
        {"kickoff": "2026-09-13T17:00:00Z"},
        {"kickoff": "2026-11-01T01:30:00-05:00"},
        {"kickoff": "2026-03-13T12:00:00-05:00"},
        {"kickoff": "2028-09-13T12:00:00-05:00"},
        {"event": "Houston Texans at Colts"},
        {"event": "Houston Texans vs Colts parking pass"},
        {"event": "Colts vs Houston Texans"},
        {
            "event": "Houston Texans vs Texas A M Aggies",
            "opponent": "Texas A M Aggies",
        },
        {"event": "Houston Texans vs Colts", "opponent": "Colt"},
        {"kickoff": "2026-09-13T12:00:00+14:00"},
    ],
)
def test_confirm_rejects_non_pair_nonhome_or_invalid_kickoff_values(
    client, session_factory, tmp_path, overrides
):
    review_id, token, _response = _stage(client, tmp_path)

    response = client.post(
        "/manual/confirm", data=_confirmation(review_id, token, **overrides)
    )

    assert response.status_code == 400
    with session_factory() as session:
        assert session.get(ManualReviewRow, review_id).confirmed_at is None
        assert session.scalar(select(func.count(OpportunityRow.id))) == 0


def test_extract_purges_only_expired_unconfirmed_safe_staged_files(
    client, session_factory, tmp_path
):
    root = tmp_path / "screenshots"
    root.mkdir()
    client.app.state.settings.screenshot_directory = root
    old_name = "a" * 32 + ".png"
    kept_name = "b" * 32 + ".png"
    (root / old_name).write_bytes(_png_bytes())
    (root / kept_name).write_bytes(_png_bytes())
    with session_factory() as session:
        session.add_all(
            [
                ManualReviewRow(
                    screenshot_path=old_name,
                    ocr_text="",
                    corrected_payload={
                        "state": "unconfirmed",
                        "staged_at": (client.app.state.clock() - timedelta(days=2)).isoformat(),
                    },
                ),
                ManualReviewRow(
                    screenshot_path=kept_name,
                    ocr_text="",
                    corrected_payload={
                        "state": "confirmed",
                        "staged_at": (client.app.state.clock() - timedelta(days=2)).isoformat(),
                    },
                    confirmed_at=client.app.state.clock(),
                ),
            ]
        )
        session.commit()
    client.app.state.ocr_engine = SimpleNamespace(
        extract_text=lambda _path: "Texans vs Colts\n2 tickets\nTotal $244"
    )

    response = client.post(
        "/manual/extract",
        data={"csrf_token": _token(client)},
        files={"screenshot": ("x.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    assert not (root / old_name).exists()
    assert (root / kept_name).is_file()
    with session_factory() as session:
        assert session.scalar(
            select(func.count(ManualReviewRow.id)).where(ManualReviewRow.screenshot_path == old_name)
        ) == 0
        assert session.scalar(
            select(func.count(ManualReviewRow.id)).where(ManualReviewRow.screenshot_path == kept_name)
        ) == 1


def test_expired_review_unlink_failure_restores_database_row(
    client, session_factory, tmp_path, monkeypatch
):
    root = tmp_path / "screenshots"
    root.mkdir()
    client.app.state.settings.screenshot_directory = root
    old_name = "c" * 32 + ".png"
    old_path = root / old_name
    old_path.write_bytes(_png_bytes())
    with session_factory() as session:
        review = ManualReviewRow(
            screenshot_path=old_name,
            ocr_text="private",
            corrected_payload={
                "state": "unconfirmed",
                "staged_at": (client.app.state.clock() - timedelta(days=2)).isoformat(),
            },
        )
        session.add(review)
        session.commit()
        review_id = review.id
    original_unlink = Path.unlink

    def fail_old_unlink(path, *args, **kwargs):
        if path == old_path:
            raise OSError("synthetic unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_unlink)
    client.app.state.ocr_engine = SimpleNamespace(
        extract_text=lambda _path: "Texans vs Colts\n2 tickets\nTotal $244"
    )

    response = client.post(
        "/manual/extract",
        data={"csrf_token": _token(client)},
        files={"screenshot": ("x.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    assert old_path.is_file()
    with session_factory() as session:
        assert session.get(ManualReviewRow, review_id) is not None


def test_delete_removes_only_private_review_and_screenshot(client, session_factory, tmp_path):
    review_id, token, _response = _stage(client, tmp_path)
    confirmed = client.post("/manual/confirm", data=_confirmation(review_id, token))
    assert confirmed.status_code == 200
    with session_factory() as session:
        path = tmp_path / "screenshots" / session.get(ManualReviewRow, review_id).screenshot_path
        assert path.is_file()

    deleted = client.post(
        f"/manual/{review_id}/delete", data={"csrf_token": token}
    )

    assert deleted.status_code == 200
    assert "history remains" in deleted.text
    assert not path.exists()
    with session_factory() as session:
        assert session.get(ManualReviewRow, review_id) is None
        assert session.scalar(select(func.count(EventRow.id))) == 1
        assert session.scalar(select(func.count(ObservationRow.id))) == 1
        assert session.scalar(select(func.count(OpportunityRow.id))) == 1


def test_delete_rejects_foreign_stored_path_without_touching_it(
    client, session_factory, tmp_path
):
    root = tmp_path / "screenshots"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_bytes())
    client.app.state.settings.screenshot_directory = root
    with session_factory() as session:
        review = ManualReviewRow(screenshot_path="../outside.png", ocr_text="private")
        session.add(review)
        session.commit()
        review_id = review.id

    response = client.post(
        f"/manual/{review_id}/delete", data={"csrf_token": _token(client)}
    )

    assert response.status_code == 500
    assert outside.is_file()
    with session_factory() as session:
        assert session.get(ManualReviewRow, review_id) is not None


def test_delete_unlink_failure_returns_error_and_restores_review(
    client, session_factory, tmp_path, monkeypatch
):
    review_id, token, _response = _stage(client, tmp_path)
    with session_factory() as session:
        path = tmp_path / "screenshots" / session.get(
            ManualReviewRow, review_id
        ).screenshot_path
    original_unlink = Path.unlink

    def fail_review_unlink(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError("synthetic unlink failure")
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_review_unlink)

    response = client.post(
        f"/manual/{review_id}/delete", data={"csrf_token": token}
    )

    assert response.status_code == 500
    assert path.is_file()
    with session_factory() as session:
        assert session.get(ManualReviewRow, review_id) is not None


def test_confirm_queries_only_fresh_observations_for_scoring(
    client, session_factory, tmp_path
):
    review_id, token, _response = _stage(client, tmp_path)
    engine = session_factory.kw["bind"]
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        if "FROM observations" in statement:
            statements.append(" ".join(statement.split()).casefold())

    sqlalchemy_event.listen(engine, "before_cursor_execute", record_statement)
    try:
        response = client.post(
            "/manual/confirm", data=_confirmation(review_id, token)
        )
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    assert any(
        "observations.observed_at >=" in statement
        and "observations.observed_at <=" in statement
        for statement in statements
    )
