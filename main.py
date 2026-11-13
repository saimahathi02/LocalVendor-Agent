
"""LocalVendor-Agent: Autonomous Hyperlocal Supply Chain Negotiator (hackathon prototype).
Stack (same as the PPT): Python + FastAPI backend, HTML/CSS/JS frontend, Gemini API for language tasks,
JSON storage, browser speech-to-text, WhatsApp Cloud API (simulator first).

Agents (PPT slide 4):
  1 Vendor Input Agent       receives a typed, spoken or WhatsApp (text / voice note) order
  2 Requirement Agent        extracts items, quantities, budget, deadline
  3 Supplier & Price Agent   checks live price, stock, quality, delivery
  4 Negotiation Agent        bundle offers, counteroffers, stretch round, re-rank
  5 Decision & Approval      ranks feasible deals and asks the vendor to confirm

Guardrail (PPT slide 6): the system drafts offers but NEVER contacts a supplier without explicit vendor approval.
do_approve() is the only code path that sends supplier messages.
All money math is deterministic Python. The LLM only parses text and words the negotiation chat.
This file is ASCII-only on purpose (rupee sign is written as \\u20b9) so Windows encodings cannot corrupt it.

Run:  python -m uvicorn main:app --reload      then open http://127.0.0.1:8000
"""
import base64, hashlib, hmac, itertools, json, os, random, re, threading, time, uuid
from collections import deque
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()
BASE = Path(__file__).parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
SUP_FILE, QUOTE_FILE = DATA / "suppliers.json", DATA / "quotes.json"
LOCK = threading.RLock()
OUTBOX = deque(maxlen=80)   # every WhatsApp message the app sends or simulates
LAST = {}                   # vendor number -> latest quote id (for YES / NO replies)
RUPEE = "\u20b9"
MIN_QUALITY = float(os.getenv("MIN_QUALITY", "3.5"))
GRAPH = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0")
app = FastAPI(title="LocalVendor-Agent")


def base_url():
    return os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


# ------------------------------------------------------------------ data (JSON storage)
ITEMS = {"tomato": ("kg", 5), "onion": ("kg", 30), "potato": ("kg", 30),
         "milk": ("L", 2), "paneer": ("kg", 4), "capsicum": ("kg", 6)}   # unit, shelf life in days
SEED = {"market_tick": 0, "suppliers": [
    {"id": "kisan", "name": "Kisan Mandi Direct", "phone": "+91 98000 10001", "eta": "5:30 AM", "quality": 4.4,
     "fee": 100, "free_above": 3000, "flex": 2.0, "flex_max": 3.5,
     "inv": {"tomato": [38, 90], "onion": [28, 150], "potato": [24, 200], "capsicum": [52, 40]}},
    {"id": "city", "name": "City Wholesale Hub", "phone": "+91 98000 10002", "eta": "5:45 AM", "quality": 4.0,
     "fee": 80, "free_above": 3000, "flex": 1.5, "flex_max": 3.0,
     "inv": {"tomato": [42, 300], "onion": [26, 400], "potato": [25, 300], "milk": [56, 80], "paneer": [340, 25]}},
    {"id": "gokul", "name": "Gokul Dairy & Fresh", "phone": "+91 98000 10003", "eta": "5:00 AM", "quality": 4.8,
     "fee": 60, "free_above": 3000, "flex": 1.0, "flex_max": 2.5,
     "inv": {"milk": [51, 200], "paneer": [320, 40], "capsicum": [55, 30]}}]}


def read_json(path, default):
    with LOCK:
        if not path.exists():
            path.write_text(json.dumps(default, indent=2), encoding="utf-8")
        return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, obj):
    with LOCK:
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def get_quote(qid):
    return read_json(QUOTE_FILE, {}).get(qid)


def save_quote(q):
    with LOCK:
        db = read_json(QUOTE_FILE, {})
        db[q["id"]] = q
        for k in list(db)[:-200]:
            del db[k]
        write_json(QUOTE_FILE, db)


def market():
    """Suppliers with fluctuating live price/stock (deterministic per day + 'market tick')."""
    db, out = read_json(SUP_FILE, SEED), []
    for s in db["suppliers"]:
        r = random.Random(f"{date.today()}-{db['market_tick']}-{s['id']}")
        s = dict(s)
        s["live"] = {i: (round(p * r.uniform(.97, 1.03), 2), int(st * r.uniform(.8, 1.0))) for i, (p, st) in s["inv"].items()}
        out.append(s)
    return out


