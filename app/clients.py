"""Клієнти Нової Пошти, Checkbox і KeyCRM: справжні (за публічною документацією) та демо-симулятори.

Справжні клієнти вмикаються змінними оточення (див. .env.example). У демо-режимі
працюють симулятори з тими самими інтерфейсами й помилками, які можна ввімкнути
перемикачами на сторінці.
"""
import os
import random
import time
import uuid

import requests

from .errors import PermanentError, ShiftClosedError, TransientError

TIMEOUT = 15


def _post(url: str, **kwargs) -> requests.Response:
    try:
        resp = requests.post(url, timeout=TIMEOUT, **kwargs)
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise TransientError(f"Сервіс не відповів: {exc.__class__.__name__}") from exc
    if resp.status_code >= 500:
        raise TransientError(f"Сервіс повернув {resp.status_code}")
    return resp


# ---------------------------------------------------------------- Нова Пошта
class NovaPoshtaClient:
    """API 2.0: InternetDocument.save. Ref-и відправника беруться з кабінету НП."""

    URL = "https://api.novaposhta.ua/v2.0/json/"

    def __init__(self, api_key: str, sender: dict):
        self.api_key = api_key
        self.sender = sender  # CitySender, Sender, SenderAddress, ContactSender, SendersPhone

    def create_ttn(self, order: dict) -> str:
        body = {
            "apiKey": self.api_key,
            "modelName": "InternetDocument",
            "calledMethod": "save",
            "methodProperties": {
                **self.sender,
                "PayerType": "Recipient",
                "PaymentMethod": "Cash",
                "CargoType": "Parcel",
                "ServiceType": "WarehouseWarehouse",
                "SeatsAmount": "1",
                "Weight": str(order.get("weight_kg", 1)),
                "Cost": str(order["total"]),
                "Description": f"Замовлення {order['id']}",
                "CityRecipient": order["np_city_ref"],
                "RecipientAddress": order["np_warehouse_ref"],
                "RecipientsPhone": order["phone"],
                "RecipientName": order["customer"],
                "RecipientType": "PrivatePerson",
            },
        }
        data = _post(self.URL, json=body).json()
        if not data.get("success"):
            raise PermanentError("Нова Пошта: " + "; ".join(data.get("errors") or ["невідома помилка"]))
        return data["data"][0]["IntDocNumber"]


# ---------------------------------------------------------------- Checkbox
class CheckboxClient:
    """Checkbox ПРРО: вхід касира, відкриття зміни, чек продажу."""

    def __init__(self, base_url: str, license_key: str, login: str, password: str):
        self.base = base_url.rstrip("/")
        self.license_key = license_key
        self.login = login
        self.password = password
        self.token = None

    def _headers(self) -> dict:
        if not self.token:
            resp = _post(f"{self.base}/api/v1/cashier/signin",
                         json={"login": self.login, "password": self.password})
            if resp.status_code != 200:
                raise PermanentError("Checkbox: не вдалося увійти касиром")
            self.token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {self.token}", "X-License-Key": self.license_key}

    def open_shift(self) -> None:
        resp = _post(f"{self.base}/api/v1/shifts", headers=self._headers())
        if resp.status_code not in (200, 201, 202):
            raise PermanentError(f"Checkbox: зміну не відкрито ({resp.status_code})")

    def sell(self, order: dict) -> str:
        body = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"receipt:{order['id']}")),  # ідемпотентність
            "goods": [
                {"good": {"code": i["sku"], "name": i["name"], "price": int(i["price"] * 100)},
                 "quantity": int(i["qty"] * 1000)}
                for i in order["items"]
            ],
            "payments": [{"type": "CASHLESS", "value": int(order["total"] * 100)}],
        }
        resp = _post(f"{self.base}/api/v1/receipts/sell", headers=self._headers(), json=body)
        if resp.status_code == 400 and "shift" in resp.text.lower():
            raise ShiftClosedError("Checkbox: зміна не відкрита")
        if resp.status_code not in (200, 201, 202):
            raise PermanentError(f"Checkbox: чек не створено ({resp.status_code}): {resp.text[:200]}")
        return resp.json()["id"]


