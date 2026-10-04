"""HTTP layer: customer chat page + JSON API. One process, no build step.

POST /api/customer/{id}/message accepts text and/or an audio file, stores
the message and returns {message_id} immediately; the pipeline worker
processes it in the background. Poll GET /api/state for everything fresh.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import bill as bill_mod
from app import clock
from app import engine
from app import owner as owner_mod
from app import pipeline
from app import tracking
from app import warmup
from app.ai import briefing as briefing_mod
from app.db import get_conn

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = ROOT / "data" / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Background loops: briefing pre-warm + supervisor sweep (30s/5s demo)."""
    import asyncio

    from app import supervisor as _super
    from app import warmup

    if os.environ.get("MUNSHI_SCHEDULER", "1") == "1":
        briefing_mod.start_scheduler()
    asyncio.create_task(warmup.run(), name="munshi-warmup")
    _super.start_loop()
    yield


app = FastAPI(title="Munshi", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "app" / "templates")


def _customer_or_404(conn, customer_id: int):
    """Customer row or HTTP 404."""
    row = conn.execute(
        "SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"No customer #{customer_id}.")
    return row


@app.post("/api/customer/{customer_id}/message")
async def post_message(customer_id: int, text: str = Form(None),
                       audio: UploadFile = File(None)):
    """Store an inbound message, enqueue it, reply at once with its id."""
    if not (text or "").strip() and audio is None:
        raise HTTPException(400, "Send text, an audio file, or both.")
    conn = get_conn()
    try:
        _customer_or_404(conn, customer_id)
        audio_path = None
        if audio is not None:
            content = await audio.read()
            if content:
                safe = Path(audio.filename or "note.webm").name
                dest = UPLOADS / f"{int(time.time())}_{safe}"
                dest.write_bytes(content)
                audio_path = str(dest)
            elif not (text or "").strip():
                raise HTTPException(400, "The audio recording is empty.")
        cur = conn.execute(
            "INSERT INTO messages (customer_id, order_id, direction, text,"
            " audio_path, ts) VALUES (?,?, 'in', ?, ?, ?)",
            (customer_id, None, (text or "").strip(), audio_path,
             clock.now().isoformat()),
        )
        message_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    depth = pipeline.enqueue(message_id)
    return {"message_id": message_id, "queued_behind": depth}


@app.get("/api/state")
def get_state(customer_id: int | None = None):
    """Chat page (with customer_id) or owner overview (without)."""
    if customer_id is None:
        feed = owner_mod.notifications()
        return {"orders_by_status": _status_counts(),
                "needs_you": len(owner_mod.board()["needs_you"]),
                "notifications": feed["unread"],
                "activity": None, "queue_depth": pipeline.queue_depth()}
    conn = get_conn()
    try:
        _customer_or_404(conn, customer_id)
        msgs = [dict(r) for r in conn.execute(
            "SELECT m.id, m.order_id, m.direction, m.text, m.ts"
            " FROM messages m LEFT JOIN orders o ON o.id = m.order_id"
            " WHERE m.customer_id = ?"
            " AND COALESCE(o.transcript, '') NOT LIKE '[DEMO HISTORY]%'"
            " ORDER BY m.id", (customer_id,))]
        orders = []
        for o in conn.execute(
                "SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC",
                (customer_id,)):
            lines = [dict(r) for r in conn.execute(
                "SELECT ol.qty, ol.unit_price, i.name, i.unit FROM order_lines ol"
                " JOIN items i ON i.id = ol.item_id WHERE ol.order_id = ?"
                " ORDER BY ol.id", (o["id"],))]
            try:
                bill = bill_mod.build_bill(o["id"])
            except Exception:
                bill = None
            orders.append({"id": o["id"], "status": o["status"],
                           "total": o["total"], "updated_at": o["updated_at"],
                           "lines": lines, "bill": bill,
                           "tracking_url": tracking.link(o["track_token"])})
        notif = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications n JOIN orders o"
            " ON o.id = n.order_id WHERE o.customer_id = ? AND n.done = 0",
            (customer_id,)).fetchone()["n"]
        return {"messages": msgs, "orders": orders,
                "notifications": notif,
                "activity": pipeline.inflight_activity(customer_id),
                "queue_depth": pipeline.queue_depth()}
    finally:
        conn.close()


@app.get("/api/health")
def get_health():
    """Ollama reachable? Which models? Is the agent loop on?"""
    host = os.environ.get("MUNSHI_LLM_HOST", "http://localhost:11434")
    reachable = False
    try:
        with httpx.Client(timeout=3) as client:
            reachable = client.get(f"{host}/api/tags").status_code == 200
    except httpx.HTTPError:
        pass
    return {
        "ollama": reachable,
        "models": {
            "llm": os.environ.get("MUNSHI_LLM_MODEL", "gemma4:e4b"),
            "stt_backend": os.environ.get("MUNSHI_STT_BACKEND", "mlx_whisper"),
        },
        "agent": os.environ.get("MUNSHI_AGENT", "1") == "1",
        "mock_ai": os.environ.get("MUNSHI_MOCK_AI", "0") == "1",
        "warmup": warmup.get_status(),
    }


