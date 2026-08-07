"""Voice note attachment shape.

Regression coverage for a real bug: signal-cli accepts --attachment as
either a plain file path (content-type guessed from the file) or a
data: URI with an explicit MIME type. Passing a plain path let the guess
come back wrong on a minimal headless install, so recipients saw a
downloadable .ogg file instead of a playable voice-message bubble even
with voiceNote set. See signal_client.py's _ogg_data_uri().
"""

import base64

import pytest

from src.signal_client import SignalClient, _ogg_data_uri


def test_data_uri_is_tagged_audio_ogg(tmp_path):
    audio = tmp_path / "rec-123.ogg"
    audio.write_bytes(b"not really opus, just test bytes")

    uri = _ogg_data_uri(audio)

    assert uri.startswith("data:audio/ogg;filename=rec-123.ogg;base64,")


def test_data_uri_round_trips_the_file_bytes(tmp_path):
    audio = tmp_path / "clip.ogg"
    payload = bytes(range(256)) * 4  # arbitrary binary content
    audio.write_bytes(payload)

    uri = _ogg_data_uri(audio)
    encoded = uri.split(",", 1)[1]

    assert base64.b64decode(encoded) == payload


@pytest.mark.asyncio
async def test_send_voice_note_sends_a_data_uri_not_a_bare_path(tmp_path):
    """The actual regression: a bare filesystem path used to go straight
    into the "attachment" field, leaving signal-cli to guess the
    content-type."""
    audio = tmp_path / "rec-999.ogg"
    audio.write_bytes(b"fake opus data")

    client = SignalClient(account="+447700900000")
    calls = []

    async def fake_call(method, params=None, timeout=30.0):
        calls.append((method, params))
        return {"timestamp": 1234}

    client._call = fake_call

    await client.send_voice_note("+447700900123", audio)

    assert len(calls) == 1
    method, params = calls[0]
    assert method == "send"
    assert params["voiceNote"] is True
    assert params["recipient"] == ["+447700900123"]
    (attachment,) = params["attachment"]
    assert attachment.startswith("data:audio/ogg;filename=rec-999.ogg;base64,")
