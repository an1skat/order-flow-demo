import os

os.environ["MOCK_LATENCY"] = "0"

import pytest

from app import db, pipeline
from app.errors import PermanentError


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db.connect(str(tmp_path / "t.db"))
    monkeypatch.setattr(pipeline, "BACKOFF", (0, 0, 0))
    for k in ("NP_API_KEY", "CHECKBOX_LICENSE_KEY", "KEYCRM_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def make_order(faults=(), phone="093 123 45 67", oid="T-1"):
    order = {"id": oid, "crm_order_id": "42", "customer": "Тест", "phone": phone,
             "np_city_ref": "c", "np_warehouse_ref": "w", "total": 100.0,
             "items": [{"sku": "A", "name": "Товар", "price": 100.0, "qty": 1}],
             "demo_faults": list(faults)}
    db.create_order(oid, order)
    return oid


def test_happy_path():
    r = pipeline.process(make_order())
    assert r["status"] == "done" and r["ttn"] and r["receipt_id"] and r["crm_synced"] == 1
    assert r["payload"]["phone"] == "380931234567"


def test_rerun_does_not_duplicate_ttn_or_receipt():
    oid = make_order()
    first = pipeline.process(oid)
    second = pipeline.process(oid)
    assert second["ttn"] == first["ttn"]
    assert not any(c[0] in ("np.create_ttn", "checkbox.sell") for c in second["calls"])


def test_transient_errors_are_retried():
    r = pipeline.process(make_order(faults=["np_timeout", "crm_500"]))
    assert r["status"] == "done"
    assert [c[0] for c in r["calls"]].count("np.create_ttn") == 2


def test_closed_shift_is_opened_automatically():
    r = pipeline.process(make_order(faults=["checkbox_shift_closed"]))
    names = [c[0] for c in r["calls"]]
    assert r["status"] == "done"
    assert names.index("checkbox.open_shift") < len(names) - 1


def test_bad_phone_stops_before_calling_nova_poshta_then_fix_resumes():
    oid = make_order(phone="12-34-5")
    r = pipeline.process(oid)
    assert r["status"] == "needs_attention" and not r["ttn"]
    assert not any(c[0] == "np.create_ttn" for c in r["calls"])
    order = db.get_order(oid)
    order["payload"]["phone"] = "0501112233"
    db.update_order(oid, payload=order["payload"])
    assert pipeline.process(oid)["status"] == "done"


def test_duplicate_webhook_event_is_ignored():
    assert db.remember_webhook("evt-1") is True
    assert db.remember_webhook("evt-1") is False


@pytest.mark.parametrize("raw,expected", [
    ("093 123 45 67", "380931234567"),
    ("+38 (067) 555-12-34", "380675551234"),
    ("380501112233", "380501112233"),
])
def test_phone_normalization(raw, expected):
    assert pipeline.normalize_phone(raw) == expected


def test_phone_normalization_rejects_garbage():
    with pytest.raises(PermanentError):
        pipeline.normalize_phone("12-34-5")
