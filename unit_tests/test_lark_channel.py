"""Tests for the Lark / Feishu channel implementation."""

import base64
import hmac
import hashlib
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def lark_channel():
    from backend.channels.lark import LarkChannel
    return LarkChannel(
        channel_id="ch_lark_1",
        agent_id="agent_1",
        config={
            "app_id": "cli_test",
            "app_secret": "secret_test",
            "encrypt_key": "encrypt_test",
            "base_url": "https://open.larksuite.com",
        },
    )


def _make_token_response(token="tok", expire=7200):
    return {"code": 0, "msg": "ok", "tenant_access_token": token, "expire": expire}


def test_get_channel_type():
    from backend.channels.lark import LarkChannel
    assert LarkChannel.get_channel_type() == "lark"


def test_start_requires_credentials():
    from backend.channels.lark import LarkChannel
    ch = LarkChannel("ch", "ag", {})
    with pytest.raises(ValueError, match="app_id and app_secret are required"):
        ch.start()


def test_start_fetches_token_and_sets_running(lark_channel):
    with patch("backend.channels.lark.requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: _make_token_response("tok1", 7200),
            raise_for_status=lambda: None,
        )
        lark_channel.start()
        assert lark_channel.is_running
        assert lark_channel._token_cache.get() == "tok1"
        mock_post.assert_called_once()


def test_token_cache_reuses_valid_token(lark_channel):
    with patch("backend.channels.lark.requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: _make_token_response("tok2", 7200),
            raise_for_status=lambda: None,
        )
        lark_channel._token_cache.set("cached", 9999)
        token = lark_channel._ensure_token()
        assert token == "cached"
        mock_post.assert_not_called()


def test_token_refresh_on_expired_cache(lark_channel):
    with patch("backend.channels.lark.requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: _make_token_response("tok3", 7200),
            raise_for_status=lambda: None,
        )
        lark_channel._token_cache.set("old", -1)
        token = lark_channel._ensure_token()
        assert token == "tok3"
        mock_post.assert_called_once()


def test_ws_endpoint_url_accepts_lark_sdk_field(lark_channel):
    ws_url = lark_channel._extract_ws_url({"data": {"URL": "wss://example.test/ws"}})
    assert ws_url == "wss://example.test/ws"


def test_ws_endpoint_url_accepts_lowercase_field(lark_channel):
    ws_url = lark_channel._extract_ws_url({"data": {"url": "wss://example.test/ws"}})
    assert ws_url == "wss://example.test/ws"


def test_websocket_connect_kwargs_disable_websockets15_proxy():
    from backend.channels.lark import _websockets_connect_kwargs
    assert _websockets_connect_kwargs() == {"proxy": None}


def test_extract_sender_display_name_prefers_event_name(lark_channel):
    event = {
        "sender": {
            "sender_id": {"open_id": "ou_123"},
            "name": "Alice Chen",
            "sender_type": "user",
        }
    }
    assert lark_channel._extract_sender_display_name(event) == "Alice Chen"


def test_fetch_lark_user_display_name_from_contact_api(lark_channel):
    with patch.object(lark_channel, "_api_get") as mock_api:
        mock_api.return_value = {
            "code": 0,
            "data": {
                "user": {
                    "name": "Alice Chen",
                    "en_name": "Alice C.",
                }
            },
        }
        assert lark_channel._fetch_lark_user_display_name("ou_123") == "Alice Chen"
        mock_api.assert_called_once_with(
            "/open-apis/contact/v3/users/ou_123",
            params={"user_id_type": "open_id"},
        )


