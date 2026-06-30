"""Lark / Feishu channel implementation using the open API.

Lark and Feishu share the same API surface; this adapter works for both by
allowing the caller to configure ``base_url`` (defaults to the international
Lark endpoint). Messages are received via webhook and replies are sent through
the Bot IM API.
"""

import asyncio
import base64
import hmac
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Optional

import requests
import websockets

from backend.channels.base import BaseChannel, strip_system_tags

_logger = logging.getLogger(__name__)

_LARK_DEFAULT_BASE_URL = "https://open.larksuite.com"
# Feishu (China) endpoint; exposed so documentation and tests can reference it.
# The actual endpoint is selected via the per-channel ``base_url`` config.
_LARK_CHINA_BASE_URL = "https://open.feishu.cn"

# Lark text messages support a small subset of Markdown. Keep responses plain
# text to avoid rendering quirks between Lark clients.
_LARK_MSG_MAX_LEN = 10000


_SEEN_MSG_TTL = 120  # seconds to remember processed message IDs


class _SeenMessageIds:
    """Thread-safe dedup cache for Lark webhook message IDs.

    Lark may retry a webhook POST if the server doesn't respond within ~3 s.
    Tracking the last-seen message_id prevents the duplicate delivery from
    being processed twice and causing a double-reply to the user.
    """

    def __init__(self, ttl: int = _SEEN_MSG_TTL):
        self._lock = threading.Lock()
        self._seen: dict = {}  # message_id -> expire_at
        self._ttl = ttl

    def check_and_set(self, message_id: str) -> bool:
        """Return True (duplicate — skip) if already seen; otherwise record and return False."""
        now = time.time()
        with self._lock:
            self._purge(now)
            if message_id in self._seen:
                return True
            self._seen[message_id] = now + self._ttl
            return False

    def _purge(self, now: float) -> None:
        expired = [k for k, v in self._seen.items() if v <= now]
        for k in expired:
            del self._seen[k]


class _LarkTokenCache:
    """Thread-safe cache for Lark tenant access tokens."""

    def __init__(self):
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def get(self) -> Optional[str]:
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            return None

    def set(self, token: str, expires_in: int) -> None:
        with self._lock:
            self._token = token
            self._expires_at = time.time() + expires_in

    def clear(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0


def _strip_markdown(text: str) -> str:
    """Remove markdown symbols that render poorly in plain Lark text."""
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'`+', '', text)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    return text.strip()