def inr(x):
    x = round(x, 2)
    return RUPEE + ("{:,.0f}".format(x) if x == int(x) else "{:,.2f}".format(x))


def to_min(s):
    """'6 AM tomorrow' -> 360, '5:30 am' -> 330, '06:15' -> 375, anything else -> None."""
    s = s or ""
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", s, re.I)
    if m:
        return (int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)) * 60 + int(m.group(2) or 0)
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", s)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


# ------------------------------------------------------------------ Gemini layer
def provider():
    p = os.getenv("LLM_PROVIDER", "").lower()
    if p in ("gemini", "offline"):
        return p
    return "gemini" if os.getenv("GEMINI_API_KEY") else "offline"


def gemini(parts, system=None, as_json=False, timeout=30):
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.2}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if as_json:
        body["generationConfig"]["responseMimeType"] = "application/json"
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def llm_json(system, user):
    if provider() != "gemini":
        raise RuntimeError("offline")
    txt = gemini([{"text": user}], system, as_json=True)
    return json.loads(re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip())


# ------------------------------------------------------------------ Agent 2: Requirement Agent
PARSER_SYS = """You are the Requirement Agent for Indian street vendors and cafes. Convert the raw (maybe voice-transcribed) request into STRICT JSON:
{"items":[{"item":str,"quantity":number,"unit":"kg"|"L"|"pcs"|"dozen","max_unit_price":number|null}],"target_budget":number|null,"delivery_deadline":str|null}
Singular lowercase item names (tomato, onion, milk). Budget in INR as a plain number. max_unit_price only if explicitly stated per unit, else null.
delivery_deadline like "6:00 AM tomorrow". JSON only."""
ALIAS = {"tomatoes": "tomato", "tamatar": "tomato", "onions": "onion", "pyaz": "onion", "potatoes": "potato",
         "aloo": "potato", "capsicums": "capsicum", "bell pepper": "capsicum", "doodh": "milk"}
UNIT = {"kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg", "l": "L", "ltr": "L",
        "ltrs": "L", "litre": "L", "litres": "L", "liter": "L", "liters": "L", "pcs": "pcs", "pieces": "pcs", "dozen": "dozen"}


def canon(n):
    n = re.sub(r"[^a-z ]", "", str(n).lower()).strip()
    return ALIAS.get(n, n[:-1] if n.endswith("s") and not n.endswith("ss") else n)


def regex_parse(t):
    b = re.search(r"(?:under|below|within|budget(?: of)?|upto|up to)\s*(?:\u20b9|rs\.?|inr|rupees)?\s*(\d[\d,]*)(?:\s*(?:rupees|rs|inr)\b)?", t, re.I)
    budget = float(b.group(1).replace(",", "")) if b else None
    t2 = t.replace(b.group(0), "") if b else t
    d = re.search(r"\bby\s+(.+?)(?=$|[,.]|\s+(?:under|below|within|budget))", t2, re.I)
    deadline = d.group(1).strip() if d else None
    t2 = t2.replace(d.group(0), "") if d else t2
    t2 = re.sub(r"^.*?\b(?:need|want|require|order|get me)\s+", "", t2, flags=re.I)
    items = []
    for c in re.split(r",|\band\b", t2, flags=re.I):
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(kgs?|kilos?|kilograms?|ltrs?|litres?|liters?|l|pcs|pieces|dozen)?\b\s*(?:of\s+)?([a-z ]+)", c, re.I)
        if m:
            items.append({"item": m.group(3), "quantity": float(m.group(1)), "unit": m.group(2) or "", "max_unit_price": None})
    return {"items": items, "target_budget": budget, "delivery_deadline": deadline}


def normalize(d):
    items = []
    for i in d.get("items", []):
        name, q = canon(i["item"]), float(i["quantity"])
        if q <= 0 or not name:
            continue
        unit = UNIT.get(str(i.get("unit", "")).lower().strip(), ITEMS.get(name, ("kg",))[0])
        mp = i.get("max_unit_price")
        items.append({"item": name, "quantity": q, "unit": unit, "max_unit_price": float(mp) if mp else None})
    if not items:
        raise ValueError("Could not find any items in the request. Try: '50kg tomatoes, 20kg onions by 6 AM under \u20b93500'.")
    tb = d.get("target_budget")
    return {"items": items, "target_budget": float(tb) if tb else None, "delivery_deadline": d.get("delivery_deadline")}