def test_handle_callback_stores_lark_event_display_name(lark_channel):
    lark_channel._running = True
    lark_channel._token_cache.set("tok", 9999)
    payload = {
        "event": {
            "type": "im.message.receive_v1",
            "message": {
                "message_type": "text",
                "content": json.dumps({"text": "hello bot"}),
            },
            "sender": {
                "sender_id": {"open_id": "ou_123"},
                "name": "Alice Chen",
                "sender_type": "user",
            },
        }
    }

    with patch("backend.agent_runtime.agent_runtime") as mock_runtime, \
         patch("models.db.db") as mock_db, \
         patch("backend.event_stream.event_stream"), \
         patch.object(lark_channel, "_api_post") as mock_api:
        mock_db.get_user_display_name.return_value = "unknown"
        mock_db.is_user_allowed.return_value = True
        mock_db.needs_name.return_value = False
        mock_db.get_or_create_session.return_value = "sess_1"
        mock_db.is_session_bot_enabled.return_value = True
        mock_runtime.handle_message.return_value = {"response": "Hi there"}
        mock_api.return_value = {"code": 0, "data": {}}

        result = lark_channel.handle_callback(payload)

        assert result == {"code": 0, "msg": "ok"}
        mock_db.set_user_display_name.assert_called_once_with(
            "ch_lark_1", "ou_123", "Alice Chen"
        )


def test_handle_callback_fetches_lark_profile_display_name(lark_channel):
    lark_channel._running = True
    lark_channel._token_cache.set("tok", 9999)
    payload = {
        "event": {
            "type": "im.message.receive_v1",
            "message": {
                "message_type": "text",
                "content": json.dumps({"text": "hello bot"}),
            },
            "sender": {
                "sender_id": {"open_id": "ou_123"},
                "sender_type": "user",
            },
        }
    }

    with patch("backend.agent_runtime.agent_runtime") as mock_runtime, \
         patch("models.db.db") as mock_db, \
         patch("backend.event_stream.event_stream"), \
         patch.object(lark_channel, "_fetch_lark_user_display_name") as mock_fetch, \
         patch.object(lark_channel, "_api_post") as mock_api:
        mock_db.get_user_display_name.return_value = "unknown"
        mock_db.is_user_allowed.return_value = True
        mock_db.needs_name.return_value = False
        mock_db.get_or_create_session.return_value = "sess_1"
        mock_db.is_session_bot_enabled.return_value = True
        mock_fetch.return_value = "Alice Chen"
        mock_runtime.handle_message.return_value = {"response": "Hi there"}
        mock_api.return_value = {"code": 0, "data": {}}

        result = lark_channel.handle_callback(payload)

        assert result == {"code": 0, "msg": "ok"}
        mock_fetch.assert_called_once_with("ou_123")
        mock_db.set_user_display_name.assert_called_once_with(
            "ch_lark_1", "ou_123", "Alice Chen"
        )


def test_do_send_strips_markdown_and_escapes(lark_channel):
    lark_channel._running = True
    lark_channel._token_cache.set("tok", 9999)
    with patch.object(lark_channel, "_api_post") as mock_api:
        mock_api.return_value = {"code": 0, "data": {}}
        lark_channel._do_send("user_open_id", "**bold** and `code`")
        call = mock_api.call_args
        assert call[0][0] == "/open-apis/im/v1/messages"
        payload = call.kwargs["json_payload"]
        content = json.loads(payload["content"])
        assert content["text"] == "bold and code"
        assert payload["receive_id"] == "user_open_id"


def test_do_send_splits_long_messages(lark_channel):
    lark_channel._running = True
    lark_channel._token_cache.set("tok", 9999)
    long_text = "A" * 12000
    with patch.object(lark_channel, "_api_post") as mock_api:
        mock_api.return_value = {"code": 0, "data": {}}
        lark_channel._do_send("user", long_text)
        assert mock_api.call_count == 2


def test_do_send_file_missing_file_returns_false(lark_channel):
    assert lark_channel._do_send_file("user", "/nonexistent/file.pdf") is False


def test_do_send_file_success(lark_channel):
    lark_channel._token_cache.set("tok", 9999)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
        tmp.write(b"hello")
        path = tmp.name
    try:
        with patch.object(lark_channel, "_send_message") as mock_send, \
             patch.object(lark_channel, "_api_post") as mock_api:
            mock_api.return_value = {"code": 0, "data": {"file_key": "fk_123"}}
            mock_send.return_value = {"code": 0, "data": {}}
            result = lark_channel._do_send_file("user", path, caption="see file")
            assert result is True
            mock_api.assert_called_once()
            assert mock_send.call_count == 2  # file + caption
    finally:
        os.unlink(path)


def test_verify_signature_without_encrypt_key():
    from backend.channels.lark import LarkChannel
    ch = LarkChannel("ch", "ag", {"app_id": "a", "app_secret": "s"})
    assert ch.verify_signature(b"body", "sig", "ts", "nonce") is True


