"""WebSocket server.

Each connection gets its own Session. The agent loop is synchronous, so a turn
runs on a worker thread and pushes events back into the event loop; approval
requests cross the same bridge in the other direction.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import JarvisConfig
from ..errors import MissingDependency
from ..events import Event, TurnFinished
from ..session import Session

# Imported at module level, not inside create_app: `from __future__ import
# annotations` turns signatures into strings, and FastAPI can only resolve
# WebSocket from module globals.
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    HAVE_FASTAPI = True
except ImportError:  # the web extra is not installed
    HAVE_FASTAPI = False

STATIC = Path(__file__).parent / "static"
APPROVAL_TIMEOUT = 180.0


@dataclass
class Pending:
    """An approval request waiting on the browser."""

    event: threading.Event
    granted: bool = False


def _require_fastapi() -> None:
    if not HAVE_FASTAPI:  # pragma: no cover - depends on the install
        raise MissingDependency("The web UI", "web", "fastapi")
    try:
        import uvicorn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise MissingDependency("The web UI", "web", "uvicorn") from exc


def create_app(config: JarvisConfig, session_factory: Any = None) -> Any:
    """Build the FastAPI app.

    `session_factory` exists so tests can drive the whole stack with a faked
    API client; by default every connection gets a fresh Session.
    """
    _require_fastapi()
    make_session = session_factory or (lambda: Session(config, interface="web"))

    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index() -> Any:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "model": config.model,
            "workspace": str(config.workspace),
            "approval": config.approval,
        }

    @app.websocket("/ws")
    async def socket(websocket: WebSocket) -> None:
        await websocket.accept()
        loop = asyncio.get_running_loop()
        outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        pending: dict[str, Pending] = {}
        session = make_session()
        turn: asyncio.Task[None] | None = None

        def send(payload: dict[str, Any]) -> None:
            """Thread-safe push toward the browser."""
            loop.call_soon_threadsafe(outbox.put_nowait, payload)

        def send_event(event: Event) -> None:
            send(event.to_dict())

        def confirm(action: str, detail: str) -> bool:
            request_id = uuid.uuid4().hex[:8]
            waiting = Pending(event=threading.Event())
            pending[request_id] = waiting
            send({"kind": "approval_request", "id": request_id, "action": action, "detail": detail})
            answered = waiting.event.wait(timeout=APPROVAL_TIMEOUT)
            pending.pop(request_id, None)
            if not answered:
                # Timed out into the safe default: do nothing, and leave a note
                # rather than silently treating absence as a refusal.
                session.context.notify(
                    f"unanswered: wanted to {action}",
                    f"{detail}\n\nNot done - the request went unanswered for "
                    f"{int(APPROVAL_TIMEOUT)}s and timed out.",
                )
                return False
            return waiting.granted

        session.context.confirm = confirm
        session.start_heartbeat(send_event)

        async def pump() -> None:
            while True:
                payload = await outbox.get()
                await websocket.send_json(payload)

        def run_turn(text: str) -> None:
            try:
                for event in session.send(text):
                    send_event(event)
            except Exception as exc:  # never leave the browser hanging
                send({
                    "kind": "error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "recoverable": False,
                })
                send(TurnFinished(text="").to_dict())

        pumper = asyncio.create_task(pump())
        send({
            "kind": "ready",
            "model": config.model,
            "workspace": str(config.workspace),
            "approval": config.approval,
            "tools": session.registry.names(),
            "paused": session.paused,
            "waiting": len(session.open_notices(limit=100)),
        })
        # Whatever was raised while no browser was attached has been held for
        # exactly this moment. Flagged, because arriving in a batch on connect is
        # not the same as interrupting: the browser shows these without also
        # firing a toast for each one.
        for held in session.catch_up():
            send({**held.to_dict(), "caught_up": True})

        try:
            while True:
                message = await websocket.receive_json()
                kind = message.get("type")

                if kind == "message":
                    text = (message.get("text") or "").strip()
                    if not text:
                        continue
                    if turn and not turn.done():
                        send({
                            "kind": "notice",
                            "message": "still working on the last one",
                            "level": "warn",
                        })
                        continue
                    turn = asyncio.create_task(asyncio.to_thread(run_turn, text))
                elif kind == "interrupt":
                    session.interrupt()
                elif kind == "approval":
                    waiting = pending.get(message.get("id", ""))
                    if waiting:
                        waiting.granted = bool(message.get("allow"))
                        waiting.event.set()
                elif kind == "reset":
                    session.reset()
                    send({"kind": "notice", "message": "fresh conversation", "level": "info"})
                elif kind == "dismiss":
                    target = message.get("id")
                    if target == "all":
                        cleared = session.store.dismiss_all_notices()
                        send({"kind": "dismissed", "id": "all", "count": cleared})
                    elif target is not None:
                        ok = session.store.dismiss_notice(int(target))
                        send({"kind": "dismissed", "id": int(target), "count": int(ok)})
                elif kind == "pause":
                    session.set_paused(bool(message.get("paused", True)))
                    send({"kind": "paused", "paused": session.paused})
                elif kind == "notices":
                    send({
                        "kind": "notices",
                        "notices": [
                            {
                                "id": notice.id,
                                "text": notice.text,
                                "detail": notice.detail,
                                "level": notice.level,
                                "source": notice.source,
                                "created_at": notice.created_at,
                            }
                            for notice in session.open_notices(limit=100)
                        ],
                    })
        except WebSocketDisconnect:
            pass
        finally:
            session.interrupt()
            for waiting in pending.values():  # unblock anything still waiting
                waiting.event.set()
            pumper.cancel()
            if turn and not turn.done():
                turn.cancel()
            await asyncio.to_thread(session.close)

    return app


def serve(
    config: JarvisConfig,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool = False,
) -> int:
    """Run the web UI until interrupted."""
    _require_fastapi()
    import uvicorn

    host = host or config.web_host
    port = port or config.web_port
    app = create_app(config)

    url = f"http://{host}:{port}"
    print(f"jarvis is listening on {url}  (workspace: {config.workspace})")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