def parse_order(text):
    p, rx = provider(), regex_parse(text)
    if p == "gemini":
        try:
            parsed = normalize(llm_json(PARSER_SYS, text))
            parsed["target_budget"] = parsed["target_budget"] or rx["target_budget"]        # fill gaps from rules
            parsed["delivery_deadline"] = parsed["delivery_deadline"] or rx["delivery_deadline"]
            return parsed, "Gemini LLM"
        except Exception as e:
            print("[Gemini parser fallback]", type(e).__name__, e)
    return normalize(rx), "offline rule-based parser" + (" (Gemini unavailable)" if p == "gemini" else "")


# ------------------------------------------------------------------ Agents 3-4: pricing rules + optimizer
def feasible(s, i):
    return i["item"] in s["live"] and s["live"][i["item"]][1] >= i["quantity"]


def vol_pct(unit, q):
    lo, hi = {"kg": (30, 50), "L": (20, 40)}.get(unit, (30, 50))
    return 8 if q >= hi else 5 if q >= lo else 0


def price(s, lines, lvl):
    """lvl 1 = volume + bundle rules, 2 = + haggle, 3 = + stretch (the supplier's maximum flexibility)."""
    rows, sub = [], 0
    for l in lines:
        lt = l["qty"] * l["list"]
        vp = vol_pct(l["unit"], l["qty"]) if lvl >= 1 else 0
        rows.append(dict(l, list_total=round(lt, 2), vp=vp, av=lt * (1 - vp / 100), shelf=ITEMS.get(l["item"], ("", None))[1]))
        sub += lt * (1 - vp / 100)
    free = lvl >= 1 and sub >= s["free_above"]
    bp = 2 if free else 0
    hp = (s["flex"] if lvl == 2 else s["flex_max"]) if lvl >= 2 and sub >= 1200 else 0
    f = (1 - bp / 100) * (1 - hp / 100)
    for r in rows:
        r["net"] = round(r["av"] * f, 2)
        r["eff"] = round(r["net"] / r["qty"], 2)
    fee, lt, net = (0 if free else s["fee"]), round(sum(r["list_total"] for r in rows), 2), round(sum(r["net"] for r in rows), 2)
    return dict(supplier=s["name"], phone=s["phone"], eta=s["eta"], quality=s["quality"], rows=rows, list_total=lt,
                net=net, fee=fee, total=round(net + fee, 2), bp=bp, hp=hp, free=free, saved=round(lt - net, 2))


def build_plans(items, sups, lvl):
    """Try every feasible item->supplier assignment; keep the cheapest one per distinct supplier combination."""
    opts = [[s for s in sups if feasible(s, i)] for i in items]
    best, n = {}, 0
    for combo in itertools.product(*opts):
        n += 1
        g = {}
        for s, i in zip(combo, items):
            g.setdefault(s["id"], (s, []))[1].append(dict(item=i["item"], qty=i["quantity"], unit=i["unit"],
                                                          list=s["live"][i["item"]][0], maxp=i["max_unit_price"]))
        carts = [price(s, ls, lvl) for s, ls in g.values()]
        viol = sum(1 for c in carts for r in c["rows"] if r["maxp"] and r["eff"] > r["maxp"])
        key, cost = tuple(sorted(g)), round(sum(c["total"] for c in carts), 2)
        if key not in best or (viol, cost) < best[key][0]:
            best[key] = ((viol, cost), carts)
    return [v[1] for v in best.values()], n


def make_plan(carts, budget, dmin):
    total = round(sum(c["total"] for c in carts), 2)
    list_total = round(sum(c["list_total"] for c in carts), 2)
    quality = round(sum(c["quality"] * c["net"] for c in carts) / (sum(c["net"] for c in carts) or 1), 2)
    viol = [f"{r['item']} effective price {inr(r['eff'])} exceeds your max {inr(r['maxp'])}"
            for c in carts for r in c["rows"] if r["maxp"] and r["eff"] > r["maxp"]]
    eta = max(carts, key=lambda c: to_min(c["eta"]))["eta"]
    return dict(carts=carts, suppliers=[c["supplier"] for c in carts], total=total, list_total=list_total,
                quality=quality, eta=eta, ontime=dmin is None or to_min(eta) <= dmin,
                within=budget is None or total <= budget, violations=viol,
                budget_left=None if budget is None else round(budget - total, 2),
                pct_below=None if not budget else round((budget - total) / budget * 100, 1),
                value=round(total * (1 + 0.02 * (5 - quality)), 2))   # cost, nudged 2% per quality star below 5