def _split_message(text: str, max_len: int = _LARK_MSG_MAX_LEN) -> list:
    """Split text into chunks that fit within Lark's message size limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind('\n\n', 0, max_len)
        if split_at < max_len // 2:
            split_at = text.rfind(' ', 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return chunks


def _escape_text_for_lark(text: str) -> str:
    """Escape characters that have special meaning in Lark text messages.

    Lark text messages render a subset of Markdown; escape sequences that would
    otherwise turn into unintended formatting.
    """
    return text.replace('*', '\\*').replace('`', '\\`')


def _websockets_connect_kwargs() -> dict:
    params = inspect.signature(websockets.connect).parameters
    if "proxy" in params:
        return {"proxy": None}
    return {}


class LarkChannel(BaseChannel):
    def __init__(self, channel_id: str, agent_id: str, config: Dict[str, Any]):
        super().__init__(channel_id, agent_id, config)
        self._base_url = (config.get('base_url') or _LARK_DEFAULT_BASE_URL).rstrip('/')
        self._app_id = config.get('app_id', '')
        self._app_secret = config.get('app_secret', '')
        self._encrypt_key = config.get('encrypt_key', '')
        self._receive_mode = config.get('receive_mode', 'webhook')
        self._token_cache = _LarkTokenCache()
        self._seen_msg_ids = _SeenMessageIds()
        self._ws_thread: Optional[threading.Thread] = None

    @staticmethod
    def get_channel_type() -> str:
        return 'lark'

    def get_system_instructions(self) -> Optional[str]:
        return (
            "IMPORTANT — Lark / Feishu Formatting Constraint:\n"
            "You are responding via Lark which uses PLAIN TEXT only. "
            "Markdown formatting (bold, italic, code blocks, headers, bullet lists, "
            "blockquotes, inline code, links) is NOT supported and will appear as "
            "raw symbols, making your response unreadable.\n\n"
            "STRICTLY FOLLOW THESE RULES:\n"
            "- NEVER use markdown symbols: **, *, `, ```, #, -, >, [], ()\n"
            "- Use UPPERCASE for emphasis instead of bold/italic\n"
            "- Use numbered lists (1. 2. 3.) for lists\n"
            "- Use indentation with spaces for structure\n"
            "- Use plain URLs without markdown link syntax\n"
            "- Write code inline with clear labels like \"CODE:\" prefix\n"
            "- Keep responses clean and readable in plain text"
        )

    def start(self):
        _logger.info("Lark channel %s starting (agent: %s, mode: %s)...",
                     self.channel_id, self.agent_id, self._receive_mode)
        if not self._app_id or not self._app_secret:
            _logger.error("Lark channel %s: app_id and app_secret are required", self.channel_id)
            raise ValueError("app_id and app_secret are required for Lark channel.")
        # Validate credentials by fetching a token once.
        try:
            self._ensure_token()
            self._running = True
        except Exception as e:
            _logger.error("Lark channel %s: failed to fetch access token: %s", self.channel_id, e)
            raise

        if self._receive_mode in ('stream', 'ws'):
            self._start_ws()
        _logger.info("Lark channel %s started", self.channel_id)

    def stop(self):
        if not self._running:
            return
        _logger.info("Lark channel %s stopping...", self.channel_id)
        self._running = False
        if self._receive_mode in ('stream', 'ws'):
            self._stop_ws()
        self._token_cache.clear()
        _logger.info("Lark channel %s stopped", self.channel_id)


    def _ensure_token(self) -> str:
        """Return a valid tenant access token, fetching a new one if needed."""
        token = self._token_cache.get()
        if token:
            return token

        url = f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal"
        try:
            resp = requests.post(
                url,
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise RuntimeError(f"Lark token request failed: {e}") from e

        if data.get("code") != 0:
            raise RuntimeError(
                f"Lark token request failed: {data.get('msg')} (code={data.get('code')})"
            )

        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError("Lark token response missing tenant_access_token")

        expires_in = int(data.get("expire", 7200))
        self._token_cache.set(token, expires_in)
        return token

    def _start_ws(self):
        """Start the WebSocket client in a daemon thread."""
        _logger.info("Lark WS mode: connecting to %s", self._base_url)
        self._ws_thread = threading.Thread(
            target=self._ws_loop, daemon=True,
            name=f"lark-ws-{self.channel_id}",
        )
        self._ws_thread.start()

    def _stop_ws(self):
        """Signal the WS thread to exit and wait for it."""
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)

    def _extract_ws_url(self, ws_resp: dict) -> str:
        data = ws_resp.get("data") or {}
        ws_url = data.get("URL") or data.get("url")
        if not ws_url:
            raise RuntimeError(
                "Lark WS endpoint discovery: response missing 'data.URL'"
            )
        return ws_url

    def _ws_loop(self):
        """Background WebSocket loop using SDK-based endpoint discovery
        and protobuf frame parsing. Handles reconnect with backoff."""
        from lark_oapi.ws.pb.pbbp2_pb2 import Frame
        from lark_oapi.ws.enum import FrameType
        from lark_oapi.ws.const import HEADER_TYPE, HEADER_MESSAGE_ID

        backoff = 1
        while self._running:
            try:
                # Step 1: discover WebSocket endpoint
                resp = requests.post(
                    f"{self._base_url}/callback/ws/endpoint",
                    json={"AppID": self._app_id, "AppSecret": self._app_secret},
                    timeout=30,
                )
                resp.raise_for_status()
                ws_resp = resp.json()
                if ws_resp.get("code") != 0:
                    raise RuntimeError(
                        f"Lark WS endpoint discovery failed: {ws_resp.get('msg')} "
                        f"(code={ws_resp.get('code')})"
                    )
                ws_url = self._extract_ws_url(ws_resp)
                backoff = 1
                _logger.info("Lark WS: connected")

                # Step 2: create event loop for this thread and connect
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                async def _listen():
                    async with websockets.connect(
                        ws_url, **_websockets_connect_kwargs()
                    ) as ws:
                        while self._running:
                            msg = await ws.recv()
                            frame = Frame()
                            frame.ParseFromString(msg)
                            if frame.method != FrameType.DATA.value:
                                continue

                            # Determine message type from headers
                            msg_type = None
                            for h in frame.headers:
                                if h.key == HEADER_TYPE:
                                    msg_type = h.value
                                    break
                            if msg_type != "event":
                                continue

                            try:
                                pl = frame.payload.decode("utf-8")
                                data = json.loads(pl)
                            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                                _logger.warning("Lark WS: unparseable event: %s", e)
                                continue

                            # Build payload for handle_callback
                            cb_payload = {"event": data.get("event", {})}
                            if "header" in data:
                                cb_payload["header"] = data["header"]
                            self.handle_callback(cb_payload)

                loop.run_until_complete(_listen())

            except Exception as e:
                if self._running:
                    _logger.warning("Lark WS: disconnected (%s), retrying in %ds",
                                    e, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
        _logger.info("Lark WS: thread exiting")

    def _api_post(self, path: str, json_payload: Optional[dict] = None,
                  params: Optional[dict] = None, files: Optional[dict] = None,
                  data: Optional[dict] = None) -> dict:
        """Make an authenticated POST to the Lark API."""
        token = self._ensure_token()
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        if json_payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        def _do_post():
            return requests.post(
                url,
                headers=headers,
                json=json_payload,
                params=params,
                files=files,
                data=data,
                timeout=60,
            )

        resp = None
        try:
            resp = _do_post()
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            # Token may have been revoked; clear cache and retry once.
            if resp is not None and resp.status_code == 401:
                self._token_cache.clear()
                headers["Authorization"] = f"Bearer {self._ensure_token()}"
                try:
                    resp = _do_post()
                    resp.raise_for_status()
                    return resp.json()
                except requests.RequestException as e2:
                    raise RuntimeError(f"Lark API POST {path} failed after retry: {e2}") from e2
            raise RuntimeError(f"Lark API POST {path} failed: {e}") from e

    def _api_get(self, path: str, params: Optional[dict] = None) -> dict:
        """Make an authenticated GET to the Lark API."""
        token = self._ensure_token()
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise RuntimeError(f"Lark API GET {path} failed: {e}") from e

    def _extract_sender_display_name(self, event: dict) -> Optional[str]:
        """Extract a human display name when Lark includes it in the event."""
        sender = event.get("sender") or {}
        candidates = [
            sender.get("name"),
            sender.get("display_name"),
            sender.get("sender_name"),
            sender.get("user_name"),
        ]
        sender_user = sender.get("user") or {}
        candidates.extend([
            sender_user.get("name"),
            sender_user.get("en_name"),
            sender_user.get("nickname"),
        ])
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    def _fetch_lark_user_display_name(self, open_id: str) -> Optional[str]:
        """Fetch a user's display name from Lark Contact API by open_id."""
        if not open_id:
            return None
        result = self._api_get(
            f"/open-apis/contact/v3/users/{open_id}",
            params={"user_id_type": "open_id"},
        )
        if result.get("code") != 0:
            _logger.warning(
                "Lark user profile lookup failed for %s: %s (code=%s)",
                open_id, result.get("msg"), result.get("code"),
            )
            return None

        user = (result.get("data") or {}).get("user") or {}
        for key in ("name", "en_name", "nickname"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _ensure_user_display_name(self, db, event: dict, user_id: str) -> Optional[str]:
        """Persist the current Lark user's display name when available."""
        try:
            current_name = db.get_user_display_name(self.channel_id, user_id)
        except Exception:
            _logger.warning(
                "Lark channel %s: failed to read display name for %s",
                self.channel_id, user_id, exc_info=True,
            )
            return None

        if current_name and current_name not in ("unknown", user_id):
            return current_name

        display_name = self._extract_sender_display_name(event)
        if not display_name:
            try:
                display_name = self._fetch_lark_user_display_name(user_id)
            except Exception as e:
                _logger.warning(
                    "Lark channel %s: user profile lookup skipped for %s: %s",
                    self.channel_id, user_id, e,
                )

        if display_name:
            db.set_user_display_name(self.channel_id, user_id, display_name)
            return display_name
        return None

    def _send_message(self, receive_id: str, msg_type: str, content: dict) -> dict:
        """Send a message to a Lark user."""
        content_str = json.dumps(content, ensure_ascii=False)
        return self._api_post(
            "/open-apis/im/v1/messages",
            json_payload={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": content_str,
            },
            params={"receive_id_type": "open_id"},
        )

    def _do_send(self, external_user_id: str, text: str):
        text = _strip_markdown(text)
        for chunk in _split_message(text):
            content = {"text": _escape_text_for_lark(chunk)}
            self._send_message(external_user_id, "text", content)

        from backend.event_stream import event_stream
        event_stream.emit('message_sent', {
            'channel_type': 'lark',
            'channel_id': self.channel_id,
            'external_user_id': external_user_id,
            'message': text,
        })

    def _do_send_file(self, external_user_id: str, file_path: str,
                      caption: Optional[str] = None,
                      mime_type: Optional[str] = None) -> bool:
        if not os.path.isfile(file_path):
            _logger.error("File not found for sending: %s", file_path)
            return False
        if not os.access(file_path, os.R_OK):
            _logger.error("File not readable: %s", file_path)
            return False

        filename = os.path.basename(file_path)
        file_type = "stream"
        if mime_type:
            if mime_type.startswith("image/"):
                file_type = "image"
            elif mime_type.startswith("video/"):
                file_type = "video"
            elif mime_type.startswith("audio/"):
                file_type = "audio"

        try:
            with open(file_path, "rb") as fh:
                files = {"file": (filename, fh)}
                data = {"file_type": file_type, "file_name": filename}
                result = self._api_post(
                    "/open-apis/im/v1/files",
                    files=files,
                    data=data,
                )
        except Exception as e:
            _logger.error("Failed to upload file %s to Lark: %s", file_path, e, exc_info=True)
            return False

        file_key = (result.get("data") or {}).get("file_key")
        if not file_key:
            _logger.error("Lark file upload response missing file_key: %s", result)
            return False

        try:
            self._send_message(external_user_id, "file", {"file_key": file_key})
            if caption:
                self._do_send(external_user_id, caption)
        except Exception as e:
            _logger.error("Failed to send file %s to %s: %s", file_path, external_user_id, e)
            return False

        from backend.event_stream import event_stream
        event_stream.emit('message_sent', {
            'channel_type': 'lark',
            'channel_id': self.channel_id,
            'external_user_id': external_user_id,
            'message': f"[FILE] {filename}" + (" (with caption)" if caption else ""),
        })
        _logger.info("Sent file %s to %s via Lark", filename, external_user_id)
        return True

    def verify_signature(self, body: bytes, signature: str, timestamp: str, nonce: str) -> bool:
        """Verify the signature of a Lark webhook request.

        If no encrypt_key is configured, verification is skipped and the
        request is accepted. This is less secure but convenient for local
        testing; set encrypt_key in production.
        """
        if not self._encrypt_key:
            return True
        if not signature or not timestamp or not nonce:
            return False
        message = (timestamp + nonce + body.decode('utf-8')).encode('utf-8')
        digest = hmac.new(
            self._encrypt_key.encode('utf-8'),
            message,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode('utf-8')
        return hmac.compare_digest(expected, signature)

    def handle_callback(self, payload: dict, headers: Optional[Dict[str, str]] = None,
                        raw_body: Optional[bytes] = None):
        """Process incoming webhook POSTed by Lark.

        ``payload`` is the parsed JSON body. ``headers`` may contain the
        signature-related headers used when encrypt_key is configured.
        ``raw_body`` is the original request body bytes; it is required for
        signature verification because the signature is computed over the
        exact bytes Lark sent.
        """
        from backend.agent_runtime import agent_runtime
        from models.db import db

        # URL verification handshake. Lark sends this when the callback URL is
        # first configured in the app console.
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}

        # Validate signature when encrypt_key is configured.
        if headers and self._encrypt_key:
            signature = headers.get("X-Lark-Signature") or headers.get("x-lark-signature", "")
            timestamp = headers.get("X-Lark-Request-Timestamp") or headers.get("x-lark-request-timestamp", "")
            nonce = headers.get("X-Lark-Request-Nonce") or headers.get("x-lark-request-nonce", "")
            body_bytes = raw_body if raw_body is not None else json.dumps(
                payload, ensure_ascii=False, separators=(',', ':')
            ).encode('utf-8')
            if not self.verify_signature(body_bytes, signature, timestamp, nonce):
                _logger.warning("Lark channel %s: invalid webhook signature", self.channel_id)
                return {"code": 403, "msg": "invalid signature"}

        # Support both v1 (event["type"]) and v2 (header["event_type"]) formats
        event_type = (
            (payload.get("event") or {}).get("type")
            or payload.get("header", {}).get("event_type")
        )
        if event_type != "im.message.receive_v1":
            return {"code": 0, "msg": "ok"}
        event = payload.get("event") or {}

        message = event.get("message") or {}
        msg_id = message.get("message_id", "")
        if msg_id and self._seen_msg_ids.check_and_set(msg_id):
            _logger.debug("Lark channel %s: duplicate message_id %s — skipping", self.channel_id, msg_id)
            return {"code": 0, "msg": "ok"}

        sender_obj = event.get("sender") or {}
        if sender_obj.get("sender_type") != "user":
            return {"code": 0, "msg": "ok"}

        sender = sender_obj.get("sender_id", {})
        user_id = sender.get("open_id") or sender.get("user_id") or sender.get("union_id")
        if not user_id:
            _logger.warning("Lark channel %s: received message without sender id", self.channel_id)
            return {"code": 0, "msg": "ok"}

        display_name = self._ensure_user_display_name(db, event, user_id)
        user_name = display_name or sender_obj.get("sender_type", "")

        # Parse text content. Lark sends content as a JSON string.
        content_raw = message.get("content", "{}")
        try:
            content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
        except json.JSONDecodeError:
            content = {}

        msg_type = message.get("message_type", "")
        text = ""
        if msg_type == "text":
            text = strip_system_tags(content.get("text", ""))
        elif msg_type == "file":
            text = "[File received]"
        elif msg_type == "image":
            text = "[Image received]"
        elif msg_type == "audio":
            text = "[Audio received]"
        elif msg_type == "video":
            text = "[Video received]"
        else:
            text = f"[{msg_type or 'Message'} received]"

        # Allowlist / pairing flow.
        if db.is_user_allowed(self.channel_id, user_id):
            if db.needs_name(self.channel_id, user_id):
                name_candidate = text.strip() if text else ""
                if name_candidate and len(name_candidate) <= 100:
                    db.set_user_display_name(self.channel_id, user_id, name_candidate)
                    self._do_send(user_id,
                        f"Thanks, {name_candidate}! You're all set. How can I help you today?")
                elif text:
                    self._do_send(user_id,
                        "That name is too long. Please share a shorter name (max 100 characters).")
                else:
                    self._do_send(user_id,
                        "Please tell me your name to continue (e.g. 'My name is Budi').")
                return {"code": 0, "msg": "ok"}
        else:
            from backend.channels.pairing import extract_pair_code
            raw_code = extract_pair_code(text) if text else None
            if raw_code:
                pending = db.get_pending_approval_by_code(raw_code)
                if pending:
                    if not pending.get('external_user_id'):
                        db.update_pending_user_id(pending['id'], user_id)
                    approved_user = db.approve_pending_with_name_needed(pending['id'])
                    if approved_user:
                        if db.needs_name(self.channel_id, user_id):
                            self._do_send(user_id,
                                "✅ You're now approved! Welcome aboard.\n\n"
                                "Before we chat, please tell me your name (e.g. 'My name is Budi').")
                        else:
                            self._do_send(user_id,
                                "✅ You're now approved! Welcome aboard. How can I help you today?")
                    return {"code": 0, "msg": "ok"}
                else:
                    self._do_send(user_id,
                        "❌ That pairing code is invalid or has expired. "
                        "Please ask the administrator for a new one.")
                    return {"code": 0, "msg": "ok"}
            else:
                existing = db.get_pending_approvals(self.channel_id)
                already_pending = any(
                    p.get('external_user_id') == user_id for p in existing
                )
                if not already_pending:
                    allowed, pair_code = self._check_allowlist(user_id, user_name)
                    if not allowed and pair_code:
                        self._do_send(user_id,
                            "👋 You're not yet approved to chat here. "
                            "Please ask the administrator for a pairing code, then send it in this chat.")
                return {"code": 0, "msg": "ok"}

        session_id = db.get_or_create_session(self.agent_id, user_id, self.channel_id)
        if not db.is_session_bot_enabled(session_id, agent_id=self.agent_id):
            db.add_chat_message(session_id, 'user', text, agent_id=self.agent_id)
            return {"code": 0, "msg": "ok"}

        _logger.info("Lark message received from %s (channel %s)", user_id, self.channel_id)
        result = agent_runtime.handle_message(
            self.agent_id, user_id, text, self.channel_id,
        )
        if result.get('buffered'):
            return {"code": 0, "msg": "ok"}

        response = _strip_markdown(result.get('response') or '')
        if response and response != "(No response)":
            for chunk in _split_message(response):
                self._do_send(user_id, chunk)

        return {"code": 0, "msg": "ok"}