# ---------------------------------------------------------------- KeyCRM
class KeyCrmClient:
    """KeyCRM OpenAPI: записати номер ТТН у замовлення. Назву поля звірити з документацією акаунта."""

    def __init__(self, api_key: str, base_url: str = "https://openapi.keycrm.app/v1"):
        self.api_key = api_key
        self.base = base_url.rstrip("/")

    def set_tracking(self, crm_order_id: str, ttn: str) -> None:
        try:
            resp = requests.put(
                f"{self.base}/order/{crm_order_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"shipping": {"tracking_code": ttn}},
                timeout=TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise TransientError("KeyCRM не відповів") from exc
        if resp.status_code >= 500:
            raise TransientError(f"KeyCRM повернув {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"KeyCRM: {resp.status_code} {resp.text[:200]}")


# ---------------------------------------------------------------- Симулятори
class Faults:
    """Перемикачі збоїв для одного замовлення. '_once' спрацьовує лише раз."""

    def __init__(self, enabled: list[str]):
        self.enabled = set(enabled)
        self.fired: set[str] = set()

    def once(self, name: str) -> bool:
        if name in self.enabled and name not in self.fired:
            self.fired.add(name)
            return True
        return False


def _latency() -> None:
    """Імітація мережевої затримки, щоб на сторінці було видно кроки по черзі."""
    time.sleep(float(os.getenv("MOCK_LATENCY", "0.6")))


class MockNovaPoshta:
    def __init__(self, faults: Faults, calls: list):
        self.faults, self.calls = faults, calls

    def create_ttn(self, order: dict) -> str:
        self.calls.append(("np.create_ttn", order["id"]))
        _latency()
        if self.faults.once("np_timeout"):
            raise TransientError("Нова Пошта не відповіла за 15 с (таймаут)")
        return "2045" + "".join(random.choices("0123456789", k=10))


class MockCheckbox:
    def __init__(self, faults: Faults, calls: list):
        self.faults, self.calls = faults, calls
        self.shift_open = "checkbox_shift_closed" not in faults.enabled

    def open_shift(self) -> None:
        self.calls.append(("checkbox.open_shift", None))
        self.shift_open = True

    def sell(self, order: dict) -> str:
        self.calls.append(("checkbox.sell", order["id"]))
        _latency()
        if not self.shift_open:
            raise ShiftClosedError("Checkbox: зміна не відкрита")
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"receipt:{order['id']}"))


class MockKeyCrm:
    def __init__(self, faults: Faults, calls: list):
        self.faults, self.calls = faults, calls

    def set_tracking(self, crm_order_id: str, ttn: str) -> None:
        self.calls.append(("keycrm.set_tracking", crm_order_id))
        _latency()
        if self.faults.once("crm_500"):
            raise TransientError("KeyCRM повернув 502 Bad Gateway")


def real_clients_configured() -> bool:
    return bool(os.getenv("NP_API_KEY") and os.getenv("CHECKBOX_LICENSE_KEY") and os.getenv("KEYCRM_API_KEY"))


def build_clients(fault_names: list[str], calls: list):
    """Справжні клієнти, якщо задані ключі, інакше симулятори."""
    if real_clients_configured():
        sender = {k: os.environ[f"NP_{k.upper()}"] for k in
                  ("CitySender", "Sender", "SenderAddress", "ContactSender", "SendersPhone")}
        return (
            NovaPoshtaClient(os.environ["NP_API_KEY"], sender),
            CheckboxClient(os.getenv("CHECKBOX_BASE_URL", "https://api.checkbox.ua"),
                           os.environ["CHECKBOX_LICENSE_KEY"], os.environ["CHECKBOX_LOGIN"],
                           os.environ["CHECKBOX_PASSWORD"]),
            KeyCrmClient(os.environ["KEYCRM_API_KEY"]),
        )
    faults = Faults(fault_names)
    return MockNovaPoshta(faults, calls), MockCheckbox(faults, calls), MockKeyCrm(faults, calls)
