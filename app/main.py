"""HTTP-шар: вебхук «замовлення оплачено», демо-ендпоінти, сторінка."""
import asyncio
import hashlib
import hmac
import json
import os
import random
import uuid
from pathlib import Path

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import db
from .clients import real_clients_configured
from .pipeline import process

STATIC = Path(__file__).parent / "static"
SECRET = os.getenv("WEBHOOK_SECRET", "demo-secret").encode()
ALLOWED_FAULTS = {"np_timeout", "np_bad_phone", "checkbox_shift_closed", "crm_500", "duplicate_webhook"}

DEMO_ITEMS = [
    {"sku": "TEA-01", "name": "Чай зелений, 100 г", "price": 189.0},
    {"sku": "MUG-02", "name": "Чашка керамічна", "price": 320.0},
    {"sku": "HNY-03", "name": "Мед гречаний, 250 г", "price": 245.0},
    {"sku": "SOK-04", "name": "Шкарпетки вовняні", "price": 150.0},
]
DEMO_NAMES = ["Олена Коваль", "Андрій Мельник", "Ірина Бойко", "Тарас Шевчук", "Марія Ткаченко"]


def sign(body: bytes) -> str:
    return hmac.new(SECRET, body, hashlib.sha256).hexdigest()


async def order_paid_webhook(request: Request):
    """Те, що надсилає магазин/платіжка після оплати. Підпис HMAC-SHA256 у X-Signature."""
    body = await request.body()
    if not hmac.compare_digest(sign(body), request.headers.get("x-signature", "")):
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    event = json.loads(body)
    if not db.remember_webhook(event["event_id"]):
        db.log(event["order"]["id"], "webhook", "Повторний вебхук з тим самим event_id — проігноровано", "warn")
        return JSONResponse({"status": "duplicate_ignored"}, status_code=200)
    order = event["order"]
    created = db.create_order(order["id"], order)
    db.log(order["id"], "webhook", "Отримано вебхук: замовлення оплачено" if created
           else "Новий вебхук для наявного замовлення — перевіряю кроки")
    return JSONResponse({"status": "accepted"}, status_code=202,
                        background=BackgroundTask(process, order["id"]))


async def create_demo_order(request: Request):
    """Створює тестове оплачене замовлення і надсилає вебхук так, як це зробив би магазин."""
    data = await request.json() if await request.body() else {}
    faults = [f for f in data.get("faults", []) if f in ALLOWED_FAULTS]
    items = [dict(i, qty=random.randint(1, 2)) for i in random.sample(DEMO_ITEMS, k=random.randint(1, 3))]
    order_id = f"DEMO-{random.randint(10000, 99999)}"
    order = {
        "id": order_id,
        "crm_order_id": str(random.randint(1200, 9999)),
        "customer": random.choice(DEMO_NAMES),
        "phone": "12-34-5" if "np_bad_phone" in faults else random.choice(
            ["093 123 45 67", "+38 (067) 555-12-34", "0501112233"]),
        "np_city_ref": "demo-city-kyiv",
        "np_warehouse_ref": "demo-warehouse-12",
        "items": items,
        "total": round(sum(i["price"] * i["qty"] for i in items), 2),
        "demo_faults": faults,
    }
    body = json.dumps({"event_id": str(uuid.uuid4()), "order": order}, ensure_ascii=False).encode()
    sends = 2 if "duplicate_webhook" in faults else 1

    # Внутрішній виклик того самого обробника, що й для зовнішнього вебхука.
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/webhooks/order-paid",
             "headers": [(b"x-signature", sign(body).encode())]}
    loop = asyncio.get_running_loop()
    for _ in range(sends):
        resp = await order_paid_webhook(Request(scope, receive))
        if resp.background:  # обробка у фоні, щоб на сторінці було видно кроки наживо
            loop.run_in_executor(None, process, order_id)
    return JSONResponse({"order_id": order_id})


async def list_orders(request: Request):
    return JSONResponse({
        "orders": db.list_orders(),
        "mode": "live" if real_clients_configured() else "demo",
        "repo_url": os.getenv("REPO_URL", ""),
        "contact_url": os.getenv("CONTACT_URL", ""),
    })


async def retry_order(request: Request):
    """Повторний запуск після виправлення даних. Уже виконані кроки не дублюються."""
    order_id = request.path_params["order_id"]
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "not found"}, status_code=404)
    data = await request.json() if await request.body() else {}
    if data.get("phone"):
        payload = order["payload"]
        db.log(order_id, "manual", f"Менеджер виправив телефон: «{payload['phone']}» → «{data['phone']}»")
        payload["phone"] = data["phone"]
        db.update_order(order_id, payload=payload)
    return JSONResponse({"status": "retrying"}, background=BackgroundTask(process, order_id))


async def index(request: Request):
    return FileResponse(STATIC / "index.html")


async def health(request: Request):
    return JSONResponse({"ok": True})


db.connect()
app = Starlette(routes=[
    Route("/", index),
    Route("/health", health),
    Route("/webhooks/order-paid", order_paid_webhook, methods=["POST"]),
    Route("/api/demo/orders", create_demo_order, methods=["POST"]),
    Route("/api/orders", list_orders),
    Route("/api/orders/{order_id}/retry", retry_order, methods=["POST"]),
])