def rank(plans):
    """Feasible deals first, then lowest quality-adjusted cost, then fewest suppliers. Keep the top 3."""
    for p in plans:
        p["feasible"] = p["within"] and p["ontime"] and not p["violations"]
    plans.sort(key=lambda p: (not p["feasible"], p["value"], len(p["suppliers"])))
    for n, p in enumerate(plans[:3], 1):
        p["rank"] = n
    return plans[:3]


NEG_SYS = """Write short, realistic WhatsApp haggling threads between a small vendor's purchasing agent ('vendor') and a wholesale supplier rep ('supplier').
Light Hinglish ('ji', 'bhaiya') is fine. Use ONLY the numbers given; the final message must state final_total exactly as written.
If stretch_round is true, include one extra push where the vendor cites a tight budget and the supplier concedes.
Return JSON: {"threads":[{"supplier":"<name>","messages":[{"from":"vendor"|"supplier","text":"..."}]}]} with 4-6 messages each."""


def threads(carts, deadline, stretch):
    facts = [dict(supplier=c["supplier"], items=[f'{r["qty"]:g}{r["unit"]} {r["item"]}' for r in c["rows"]],
                  list_total=inr(c["list_total"]), bundle_discount_pct=c["bp"], free_delivery=c["free"],
                  extra_haggle_pct=c["hp"], final_total=inr(c["total"])) for c in carts]
    try:
        d = llm_json(NEG_SYS, json.dumps({"deadline": deadline, "stretch_round": stretch, "suppliers": facts}))
        out = {t["supplier"]: t["messages"] for t in d["threads"]}
        norm = lambda t: re.sub(r"[\u20b9,\s]", "", t)
        assert all(norm(f["final_total"]) in norm(json.dumps(out[f["supplier"]], ensure_ascii=False)) for f in facts)
        return out, "Gemini-written"
    except Exception as e:
        if provider() != "offline":
            print("[Gemini negotiation fallback]", type(e).__name__, e)
        out = {}
        for c, f in zip(carts, facts):
            m = [("vendor", f"Namaste ji! Need {', '.join(f['items'])} by {deadline or 'tomorrow'}. List total {f['list_total']}. Bulk rate possible?"),
                 ("supplier", "For this size: volume discounts applied" + (f", plus {c['bp']}% bundle and FREE delivery." if c["free"] else f", delivery stays {inr(c['fee'])}."))]
            if c["hp"]:
                m += [("vendor", "Regular customer, UPI payment right away. Can you go a little lower?"),
                      ("supplier", f"Okay ji, extra {c['hp']}% off for you.")]
            m.append(("supplier", f"Final: {f['final_total']} including delivery. Deal!"))
            out[c["supplier"]] = [{"from": a, "text": b} for a, b in m]
        return out, "template (offline)"


# ------------------------------------------------------------------ WhatsApp (Cloud API, simulator first)
def wa(c, oid, dl):
    L = [f"*LocalVendor Order #{oid}*", f"To: {c['supplier']}", ""]
    L += [f"{n}. {r['item'].title()} - {r['qty']:g} {r['unit']} @ {inr(r['eff'])}/{r['unit']} = {inr(r['net'])}" for n, r in enumerate(c["rows"], 1)]
    L += ["", f"Delivery: {'FREE' if not c['fee'] else inr(c['fee'])}", f"*Total: {inr(c['total'])}*",
          f"Deliver by: {dl or 'ASAP'}", "Pay: UPI on delivery", "Please reply YES to confirm."]
    return "\n".join(L)


def wa_send(to, body, tag=""):
    """Send through the WhatsApp Cloud API when WHATSAPP_TOKEN + WHATSAPP_PHONE_ID are set, else only simulate."""
    tok, pid = os.getenv("WHATSAPP_TOKEN"), os.getenv("WHATSAPP_PHONE_ID")
    status, detail = "simulated", "Shown in the simulator (WhatsApp Cloud API not configured)"
    if tok and pid and not str(to).startswith("sim"):
        try:
            r = requests.post(f"https://graph.facebook.com/{GRAPH}/{pid}/messages", timeout=20,
                              headers={"Authorization": "Bearer " + tok},
                              json={"messaging_product": "whatsapp", "to": digits(to), "type": "text", "text": {"body": body}})
            if r.status_code >= 300:
                status, detail = "failed", str(r.json().get("error", {}).get("message", r.text))[:200]
            else:
                status, detail = "sent", "Sent via WhatsApp Cloud API"
        except Exception as e:
            status, detail = "failed", str(e)[:200]
    OUTBOX.append(dict(ts=time.strftime("%H:%M:%S"), to=str(to), tag=tag, body=body, status=status, detail=detail))
    return status, detail


