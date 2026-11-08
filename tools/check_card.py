import json
import re
import sys

from jsonpath_ng.ext import parse

FILTERS = {"urlencode", "strip_quotes", "int", "escape_restli"}
PH = re.compile(r"\{\{(.*?)\}\}")
with open(sys.argv[1]) as fh:
    card = json.load(fh)
errs = []
provider_ids = {p["id"] for p in card.get("connect_providers", [])}
d = card["defaults"]


def walk(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)
    elif isinstance(o, str):
        yield o


def check_ph(s, where, params, step_keys, auth):
    for m in PH.finditer(s):
        parts = [p.strip() for p in m.group(1).split("|")]
        ref, filters = parts[0], parts[1:]
        for f in filters:
            name = f.split(":")[0]
            if name == "offset":
                if not re.fullmatch(r"offset:\d+", f):
                    errs.append(f"{where}: bad offset filter {f}")
            elif name not in FILTERS:
                errs.append(f"{where}: unknown filter {f}")
        if ref == "base_url":
            continue
        ns, _, rest = ref.partition(".")
        if ns == "params":
            if rest not in params:
                errs.append(f"{where}: unknown param {rest}")
        elif ns == "session":
            if not rest.startswith("cookies."):
                errs.append(f"{where}: bad session ref {ref}")
            if auth is None:
                errs.append(f"{where}: session ref with auth_provider null")
        elif ns == "steps":
            sid, _, key = rest.partition(".")
            if (sid, key) not in step_keys:
                errs.append(f"{where}: unknown/forward step ref {ref}")
        else:
            errs.append(f"{where}: unknown namespace {ns}")


def check_jp(expr, where):
    try:
        parse(expr)
    except Exception as e:
        errs.append(f"{where}: bad jsonpath {expr}: {e}")


for p in card.get("connect_providers", []):
    for s in walk(p["session_check"]):
        check_ph(s, f"provider {p['id']}", {}, set(), p["id"])
    for path in p["session_check"]["expect"].get("paths", []):
        check_jp(path, f"provider {p['id']}")
for s in walk(d["headers"]):
    check_ph(s, "defaults.headers", {}, set(), d.get("auth_provider"))

names = set()
SIG = re.compile(r"^(status:\d{3}|redirect:/\S*|body_contains:.+)$")
CT = {"json", "html", "text", "any"}
PT = {"string", "integer", "number", "boolean", "enum"}
tp = card.get("tool_prefix", "")
if not re.fullmatch(r"[a-z0-9_]{1,20}", tp):
    errs.append(f"bad tool_prefix {tp!r}")
for sgl in card.get("blocked_signals", []) + card.get("changed_signals", []):
    if not SIG.match(sgl):
        errs.append(f"bad blocked signal {sgl}")
for p_ in card.get("connect_providers", []):
    for sgl in p_.get("logged_out_signals", []):
        if not SIG.match(sgl):
            errs.append(f"bad logged_out signal {sgl}")
    if p_["session_check"]["expect"].get("content_type") not in CT:
        errs.append("bad session_check content_type")
for a in card["actions"]:
    n = a["name"]
    if n in names:
        errs.append(f"duplicate action {n}")
    names.add(n)
    auth = a.get("auth_provider", d.get("auth_provider"))
    if auth is not None and auth not in provider_ids:
        errs.append(f"{n}: unknown auth_provider {auth}")
    params = a["params"]
    if len(f"{tp}_{n}") > 64:
        errs.append(f"{n}: tool name too long")
    for pn, pd in params.items():
        if pn.startswith("_"):
            errs.append(f"{n}.{pn}: reserved name")
        if pd.get("type") not in PT:
            errs.append(f"{n}.{pn}: bad type")
        if pd.get("type") == "enum" and not pd.get("values"):
            errs.append(f"{n}.{pn}: enum without values")
    for pn, pd in params.items():
        if pd.get("required") and "default" in pd:
            errs.append(f"{n}.{pn}: required with default")
    for req in ("kind", "spends", "idempotent", "requires_confirmation", "steps", "output", "examples"):
        if req not in a:
            errs.append(f"{n}: missing {req}")
    if a["kind"] == "write" and not a.get("requires_confirmation"):
        errs.append(f"{n}: write without requires_confirmation (warn)")
    step_keys = set()
    for st in a["steps"]:
        for s in walk(st["request"]):
            check_ph(s, f"{n}.{st['id']}", params, step_keys, auth)
        for path in st["expect"].get("paths", []):
            check_jp(path, f"{n}.{st['id']}.expect")
        if st["expect"].get("content_type") not in CT:
            errs.append(f"{n}.{st['id']}: bad content_type")
        for k, ex in st.get("extract", {}).items():
            if ex.get("type") == "jsonpath":
                check_jp(ex["expr"], f"{n}.{st['id']}.extract.{k}")
            elif ex.get("type") == "regex":
                re.compile(ex["expr"])
                if ex.get("resolve") not in (None, "rows") or (
                    ex.get("resolve") and ex.get("parse") != "json"
                ):
                    errs.append(f'{n}.{st["id"]}.extract.{k}: resolve "rows" needs parse "json"')
                if "then" in ex:
                    check_jp(ex["then"], f"{n}.{st['id']}.extract.{k}.then")
            else:
                errs.append(f"{n}.{st['id']}.extract.{k}: missing/bad type")
            step_keys.add((st["id"], k))
    o = a["output"]
    if o["from"] is not None:
        sid, key = o["from"].split(".")[1:3]
        if (sid, key) not in step_keys:
            errs.append(f"{n}: output.from unknown {o['from']}")
        for f, expr in o["fields"].items():
            check_jp(expr, f"{n}.output.{f}")
    np_ = o.get("next_params")
    if np_:
        if np_["param"] not in params:
            errs.append(f"{n}: next_params.param unknown")
        if o.get("limit") != np_.get("page_size"):
            errs.append(f"{n}: limit != page_size")
        if np_.get("until"):
            sid, key = np_["until"].split(".")[1:3]
            if (sid, key) not in step_keys:
                errs.append(f"{n}: until unknown")
    v = a.get("verify")
    if v:
        for s in walk(v["request"]):
            check_ph(s, f"{n}.verify", params, step_keys, auth)
        check_jp(v["assert"]["expr"], f"{n}.verify.assert")
    for ex in a["examples"]:
        for k in ex["params"]:
            if k not in params:
                errs.append(f"{n}: example uses unknown param {k}")
        for pn, pd in params.items():
            if pd.get("required") and pn not in ex["params"]:
                errs.append(f"{n}: example missing required {pn}")
    if a["kind"] == "read" and not a["examples"]:
        errs.append(f"{n}: read action without examples")

print("\n".join(errs) if errs else "OK: no issues")
