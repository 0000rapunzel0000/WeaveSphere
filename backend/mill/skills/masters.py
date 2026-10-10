"""Setup data: buyers, suppliers, workers, machines, yarns, fabrics, recipes, shifts."""
from mill.skills import F, g, opt, skill

OFFICE = {"admin"}


KIND = {"buyers": "buyer", "suppliers": "supplier", "workers": "worker", "machines": "machine", "yarn_types": "yarn",
        "fabric_types": "fabric", "shifts": "shift"}


def add(name, table, what, fields, body, summary, desc=None):
    skill(name, desc or f"Add a new {what} to the mill's setup data.", OFFICE, fields, lambda v: ("POST", f"/master/{table}", body(v)), summary,
          lambda r, v, n: f"Saved as #{r['id']}.", gives=(KIND[table], lambda r: r["id"]) if table in KIND else None)


def clean(d):
    return {k: x for k, x in d.items() if x not in (None, "")}


add("add_buyer", "buyers", "buyer (customer)",
    [F("name", "text", "buyer name"), F("phone", "text", "phone", required=False), F("email", "text", "email", required=False),
     F("gstin", "text", "GSTIN", required=False)],
    lambda v: clean({k: v.get(k) for k in ("name", "phone", "email", "gstin")}), lambda v, n: f"add buyer {v['name']}" + opt(v.get("phone"), ", {}"))
add("add_supplier", "suppliers", "yarn supplier",
    [F("name", "text", "supplier name"), F("phone", "text", "phone", required=False), F("gstin", "text", "GSTIN", required=False)],
    lambda v: clean({k: v.get(k) for k in ("name", "phone", "gstin")}), lambda v, n: f"add supplier {v['name']}")
add("add_worker", "workers", "worker (operator or helper)",
    [F("name", "text", "name"), F("wage_type", "choice", "paid by", options=("daily", "piece")), F("rate", "number", "rate (₹ per day or per unit)"),
     F("phone", "text", "phone", required=False)],
    lambda v: clean({k: v.get(k) for k in ("name", "wage_type", "rate", "phone")}),
    lambda v, n: f"add {v['name']}, {v['wage_type']} rate ₹{g(v['rate'])}")
add("add_machine", "machines", "machine (loom, spinning frame or dye line)",
    [F("name", "text", "machine name"), F("kind", "choice", "kind", options=("weaving", "spinning", "dyeing")),
     F("speed", "number", "running speed (m per hour)", required=False), F("overhead", "number", "overhead (₹ per hour)", required=False)],
    lambda v: clean({"name": v["name"], "kind": v["kind"], "rate_m_per_hour": v.get("speed"), "overhead_per_hour": v.get("overhead")}),
    lambda v, n: f"add {v['kind']} machine {v['name']}" + opt(v.get("speed") and g(v["speed"]), ", {} m/h"),
    "Add a NEW machine the mill installed or bought (a loom, spinning frame or dye line) to the setup data, with its running "
    "speed in m per hour (a speed, not an amount produced).")
add("add_yarn", "yarn_types", "yarn type",
    [F("count", "text", "count (e.g. 30s)"), F("material", "text", "material"), F("colour", "text", "colour"),
     F("reorder", "number", "reorder level (kg)", required=False)],
    lambda v: clean({"count": v["count"], "material": v["material"], "colour": v["colour"], "reorder_kg": v.get("reorder")}),
    lambda v, n: f"add yarn {v['count']} {v['material']} {v['colour']}" + opt(v.get("reorder") and g(v["reorder"]), ", reorder at {} kg"))
add("add_fabric", "fabric_types", "fabric type",
    [F("name", "text", "fabric name"), F("gst", "number", "GST %", required=False), F("hsn", "text", "HSN code", required=False)],
    lambda v: clean({"name": v["name"], "gst_rate": v.get("gst"), "hsn": v.get("hsn")}), lambda v, n: f"add fabric {v['name']}")
add("add_recipe", "bom", "recipe line (how many kg of a yarn one metre of a fabric uses)",
    [F("fabric", "fabric", "fabric"), F("yarn", "yarn", "yarn"), F("kg_per_m", "number", "kg per metre")],
    lambda v: {"fabric_type_id": v["fabric"], "yarn_type_id": v["yarn"], "kg_per_m": v["kg_per_m"]},
    lambda v, n: f"set {n['fabric']} to use {g(v['kg_per_m'])} kg of {n['yarn']} per metre")
add("add_shift", "shifts", "shift",
    [F("name", "text", "shift name"), F("starts", "text", "starts (HH:MM)", required=False), F("ends", "text", "ends (HH:MM)", required=False)],
    lambda v: clean({k: v.get(k) for k in ("name", "starts", "ends")}), lambda v, n: f"add the {v['name']} shift")

skill("set_reorder_level", "Change the reorder (low-stock) level of a yarn.", OFFICE,
      [F("yarn", "yarn", "yarn"), F("kg", "number", "reorder level (kg)")],
      lambda v: ("PUT", f"/master/yarn_types/{v['yarn']}", {"reorder_kg": v["kg"]}), lambda v, n: f"set {n['yarn']}'s reorder level to {g(v['kg'])} kg")

skill("change_worker_rate", "Change a worker's wage rate.", OFFICE,
      [F("worker", "worker", "worker"), F("rate", "number", "new rate (₹)")],
      lambda v: ("PUT", f"/master/workers/{v['worker']}", {"rate": v["rate"]}), lambda v, n: f"change {n['worker']}'s rate to ₹{g(v['rate'])}")