def wa_media(media_id):
    h = {"Authorization": "Bearer " + os.environ["WHATSAPP_TOKEN"]}
    meta = requests.get(f"https://graph.facebook.com/{GRAPH}/{media_id}", headers=h, timeout=20).json()
    blob = requests.get(meta["url"], headers=h, timeout=30)
    blob.raise_for_status()
    return blob.content, meta.get("mime_type", "audio/ogg")


def transcribe(audio, mime):
    """WhatsApp voice note -> text using Gemini (speech-to-text for the WhatsApp channel)."""
    parts = [{"text": "Transcribe this voice note exactly. It may be English, Hindi or Hinglish. Return plain text only."},
             {"inlineData": {"mimeType": mime.split(";")[0], "data": base64.b64encode(audio).decode()}}]
    return gemini(parts, timeout=60).strip()


# ------------------------------------------------------------------ pipeline (streams agent steps)
CHANNEL = {"text": "typed text", "voice": "voice (browser speech-to-text)", "whatsapp": "WhatsApp text message",
           "whatsapp-voice": "WhatsApp voice note (Gemini transcription)", "simulator": "WhatsApp simulator"}


def ev(type_, **kw):
    return json.dumps(dict(type=type_, **kw)) + "\n"


def pipeline(text, source="text", pace=True):
    t0 = time.time()
    nap = (lambda: time.sleep(float(os.getenv("DEMO_DELAY", "0.5")))) if pace else (lambda: None)
    qid = "LV" + uuid.uuid4().hex[:5].upper()
    rec = dict(id=qid, ts=time.strftime("%F %T"), source=source, text=text, status="failed", plans=[], complete=False)

    def fail(msg):
        rec.update(error=msg, elapsed=round(time.time() - t0, 1))
        save_quote(rec)
        return ev("error", message=msg)

    # Agent 1: Vendor Input
    yield ev("step", agent="Vendor Input Agent", title="Received the vendor request",
             lines=[f"Channel: {CHANNEL.get(source, source)}", f"Request: {text}"]); nap()

    # Agent 2: Requirement
    try:
        parsed, engine = parse_order(text)
    except ValueError as e:
        yield fail(str(e)); return
    dl, budget = parsed["delivery_deadline"], parsed["target_budget"]
    dmin = to_min(dl)
    rec.update(parsed=parsed, engine=engine, deadline=dl, budget=budget, complete=bool(dl and budget))
    yield ev("step", agent="Requirement Agent", title="Extracted items, quantities, budget and deadline",
             lines=[f"Engine: {engine}"], code=json.dumps(parsed, indent=2)); nap()

    # Agent 3: Supplier & Price
    sups, lines = market(), []
    elig = []
    for s in sups:
        if s["quality"] < MIN_QUALITY:
            lines.append(f"{s['name']} skipped: quality {s['quality']}/5 is below the minimum {MIN_QUALITY}")
        elif dmin is not None and to_min(s["eta"]) > dmin:
            lines.append(f"{s['name']} skipped: earliest delivery {s['eta']} is after your deadline")
        else:
            elig.append(s)
    ok, rows, missing = [], [], []
    for i in parsed["items"]:
        c = [s for s in elig if feasible(s, i)]
        if not c:
            missing.append(i["item"]); continue
        ok.append(i)
        rows += [[i["item"], s["name"], f"{inr(s['live'][i['item']][0])}/{i['unit']}", f"{s['live'][i['item']][1]} {i['unit']}",
                  f"{s['quality']}/5", s["eta"]] for s in c]
    lines += [f"No eligible supplier can cover '{x}' in the requested quantity: dropped from the order" for x in missing]
    yield ev("step", agent="Supplier & Price Agent", title=f"Checked {len(sups)} suppliers: live price, stock, quality, delivery",
             lines=lines, table={"h": ["Item", "Supplier", "Price", "Stock", "Quality", "ETA"], "r": rows}); nap()
    if not ok:
        yield fail("No supplier can fulfil this order on time. Check item names, quantities or the deadline."); return

    # Agent 4: Negotiation
    cands = lambda i: [s for s in elig if feasible(s, i)]
    base = sum(min(s["live"][i["item"]][0] for s in cands(i)) * i["quantity"] for i in ok)
    # reference = what a typical single phone quote costs: average list prices + the average delivery fee
    ref = sum(sum(s["live"][i["item"]][0] for s in cands(i)) / len(cands(i)) * i["quantity"] for i in ok) \
        + sum(s["fee"] for s in elig) / len(elig)
    lines = [f"Cheapest list-price basket (before discounts and delivery): {inr(base)}" + (f" vs budget {inr(budget)}" if budget else " (no budget given)")]
    if budget:
        lines.append(f"Over budget by {inr(base - budget)}: negotiation needed" if base > budget
                     else f"Within budget by {inr(budget - base)}: optimising for max savings")
    lines.append("Rules: 5% off >=30kg (8% >=50kg), 2% bundle + free delivery above " + inr(3000) + ", per-supplier haggle room")
    carts_list, n = build_plans(ok, elig, 2)
    plans = [make_plan(c, budget, dmin) for c in carts_list]
    stretch = bool(budget) and min(p["total"] for p in plans) > budget
    if stretch:
        lines.append("Still over budget after round 1: asking suppliers to stretch to their limit, then re-ranking")
        carts_list, _ = build_plans(ok, elig, 3)
        plans = [make_plan(c, budget, dmin) for c in carts_list]
    plans = rank(plans)
    for p in plans:
        p["savings"] = round(ref - p["total"], 2)
    lines.append(f"Evaluated {n} item-to-supplier allocations, kept the best deal for each of {len(plans)} supplier combination(s)")
    yield ev("step", agent="Negotiation Agent", title="Built bundle offers and counteroffers", lines=lines); nap()
    th, how = threads(plans[0]["carts"], dl, stretch)
    for c in plans[0]["carts"]:
        yield ev("step", agent="Negotiation Agent", title=f"Negotiating with {c['supplier']}",
                 lines=[f"List {inr(c['list_total'])} -> final {inr(c['total'])} (messages: {how})"],
                 chat=th.get(c["supplier"], [])); nap()

    # Agent 5: Decision & Approval
    for p in plans:
        for c in p["carts"]:
            c["whatsapp"] = wa(c, qid, dl)
    top = plans[0]
    rec.update(status="pending", plans=plans, missing=missing, reference=round(ref, 2), baseline=round(base, 2),
               stretch=stretch, on_time_feasible=top["ontime"] and not missing, elapsed=round(time.time() - t0, 1))
    save_quote(rec)
    yield ev("step", agent="Decision & Approval Agent", title="Ranked the feasible deals",
             lines=[f"Recommended: {' + '.join(top['suppliers'])} at {inr(top['total'])}",
                    "Flow: retrieve data -> evaluate options -> draft counteroffers -> re-rank -> recommend",
                    "Nothing is sent to any supplier until you approve."],
             table={"h": ["Rank", "Suppliers", "Total", "Quality", "Arrives by", "Status"],
                    "r": [[f"#{p['rank']}", " + ".join(p["suppliers"]), inr(p["total"]), f"{p['quality']}/5", p["eta"],
                           "Feasible" if p["feasible"] else "Not feasible"] for p in plans]})
    yield ev("result", result=rec)