@app.get("/customer/{customer_id}", response_class=HTMLResponse)
def customer_page(customer_id: int, request: Request):
    """WhatsApp-style chat page (server-rendered shell, JS polls state)."""
    conn = get_conn()
    try:
        me = _customer_or_404(conn, customer_id)
        customers = [dict(r) for r in conn.execute(
            "SELECT id, name FROM customers ORDER BY id")]
        return templates.TemplateResponse(
            request, "customer.html", {"me": dict(me), "customers": customers},
        )
    finally:
        conn.close()


def _status_counts() -> dict:
    """Live order counts per status (owner board polling)."""
    conn = get_conn()
    try:
        return {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}
    finally:
        conn.close()


def _owner_id(conn) -> int:
    """First staffer with the owner role (acts in approve/decline events)."""
    row = conn.execute(
        "SELECT id FROM staff WHERE role = 'owner' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        raise HTTPException(409, "No owner on staff.")
    return int(row["id"])


@app.get("/owner", response_class=HTMLResponse)
def owner_page(request: Request):
    """Owner portal shell; every tab fills in over fetch (polls every 2s)."""
    return templates.TemplateResponse(request, "owner.html", {})


@app.get("/api/owner/board")
def owner_board():
    """Orders grouped by status + the needs-you strip."""
    return owner_mod.board()


@app.get("/api/owner/order/{order_id}")
def owner_order(order_id: int):
    """Transcript, lines, bill and the reasoning timeline for one order."""
    try:
        return owner_mod.order_detail(order_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/owner/order/{order_id}/reassign")
def owner_reassign(order_id: int):
    """Owner moves a packing order to another packer."""
    try:
        packer_id = engine.reassign_packer(order_id)
    except engine.EngineError as e:
        raise HTTPException(409, str(e))
    return {"status": engine.PACKING, "packer_id": packer_id}


@app.post("/api/owner/order/{order_id}/approve")
def owner_approve(order_id: int):
    """Approve a waiting credit order (same path as auto-confirm)."""
    conn = get_conn()
    try:
        oid = _owner_id(conn)
    finally:
        conn.close()
    try:
        pipeline.owner_approve(order_id, oid)
    except engine.EngineError as e:
        raise HTTPException(409, str(e))
    conn = get_conn()
    try:
        live = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        return {"status": live["status"]}
    finally:
        conn.close()


@app.post("/api/owner/order/{order_id}/decline")
async def owner_decline(order_id: int, request: Request):
    """Decline a waiting credit order; optional {"reason": ...} JSON body."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    conn = get_conn()
    try:
        oid = _owner_id(conn)
    finally:
        conn.close()
    try:
        return {"status": pipeline.owner_decline(
            order_id, oid, (body or {}).get("reason", ""))}
    except engine.EngineError as e:
        raise HTTPException(409, str(e))


@app.post("/api/owner/order/{order_id}/mismatch")
def owner_mismatch(order_id: int, payload: dict):
    """Resolve a pack mismatch: accept_partial | recount | cancel."""
    conn = get_conn()
    try:
        oid = _owner_id(conn)
    finally:
        conn.close()
    try:
        return {"status": engine.owner_resolve_mismatch(
            order_id, payload.get("mode", ""), oid)}
    except engine.EngineError as e:
        raise HTTPException(409, str(e))


@app.get("/api/owner/khata")
def owner_khata():
    """Outstanding vs limit per customer + last order date."""
    return owner_mod.khata()


@app.get("/api/owner/stock")
def owner_stock():
    """Items with days-of-stock-left from the last 14 days of usage."""
    return owner_mod.stock_report()


@app.get("/api/owner/rules")
def owner_rules_get():
    """Raw rules.yaml text for the editable textarea."""
    return {"text": owner_mod.rules_text()}


@app.post("/api/owner/rules")
def owner_rules_post(payload: dict):
    """Validate + save rules.yaml (takes effect immediately) or 400."""
    try:
        return owner_mod.save_rules(payload.get("text", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/owner/notifications")
def owner_notifications():
    """Inbox feed + bell counter."""
    return owner_mod.notifications()


@app.post("/api/owner/notifications/{notification_id}/done")
def owner_notification_done(notification_id: int):
    """Clear one notification."""
    try:
        owner_mod.mark_done(notification_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"done": True}


@app.get("/api/owner/health_extra")
def owner_health_extra():
    """STT/LLM config + last order's seconds per stage (health strip)."""
    import os

    return {
        "stt": {"backend": os.environ.get("MUNSHI_STT_BACKEND", "mlx_whisper"),
                "model": os.environ.get(
                    "MUNSHI_STT_MODEL",
                    "mlx-community/whisper-large-v3-turbo")},
        "last_order": owner_mod.last_order_stages(),
    }


@app.get("/api/owner/briefing")
def owner_briefing():
    """Morning briefing JSON: summary on top, fact cards below."""
    return briefing_mod.briefing()


@app.get("/owner/briefing", response_class=HTMLResponse)
def briefing_page(request: Request):
    """Morning briefing page (summary + cards, fetched as JSON)."""
    return templates.TemplateResponse(request, "briefing.html", {})


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Role picker: Customer, Owner, Packer, Delivery + model status."""
    return templates.TemplateResponse(request, "index.html", {})


def _task_orders(statuses: list[str]):
    """Packer/delivery task lists with customer + lines."""
    conn = get_conn()
    try:
        out = []
        for o in conn.execute(
                "SELECT * FROM orders WHERE status IN ({}) ORDER BY id".format(
                    ",".join("?" * len(statuses))), statuses):
            cust = conn.execute(
                "SELECT name, area FROM customers WHERE id = ?",
                (o["customer_id"],)).fetchone()
            lines = [dict(r) for r in conn.execute(
                "SELECT ol.item_id, ol.qty, ol.packed_qty, i.name, i.unit"
                " FROM order_lines ol JOIN items i ON i.id = ol.item_id"
                " WHERE ol.order_id = ? ORDER BY ol.id", (o["id"],))]
            out.append({"id": o["id"], "status": o["status"], "total": o["total"],
                        "customer": cust["name"], "area": cust["area"],
                        "lines": lines})
        return out
    finally:
        conn.close()


@app.get("/api/tasks/packing")
def tasks_packing():
    """Orders waiting to be packed."""
    return _task_orders(["PACKING"])


@app.post("/api/tasks/packing/{order_id}")
def tasks_pack_submit(order_id: int, payload: dict):
    """Packer submits packed counts; mismatch locks dispatch, owner notified."""
    try:
        counts = {int(k): float(v) for k, v in
                  (payload.get("counts") or {}).items()}
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "counts must map item_id to quantity.")
    try:
        return {"status": engine.submit_pack_counts(order_id, counts)}
    except engine.EngineError as e:
        raise HTTPException(409, str(e))


@app.get("/api/tasks/delivery")
def tasks_delivery():
    """Orders ready to go out or already on the way."""
    return _task_orders(["READY_FOR_DELIVERY", "OUT_FOR_DELIVERY"])


@app.post("/api/tasks/delivery/{order_id}/start")
def tasks_delivery_start(order_id: int):
    """Rider leaves the shop."""
    try:
        return {"status": engine.start_delivery(order_id)}
    except engine.EngineError as e:
        raise HTTPException(409, str(e))


@app.post("/api/tasks/delivery/{order_id}/delivered")
def tasks_delivered(order_id: int, payload: dict):
    """Delivered; cash/upi closes it, credit adds to the khata."""
    try:
        return {"status": engine.mark_delivered(
            order_id, payload.get("payment_mode", ""))}
    except engine.EngineError as e:
        raise HTTPException(409, str(e))


@app.post("/api/tasks/delivery/{order_id}/problem")
def tasks_problem(order_id: int, payload: dict):
    """Rider flags a problem; order waits for retry, owner notified."""
    note = (payload.get("note") or "").strip()
    if not note:
        raise HTTPException(400, "A short note is required.")
    try:
        return {"status": engine.report_delivery_problem(order_id, note)}
    except engine.EngineError as e:
        raise HTTPException(409, str(e))


@app.get("/packer", response_class=HTMLResponse)
def packer_page(request: Request):
    """Packer task board: big tap targets, works one-handed."""
    return templates.TemplateResponse(request, "packer.html", {})


@app.get("/delivery", response_class=HTMLResponse)
def delivery_page(request: Request):
    """Delivery task board: big tap targets, works one-handed."""
    return templates.TemplateResponse(request, "delivery.html", {})


@app.get("/api/demo/clock")
def demo_clock():
    """Demo mode flag + clock offset for the portal badges."""
    return {"demo": os.environ.get("MUNSHI_DEMO", "0") == "1",
            "offset_min": clock.offset_minutes()}


@app.post("/api/demo/skip")
def demo_skip(minutes: int = 10):
    """Advance the demo clock (demo mode only)."""
    try:
        return {"offset_min": clock.skip(minutes)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/supervisor/tick")
def supervisor_tick():
    """Run one deterministic supervision sweep now (button + tests)."""
    from app import supervisor as _super

    try:
        return _super.scan_once()
    except ValueError as e:  # bad supervision section in rules.yaml
        raise HTTPException(400, str(e))


@app.get("/t/{token}", response_class=HTMLResponse)
def track_order(token: str, request: Request):
    """Render the noindex customer view, including a friendly unknown-link page."""
    payload = tracking.track_json(token)
    response = templates.TemplateResponse(request, "track.html", {
        "payload": payload,
        "unknown": payload is None,
    }, status_code=404 if payload is None else 200)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/track/{token}")
def track_order_json(token: str):
    """Minimal public status payload for the customer's polling view."""
    payload = tracking.track_json(token)
    if payload is None:
        raise HTTPException(404, "Tracking link not found.")
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})
