"""Link to the browser extension (browser_extension/): a WebSocket server on 127.0.0.1 that only it may use.

- Only this computer: the server listens on 127.0.0.1.
- Only our extension: the browser puts the extension's id into the Origin header, web pages cannot fake it.
- Mutual proof with the pairing key: Jarvis writes a random key into data/web_key and browser_extension/pairing.json
  (both outside git); each side answers the other's random challenge with an HMAC of it. Until then nothing is sent.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Callable

from assistant.paths import DATA_DIR, ROOT

log = logging.getLogger("web")

EXTENSION_ID = "ojccaehhgmcodmmaklilhcacibehfain"   # fixed by the "key" in browser_extension/manifest.json
EXTENSION_DIR = ROOT / "browser_extension"
KEY_FILE = DATA_DIR / "web_key"


class BridgeError(Exception):
    """The browser did not do it (no connection, a page error, a refusal)."""


def proof(key: str, text: str) -> str:
    return hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()


def ensure_key(key_file: Path = KEY_FILE, ext_dir: Path = EXTENSION_DIR) -> str:
    """The pairing key, created on first start and copied where the extension reads it."""
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    if len(key) < 32:
        key = secrets.token_hex(32)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key, encoding="utf-8")
    pairing = ext_dir / "pairing.json"
    try:
        current = json.loads(pairing.read_text(encoding="utf-8")).get("key")
    except (OSError, ValueError):
        current = None
    if current != key and ext_dir.is_dir():
        pairing.write_text(json.dumps({"key": key}), encoding="utf-8")
    return key


class Bridge:
    def __init__(self, port: int, key: str, extension_id: str = EXTENSION_ID) -> None:
        self.port = port
        self.key = key
        self.origin = f"chrome-extension://{extension_id}"
        self._conn = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._server = None
        self.version = ""
        self.on_event: Callable[[str, dict], None] = lambda _e, _d: None
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def start(self) -> None:
        from websockets.asyncio.server import serve

        logging.getLogger("websockets").setLevel(logging.WARNING)   # "connection open" on every reconnect is noise
        self._server = await serve(self._handle, "127.0.0.1", self.port, origins=[self.origin],
                                   max_size=16 * 1024 * 1024, ping_interval=None)
        log.info("Связь с браузером: ws://127.0.0.1:%d (ждёт расширение)", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def wait_connected(self, timeout: float) -> bool:
        if self.connected:
            return True
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.connected

    async def _handshake(self, conn) -> dict | None:
        hello = json.loads(await asyncio.wait_for(conn.recv(), 10))
        if hello.get("event") != "hello" or not hello.get("nonce"):
            return None
        mine = secrets.token_hex(16)
        await conn.send(json.dumps({"event": "welcome", "proof": proof(self.key, "jarvis:" + str(hello["nonce"])),
                                    "nonce": mine}))
        auth = json.loads(await asyncio.wait_for(conn.recv(), 10))
        if auth.get("event") != "auth" or not hmac.compare_digest(str(auth.get("proof", "")),
                                                                    proof(self.key, "ext:" + mine)):
            return None
        return hello

    async def _handle(self, conn) -> None:
        try:
            hello = await self._handshake(conn)
        except (asyncio.TimeoutError, ValueError, Exception) as exc:  # noqa: BLE001 - any failure: drop it
            log.warning("Расширение браузера не прошло проверку: %s", exc)
            return
        if hello is None:
            log.warning("Расширение браузера: неверный ключ связи (перезагрузите расширение)")
            return
        if self._conn is not None:
            # A second copy of the extension (another browser or profile) takes over; the old one is told so and
            # waits a minute instead of reconnecting at once and pushing this one out in turn.
            old = self._conn
            try:
                await old.send(json.dumps({"event": "replaced"}))
            except Exception:  # noqa: BLE001 - it may be gone already
                pass
            await old.close()
            log.info("Другая копия расширения заняла связь — старая отключена")
        self._conn = conn
        self.version = str(hello.get("version") or "")
        self._connected.set()
        log.info("Браузер подключён (расширение %s)", self.version)
        self.on_event("connected", hello)
        try:
            async for raw in conn:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_exception(BridgeError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result"))
                elif msg.get("event") not in (None, "ping"):
                    self.on_event(str(msg["event"]), msg)
        except Exception as exc:  # noqa: BLE001 - connection dropped
            log.debug("Связь с браузером оборвалась: %s", exc)
        finally:
            if self._conn is conn:
                self._conn = None
                self._connected.clear()
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(BridgeError("браузер отключился"))
                self._pending.clear()
                log.info("Браузер отключился")
                self.on_event("disconnected", {})

    def notify(self, event: str, **data: Any) -> None:
        """A one-way message to the extension (Jarvis's state for its icon); dropped when it is not connected."""
        conn = self._conn
        if conn is None:
            return

        async def send() -> None:
            try:
                await conn.send(json.dumps({"event": event, **data}, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 - the icon is not worth an error
                log.debug("Браузеру не отправлено %s: %s", event, exc)

        asyncio.get_running_loop().create_task(send())

    async def call(self, method: str, timeout: float = 40, **params: Any) -> Any:
        conn = self._conn
        if conn is None:
            raise BridgeError("расширение браузера не подключено")
        msg_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await conn.send(json.dumps({"id": msg_id, "method": method, "params": params}, ensure_ascii=False))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise BridgeError(f"браузер не ответил на «{method}»") from None
        finally:
            self._pending.pop(msg_id, None)