# ------------------------------------------------------------------ approval (the ONLY place supplier messages are sent)
def do_approve(qid, plan_idx):
    with LOCK:
        q = get_quote(qid)
        if not q or q["status"] == "failed":
            raise KeyError(qid)
        if q["status"] != "pending":
            return q                      # already decided: never dispatch twice
        if not 0 <= plan_idx < len(q["plans"]):
            raise IndexError(plan_idx)
        q.update(status="approved", chosen=plan_idx, approved_at=time.strftime("%F %T"))
        save_quote(q)
    demo, dispatch = digits(os.getenv("DEMO_WHATSAPP_TO")), []
    for c in q["plans"][plan_idx]["carts"]:
        to = demo or digits(c["phone"])   # supplier numbers are fake: DEMO_WHATSAPP_TO redirects them to your phone
        st, info = wa_send(to, c["whatsapp"], tag="supplier: " + c["supplier"])
        dispatch.append(dict(supplier=c["supplier"], to=to, status=st, detail=info))
    with LOCK:
        q = get_quote(qid)
        q["dispatch"] = dispatch
        save_quote(q)
    return q


def do_reject(qid):
    with LOCK:
        q = get_quote(qid)
        if not q or q["status"] == "failed":
            raise KeyError(qid)
        if q["status"] == "pending":
            q.update(status="rejected", rejected_at=time.strftime("%F %T"))
            save_quote(q)
        return q


