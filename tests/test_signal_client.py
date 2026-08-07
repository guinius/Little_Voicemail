"""Voice note attachment shape.

Regression coverage for two real bugs found sending to iOS:

  * signal-cli accepts --attachment as either a plain file path
    (content-type guessed from the file) or a data: URI with an explicit
    MIME type. Passing a plain path let the guess come back wrong on a
    minimal headless install, so recipients saw a downloadable file
    instead of a playable voice-message bubble even with voiceNote set.
  * Ogg/Opus - the format Signal's own docs point to - doesn't actually
    play on Signal iOS (signalapp/Signal-iOS#5771, long-standing and
    unresolved): the bubble showed up but tapping it did nothing. AAC in
    an M4A container is what both mobile platforms actually record their
    own voice notes in, so that's what audio.py encodes to now.

See signal_client.py's _m4a_data_uri().
"""

import base64

import pytest

from src.signal_client import SignalClient, _m4a_data_uri


def test_data_uri_is_tagged_audio_mp4(tmp_path):
    audio = tmp_path / "rec-123.m4a"
    audio.write_bytes(b"not really aac, just test bytes")

    uri = _m4a_data_uri(audio)

    assert uri.startswith("data:audio/mp4;filename=rec-123.m4a;base64,")


def test_data_uri_round_trips_the_file_bytes(tmp_path):
    audio = tmp_path / "clip.m4a"
    payload = bytes(range(256)) * 4  # arbitrary binary content
    audio.write_bytes(payload)

    uri = _m4a_data_uri(audio)
    encoded = uri.split(",", 1)[1]

    assert base64.b64decode(encoded) == payload


@pytest.mark.asyncio
async def test_send_voice_note_sends_a_data_uri_not_a_bare_path(tmp_path):
    """The attachment-shape regression: a bare filesystem path used to go
    straight into the "attachment" field, leaving signal-cli to guess the
    content-type."""
    audio = tmp_path / "rec-999.m4a"
    audio.write_bytes(b"fake aac data")

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
    assert attachment.startswith("data:audio/mp4;filename=rec-999.m4a;base64,")