def test_verify_signature_with_encrypt_key(lark_channel):
    timestamp = "1234567890"
    nonce = "abcdef"
    body = b'{"type":"url_verification"}'
    message = (timestamp + nonce + body.decode("utf-8")).encode("utf-8")
    expected = base64.b64encode(
        hmac.new(
            lark_channel._encrypt_key.encode("utf-8"),
            message,
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    assert lark_channel.verify_signature(body, expected, timestamp, nonce) is True
    assert lark_channel.verify_signature(body, "wrong", timestamp, nonce) is False


def test_handle_callback_url_verification(lark_channel):
    payload = {"type": "url_verification", "challenge": "chal_123"}
    result = lark_channel.handle_callback(payload)
    assert result == {"challenge": "chal_123"}


def test_handle_callback_ignores_unknown_events(lark_channel):
    payload = {"event": {"type": "some.other.event"}}
    result = lark_channel.handle_callback(payload)
    assert result == {"code": 0, "msg": "ok"}


def test_handle_callback_text_message_triggers_agent(lark_channel):
    lark_channel._running = True
    lark_channel._token_cache.set("tok", 9999)
    payload = {
        "event": {
            "type": "im.message.receive_v1",
            "message": {
                "message_type": "text",
                "content": json.dumps({"text": "hello bot"}),
            },
            "sender": {
                "sender_id": {"open_id": "ou_123"},
                "sender_type": "user",
            },
        }
    }

    with patch("backend.agent_runtime.agent_runtime") as mock_runtime, \
         patch("models.db.db") as mock_db, \
         patch("backend.event_stream.event_stream"), \
         patch.object(lark_channel, "_api_post") as mock_api:
        mock_db.is_user_allowed.return_value = True
        mock_db.needs_name.return_value = False
        mock_db.get_or_create_session.return_value = "sess_1"
        mock_db.is_session_bot_enabled.return_value = True
        mock_runtime.handle_message.return_value = {"response": "Hi there"}
        mock_api.return_value = {"code": 0, "data": {}}

        result = lark_channel.handle_callback(payload)

        assert result == {"code": 0, "msg": "ok"}
        mock_runtime.handle_message.assert_called_once_with(
            "agent_1", "ou_123", "hello bot", "ch_lark_1"
        )
        mock_api.assert_called()


def test_handle_callback_signature_invalid(lark_channel):
    payload = {
        "event": {
            "type": "im.message.receive_v1",
            "message": {"message_type": "text", "content": '{"text":"hi"}'},
            "sender": {"sender_id": {"open_id": "ou_123"}},
        }
    }
    headers = {
        "X-Lark-Signature": "bad-sig",
        "X-Lark-Request-Timestamp": "123",
        "X-Lark-Request-Nonce": "abc",
    }
    result = lark_channel.handle_callback(payload, headers=headers)
    assert result == {"code": 403, "msg": "invalid signature"}


def test_handle_callback_pairing_code_flow(lark_channel):
    lark_channel._running = True
    lark_channel._token_cache.set("tok", 9999)
    payload = {
        "event": {
            "type": "im.message.receive_v1",
            "message": {
                "message_type": "text",
                "content": json.dumps({"text": "pair code ABC-123"}),
            },
            "sender": {
                "sender_id": {"open_id": "ou_999"},
                "sender_type": "user",
            },
        }
    }

    with patch("models.db.db") as mock_db, \
         patch("backend.event_stream.event_stream"), \
         patch.object(lark_channel, "_api_post") as mock_api:
        mock_db.is_user_allowed.return_value = False
        mock_db.get_pending_approval_by_code.return_value = {
            "id": "pend_1",
            "external_user_id": "",
        }
        mock_db.approve_pending_with_name_needed.return_value = "ou_999"
        mock_db.needs_name.return_value = True
        mock_api.return_value = {"code": 0, "data": {}}

        result = lark_channel.handle_callback(payload)

        assert result == {"code": 0, "msg": "ok"}
        mock_db.approve_pending_with_name_needed.assert_called_once_with("pend_1")
        mock_db.update_pending_user_id.assert_called_once_with("pend_1", "ou_999")