def quote_msg(q):
    p, b = q["plans"][0], q.get("budget")
    L = [f"LocalVendor-Agent quote #{q['id']}", "Best deal: " + " + ".join(p["suppliers"]), f"Total: {inr(p['total'])}"]
    if b:
        L.append(f"Budget {inr(b)}: {inr(abs(p['budget_left']))} {'left' if p['within'] else 'over'} "
                 f"({abs(p['pct_below'])}% {'below' if p['within'] else 'above'} budget)")
    L += [f"Arrives by {p['eta']}", "", f"Review and approve: {base_url()}/?quote={q['id']}",
          "Or reply YES to approve / NO to reject. Nothing is ordered until you approve."]
    return "\n".join(L)


def inbound(sender, text, source):
    """A vendor message arrived over WhatsApp (real webhook or simulator). Returns the quote id if a new quote was made."""
    t, qid = text.strip(), LAST.get(sender)
    m = re.fullmatch(r"(yes|y|approve|confirm|ok|no|n|reject|cancel)[.! ]*", t, re.I)
    if m and qid and (get_quote(qid) or {}).get("status") == "pending":
        if m.group(1).lower() in ("yes", "y", "approve", "confirm", "ok"):
            do_approve(qid, 0)
            wa_send(sender, "Approved. Your orders were sent to the suppliers.", "vendor reply")
        else:
            do_reject(qid)
            wa_send(sender, "Rejected. Nothing was sent to any supplier.", "vendor reply")
        return qid
    result, err = None, None
    try:
        for chunk in pipeline(t, source, pace=False):
            e = json.loads(chunk)
            if e["type"] == "result":
                result = e["result"]
            elif e["type"] == "error":
                err = e["message"]
    except Exception as ex:
        err = f"Unexpected error: {ex}"
    if result:
        LAST[sender] = result["id"]
        wa_send(sender, quote_msg(result), "vendor reply")
        return result["id"]
    wa_send(sender, err or "Sorry, I could not process that order.", "vendor reply")
    return None


def handle_message(m):
    sender, kind = m.get("from", ""), m.get("type")
    try:
        if kind == "text":
            inbound(sender, m["text"]["body"], "whatsapp")
        elif kind == "audio":
            audio, mime = wa_media(m["audio"]["id"])
            inbound(sender, transcribe(audio, mime), "whatsapp-voice")
        else:
            wa_send(sender, "Please send your order as text or a voice note.", "vendor reply")
    except Exception as e:
        print("[webhook]", type(e).__name__, e)
        wa_send(sender, "Sorry, something went wrong while reading your message.", "vendor reply")


# ------------------------------------------------------------------ routes
class RunBody(BaseModel):
    text: str
    source: str = "text"


class ApproveBody(BaseModel):
    plan: int = 0


class SimBody(BaseModel):
    text: str


@app.post("/api/run")
def run(body: RunBody):
    text = body.text.strip()
    if not 3 <= len(text) <= 1000:
        raise HTTPException(400, "Enter an order between 3 and 1000 characters.")

    def gen():
        try:
            yield from pipeline(text, body.source if body.source in ("text", "voice") else "text")
        except Exception as e:                       # never kill the demo
            yield ev("error", message=f"Unexpected error: {e}")
    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/quotes/{qid}")
def quote(qid: str):
    q = get_quote(qid)
    if not q:
        raise HTTPException(404, "Quote not found. Run the agents again.")
    return q


@app.post("/api/quotes/{qid}/approve")
def approve(qid: str, body: ApproveBody):
    try:
        return do_approve(qid, body.plan)
    except (KeyError, IndexError):
        raise HTTPException(404, "Quote not found. Run the agents again.")


@app.post("/api/quotes/{qid}/reject")
def reject(qid: str):
    try:
        return do_reject(qid)
    except KeyError:
        raise HTTPException(404, "Quote not found. Run the agents again.")


@app.post("/api/whatsapp/simulate")
def simulate(body: SimBody):
    """Pretend the vendor sent this text from WhatsApp (same handler as the real webhook)."""
    if not 1 <= len(body.text.strip()) <= 1000:
        raise HTTPException(400, "Enter a message up to 1000 characters.")
    return {"quote_id": inbound("sim-vendor", body.text, "simulator")}


@app.get("/api/outbox")
def outbox():
    return list(OUTBOX)


@app.post("/api/shuffle")
def shuffle():
    with LOCK:
        db = read_json(SUP_FILE, SEED)
        db["market_tick"] += 1
        write_json(SUP_FILE, db)
    return {"tick": db["market_tick"]}


