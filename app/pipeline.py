"""Конвеєр: оплачене замовлення → ТТН → фіскальний чек → номер ТТН у CRM.

Принципи:
- кожен крок ідемпотентний: якщо ТТН уже є, вдруге її не створюємо;
- тимчасові збої повторюємо з паузою, помилки в даних — зупиняємо і показуємо людині;
- закрита зміна в касі — відкриваємо її самі й пробиваємо чек ще раз.
"""
import re
import threading
import time

from . import db
from .clients import build_clients
from .errors import PermanentError, ShiftClosedError, TransientError

RETRIES = 3
BACKOFF = (0.4, 1.0, 2.0)  # секунди між спробами

_order_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(order_id: str) -> threading.Lock:
    with _locks_guard:
        return _order_locks.setdefault(order_id, threading.Lock())


def normalize_phone(raw: str) -> str:
    """'093 123 45 67', '+38 (093) 123-45-67' → '380931234567'. Інакше PermanentError."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    if len(digits) == 12 and digits.startswith("380"):
        return digits
    raise PermanentError(f"Некоректний телефон отримувача: «{raw}». Нова Пошта такий не прийме.")


def _with_retries(order_id: str, step: str, fn):
    for attempt in range(1, RETRIES + 1):
        try:
            return fn()
        except TransientError as exc:
            if attempt == RETRIES:
                raise
            pause = BACKOFF[attempt - 1]
            db.log(order_id, step, f"{exc}. Спроба {attempt} з {RETRIES}, повтор через {pause} с", "warn")
            time.sleep(pause)


def process(order_id: str) -> dict:
    """Проганяє замовлення через усі кроки. Безпечно викликати повторно."""
    lock = _lock_for(order_id)
    if not lock.acquire(blocking=False):
        db.log(order_id, "pipeline", "Замовлення вже обробляється — паралельний запуск пропущено", "warn")
        return db.get_order(order_id)
    try:
        order = db.get_order(order_id)
        payload = order["payload"]
        calls: list = []
        np, checkbox, crm = build_clients(payload.get("demo_faults", []), calls)
        db.update_order(order_id, status="processing", error=None)

        try:
            # 1. ТТН
            if order["ttn"]:
                db.log(order_id, "ttn", f"ТТН {order['ttn']} вже є — повторно не створюємо")
            else:
                payload["phone"] = normalize_phone(payload["phone"])
                ttn = _with_retries(order_id, "ttn", lambda: np.create_ttn(payload))
                db.update_order(order_id, ttn=ttn, payload=payload)
                order["ttn"] = ttn
                db.log(order_id, "ttn", f"Створено ТТН {ttn}", "ok")

            # 2. Чек
            if order["receipt_id"]:
                db.log(order_id, "receipt", "Чек уже пробито — пропускаємо")
            else:
                try:
                    receipt = _with_retries(order_id, "receipt", lambda: checkbox.sell(payload))
                except ShiftClosedError:
                    db.log(order_id, "receipt", "Зміна в касі закрита — відкриваю зміну", "warn")
                    checkbox.open_shift()
                    receipt = _with_retries(order_id, "receipt", lambda: checkbox.sell(payload))
                db.update_order(order_id, receipt_id=receipt)
                order["receipt_id"] = receipt
                db.log(order_id, "receipt", f"Фіскальний чек створено ({receipt[:8]}…)", "ok")

            # 3. CRM
            if order["crm_synced"]:
                db.log(order_id, "crm", "ТТН у CRM уже записана")
            else:
                _with_retries(order_id, "crm",
                              lambda: crm.set_tracking(payload["crm_order_id"], order["ttn"]))
                db.update_order(order_id, crm_synced=1)
                db.log(order_id, "crm", f"ТТН записана в KeyCRM, замовлення #{payload['crm_order_id']}", "ok")

            db.update_order(order_id, status="done")
            db.log(order_id, "pipeline", "Готово: ТТН, чек і CRM синхронізовані", "ok")
        except PermanentError as exc:
            db.update_order(order_id, status="needs_attention", error=str(exc))
            db.log(order_id, "pipeline", f"Зупинено: {exc} Потрібна дія людини, повтор не допоможе.", "error")
        except TransientError as exc:
            db.update_order(order_id, status="failed", error=str(exc))
            db.log(order_id, "pipeline", f"Сервіс недоступний після {RETRIES} спроб: {exc}", "error")
        result = db.get_order(order_id)
        result["calls"] = calls
        return result
    finally:
        lock.release()
