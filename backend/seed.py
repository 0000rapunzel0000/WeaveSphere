"""Demo data for an empty database: one account per role, master data, stock and a few orders.
Run from backend/:  python3 seed.py      (DEMO_PASSWORD=... to choose the password)
Goes through the real API, so every rule and validation applies."""
import os
import sys

from fastapi.testclient import TestClient

import app
from mill.config import DB_PATH

PASSWORD = os.environ.get("DEMO_PASSWORD", "everest-demo")
c = TestClient(app.app)


def call(method, path, body=None, token=None):
    r = c.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"} if token else {})
    if r.status_code >= 400:
        sys.exit(f"{method} {path} failed: {r.status_code} {r.text}")
    return r.json()


setup = c.post("/auth/setup", json={"username": "owner", "password": PASSWORD})
if setup.status_code == 403:
    sys.exit(f"{DB_PATH} already has users; seed only runs on an empty database.")
T = setup.json()["token"]
for user, role in [("admin", "admin"), ("store", "store"), ("floor", "supervisor"), ("qc", "qc")]:
    call("POST", "/users", {"username": user, "password": PASSWORD, "role": role}, T)


def m(table, **row):
    return call("POST", f"/master/{table}", row, T)["id"]


meridian = m("buyers", name="Meridian Apparel", phone="+91 98400 10401", email="buying@meridian.example", gstin="33AAACM1234F1Z5")
northloom = m("buyers", name="North Loom Studio", phone="+91 98100 20802", email="orders@northloom.example")
south = m("suppliers", name="South Cotton Co.", phone="+91 94440 11001")
rethread = m("suppliers", name="ReThread Fibres", phone="+91 94440 11002")
cotton30 = m("yarn_types", count="30s", material="cotton", colour="natural", reorder_kg=250)
poly24 = m("yarn_types", count="24s", material="recycled polyester", colour="navy", reorder_kg=180)
cotton20 = m("yarn_types", count="20s", material="cotton", colour="ecru", reorder_kg=220)
poplin = m("fabric_types", name="Organic poplin", hsn="5208", gst_rate=5)
twill = m("fabric_types", name="Recycled twill", hsn="5407", gst_rate=5)
canvas = m("fabric_types", name="Canvas 10 oz", hsn="5209", gst_rate=5)
m("bom", fabric_type_id=poplin, yarn_type_id=cotton30, kg_per_m=0.18)
m("bom", fabric_type_id=twill, yarn_type_id=poly24, kg_per_m=0.23)
m("bom", fabric_type_id=twill, yarn_type_id=cotton30, kg_per_m=0.04)
m("bom", fabric_type_id=canvas, yarn_type_id=cotton20, kg_per_m=0.29)
spin = m("machines", name="Spinning 01", kind="spinning", overhead_per_hour=180)
loom1 = m("machines", name="Loom 01", kind="weaving", overhead_per_hour=220, rate_m_per_hour=12)
loom4 = m("machines", name="Loom 04", kind="weaving", overhead_per_hour=205, rate_m_per_hour=14)
dye = m("machines", name="Dye line 01", kind="dyeing", overhead_per_hour=310, rate_m_per_hour=40)
arun = m("workers", name="Arun Kumar", wage_type="piece", rate=2)
meena = m("workers", name="Meena Devi", wage_type="daily", rate=620)
ravi = m("workers", name="S. Ravi", wage_type="daily", rate=820)
day = m("shifts", name="Day", starts="06:00", ends="14:00")
night = m("shifts", name="Night", starts="22:00", ends="06:00")

for sup, yarn, kg, rate in [(south, cotton30, 920, 210), (rethread, poly24, 150, 168), (south, cotton20, 610, 185)]:
    call("POST", "/grn", {"supplier_id": sup, "yarn_type_id": yarn, "qty_kg": kg, "rate": rate, "challan_no": "DEMO"}, T)

o1 = call("POST", "/orders", {"buyer_id": meridian, "fabric_type_id": poplin, "colour": "ivory", "qty_m": 1200,
                              "deadline": "2026-11-20", "price_per_m": 480}, T)["id"]
o2 = call("POST", "/orders", {"buyer_id": northloom, "fabric_type_id": twill, "colour": "navy", "qty_m": 860,
                              "deadline": "2026-11-05", "price_per_m": 540}, T)["id"]  # short of polyester
call("POST", "/orders", {"buyer_id": meridian, "fabric_type_id": canvas, "colour": "ecru", "qty_m": 1050,
                         "deadline": "2026-12-15", "price_per_m": 610}, T)

call("POST", "/issues", {"order_id": o1, "yarn_type_id": cotton30, "qty_kg": 216, "machine_id": loom4}, T)
call("POST", "/jobs", {"order_id": o1, "machine_id": loom4, "operator_id": arun, "shift_id": night}, T)
call("POST", "/jobs", {"order_id": o2, "machine_id": loom1, "operator_id": arun, "shift_id": night}, T)
call("POST", f"/machines/{loom4}/status", {"status": "Running"}, T)
call("POST", f"/machines/{spin}/status", {"status": "Maintenance"}, T)
call("POST", "/attendance", {"shift_id": night, "worker_ids": [arun, meena, ravi]}, T)
call("POST", "/production", {"shift_id": night, "machine_id": loom4, "order_id": o1, "operator_id": arun, "output": 600, "hours": 8}, T)
rolls = [call("POST", "/rolls", {"order_id": o1, "machine_id": loom4, "shift_id": night, "operator_id": arun, "metres": 200}, T)["qr"]
         for _ in range(3)]

print(f"Seeded {DB_PATH}.")
print(f"Accounts: owner, admin, store, floor, qc. Password: {PASSWORD}")
print(f"Rolls waiting for QC: {', '.join(rolls)}")