@app.get("/api/config")
def config():
    return dict(provider=provider(), whatsapp="cloud-api" if os.getenv("WHATSAPP_TOKEN") and os.getenv("WHATSAPP_PHONE_ID") else "simulator",
                wa_to=digits(os.getenv("DEMO_WHATSAPP_TO")), base_url=base_url())


# ------------------------------------------------------------------ success metrics (PPT slide 9)
SELFTEST = [
    ("Need 50kg tomatoes, 20kg onions and 10L milk by 6 AM tomorrow under \u20b93500", {"tomato": 50, "onion": 20, "milk": 10}, 3500, 360),
    ("I want 30 kg potatoes and 5 kg capsicum by 7:30 AM, budget of Rs 2000", {"potato": 30, "capsicum": 5}, 2000, 450),
    ("Need 12L milk, 5kg paneer by 5:30 AM under 3000 rupees", {"milk": 12, "paneer": 5}, 3000, 330),
    ("Order 100 kg onions by 6 AM tomorrow within \u20b92,500", {"onion": 100}, 2500, 360),
    ("Get me 8 kg tomatoes and 2 L milk by 6:30 AM under Rs. 600", {"tomato": 8, "milk": 2}, 600, 390),
    ("Need 25kg potatoes and 15kg tomatoes by 5 AM under \u20b91800", {"potato": 25, "tomato": 15}, 1800, 300)]


@app.get("/api/metrics")
def metrics():
    recs = list(read_json(QUOTE_FILE, {}).values())
    ok = [r for r in recs if r["status"] != "failed"]
    pct = lambda a, b: round(100 * a / b, 1) if b else None
    budgeted = [r for r in ok if r.get("budget")]
    timed = [r for r in recs if r.get("deadline")]
    n_sup = len(read_json(SUP_FILE, SEED)["suppliers"])
    manual = float(os.getenv("MANUAL_MIN_PER_CALL", "6")) * n_sup           # assumption: minutes per phone call
    avg = round(sum(r.get("elapsed", 0) for r in ok) / len(ok), 1) if ok else None
    return dict(
        runs=len(recs), quotes=len(ok),
        extraction_completeness=pct(sum(1 for r in recs if r.get("complete")), len(recs)),
        budget_compliance=pct(sum(1 for r in budgeted if r["plans"][0]["within"]), len(budgeted)),
        on_time_feasible=pct(sum(1 for r in timed if r.get("on_time_feasible")), len(timed)),
        approval_rate=pct(sum(1 for r in ok if r["status"] == "approved"), len(ok)),
        approved=sum(1 for r in ok if r["status"] == "approved"), rejected=sum(1 for r in ok if r["status"] == "rejected"),
        avg_savings=round(sum(r["plans"][0]["savings"] for r in ok) / len(ok), 2) if ok else None,
        avg_agent_seconds=avg, est_manual_minutes=manual)


@app.post("/api/selftest")
def selftest():
    """Order extraction accuracy: run the active parser on labelled requests and compare field by field."""
    cases, right, total = [], 0, 0
    for text, items, budget, deadline in SELFTEST:
        try:
            p, eng = parse_order(text)
        except Exception:
            p, eng = {"items": [], "target_budget": None, "delivery_deadline": None}, "error"
        got = {i["item"]: i["quantity"] for i in p["items"]}
        chk = {"items": got == items, "budget": p["target_budget"] == budget, "deadline": to_min(p["delivery_deadline"]) == deadline}
        right += sum(chk.values()); total += 3
        cases.append(dict(text=text, engine=eng, **chk))
    return dict(accuracy=round(100 * right / total, 1), cases=cases)


# ------------------------------------------------------------------ WhatsApp Cloud API webhook
@app.get("/webhook")
def webhook_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == os.getenv("WHATSAPP_VERIFY_TOKEN", "localvendor-verify"):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


@app.post("/webhook")
async def webhook_receive(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    secret = os.getenv("WHATSAPP_APP_SECRET")
    if secret:
        good = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("x-hub-signature-256", ""), good):
            raise HTTPException(403, "Bad signature")
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": False}
    for e in data.get("entry", []):
        for ch in e.get("changes", []):
            for m in ch.get("value", {}).get("messages") or []:
                bg.add_task(handle_message, m)       # reply in the background so Meta gets a fast 200
    return {"ok": True}


# ------------------------------------------------------------------ frontend
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
