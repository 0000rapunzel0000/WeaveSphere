"""Skills: everything the chat assistant can do, one module per business area.

A skill says what it does (Jev reads this to pick it), who may use it, the fields it needs, and how to turn the
filled fields into one call to the app's own API. Adding a skill = adding one `skill(...)` to an area module.
The server still checks every call: `roles` here only keeps a role from being offered what it can't do."""
from dataclasses import dataclass, field


@dataclass
class F:
    name: str
    kind: str        # a record kind from assistant.CATALOG (machine, order, yarn…), or number, date, month, choice, text, roll, workers
    label: str
    required: bool = True
    options: tuple = ()


@dataclass
class Skill:
    name: str
    desc: str              # what Jev reads to choose; say plainly what the person would be asking for
    roles: set             # who may (the owner always may)
    fields: list
    request: object        # values -> (method, path, body)
    summary: object        # values, names -> "log 120 m on Loom 04 …" (lower-case start; shown as "I'll …" / "Done: …")
    done: object = None    # response, values, names -> what to tell the person afterwards
    read: bool = False     # only looks something up: runs straight away, no confirmation
    prepare: object = None # db, values, names -> a problem to ask about, or None (for values worked out from others)
    rules: list = field(default_factory=list)  # [values -> a question, or None]
    gives: tuple = None    # (kind, response -> value): what this makes that a later step in the same message can use


class Ref:
    """A value a later step takes from an earlier step's result ("load it onto the truck from step 1").
    Filled in when the plan runs; until then it prints as a plain description in summaries."""
    def __init__(self, step, kind):
        self.step, self.kind = step, kind

    def __format__(self, spec):
        return f"(the new {self.kind} from step {self.step})"

    __str__ = __repr__ = lambda self: format(self, "")


SKILLS: dict[str, Skill] = {}
STAFF = {"admin", "store", "supervisor", "qc"}


def skill(*a, **kw):
    s = Skill(*a, **kw)
    assert s.name not in SKILLS, s.name
    SKILLS[s.name] = s
    return s


# Small formatting helpers shared by the area modules.
def g(x):
    return f"{x:,.2f}".rstrip("0").rstrip(".") if isinstance(x, (int, float)) else str(x)


def money(x):
    return f"₹{x:,.2f}"


def opt(v, fmt):
    return "" if v is None or (isinstance(v, (str, list)) and not v) else fmt.format(v)  # a Ref (later step) prints too


def low_line(r):
    lows = r.get("low_stock") or []
    return " Low stock: " + ", ".join(f"{y['count']} {y['material']} {y['colour']} ({g(y['stock_kg'])} kg)" for y in lows) + "." if lows else ""


from mill.skills import production, inventory, quality, dispatch, orders, inbox, people, accounts, masters  # noqa: E402,F401
