"""Call-me requests: a person asks for a call about their CQC report, Grace (Dialora) rings them.

Flow
  1. The form at https://wln-replies.onrender.com/call-me (or the same form embedded on welllednetwork.com)
     posts here. The person must tick a consent box that names an automated call from Grace.
  2. Inside calling hours we ask Dialora to ring now: POST to the "Grace Callbacks" agent's webhook.
     Outside calling hours the request waits in a queue and goes out at the next opening time. The queue
     is checked every 10 minutes by the keep-awake ping (web.py health()).
  3. Yeukai gets a Telegram alert for every request, and when the call is placed.

Safety
  - No consent tick, no call. Empty honeypot field required. One call per phone number per 24 hours.
  - Calling hours (UK): Mon-Fri 09:00-19:30, Sat 10:00-16:00. Never Sunday.
  - Telegram "callme pause" stops all calls (requests are still saved and alerted); "callme resume" restarts.

Render settings (wln-secrets)
  DIALORA_CALLBACK_WEBHOOK   the Grace Callbacks agent webhook URL (Dialora > agent > Advanced Settings >
                             Call Management). Keep it out of the code: anyone with it can trigger calls.

Telegram commands
  callme           status: paused or live, waiting queue, last requests
  callme pause     stop placing calls
  callme resume    start placing calls again
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

UK = ZoneInfo("Europe/London")
K_QUEUE = "callme:queue"        # list of JSON requests waiting for calling hours
K_LOG = "callme:log"            # list of recent requests (newest first, capped)
K_RECENT = "callme:recent:"     # + phone -> set for 24h after a call is placed
K_PAUSED = "callme:paused"

CONSENT_TEXT = ("I agree to receive an automated phone call from Grace, the Well-Led Network's AI assistant, "
                "on the number above about my request. I can ask her to stop at any time.")


# ---------------------------------------------------------------- helpers
def _r(store):
    return getattr(store, "r", None)


def _clean(v, n=300) -> str:
    v = str(v or "").replace("\r", " ").replace("\n", " ").strip()
    return v[:n]


def normalise_phone(raw: str) -> str | None:
    """UK numbers to +44 form. Returns None if it does not look like a real phone number."""
    p = re.sub(r"[^\d+]", "", str(raw or ""))
    if p.startswith("00"):
        p = "+" + p[2:]
    if p.startswith("0"):
        p = "+44" + p[1:]
    elif p.startswith("44"):
        p = "+" + p
    elif not p.startswith("+"):
        return None
    digits = re.sub(r"\D", "", p)
    if not 10 <= len(digits) <= 15:
        return None
    if p.startswith("+44") and len(digits) != 12:
        return None
    return p


def in_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(UK)
    t = now.hour * 60 + now.minute
    if now.weekday() < 5:
        return 9 * 60 <= t <= 19 * 60 + 30
    if now.weekday() == 5:
        return 10 * 60 <= t <= 16 * 60
    return False


def next_opening(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UK)
    for i in range(8):
        d = now + timedelta(days=i)
        if d.weekday() == 6:
            continue
        opens = d.replace(hour=10 if d.weekday() == 5 else 9, minute=0, second=0, microsecond=0)
        closes = d.replace(hour=16 if d.weekday() == 5 else 19, minute=0 if d.weekday() == 5 else 30,
                           second=0, microsecond=0)
        if now < closes:
            return max(now, opens)
    return now + timedelta(days=1)


def _when_text(dt: datetime) -> str:
    now = datetime.now(UK)
    day = "today" if dt.date() == now.date() else ("tomorrow" if dt.date() == (now + timedelta(days=1)).date()
                                                    else dt.strftime("%A"))
    return f"{day} from {dt.strftime('%-I%p').lower().replace(':00', '')}"


def _log(store, item: dict) -> None:
    r = _r(store)
    if r:
        r.lpush(K_LOG, json.dumps(item))
        r.ltrim(K_LOG, 0, 49)


def paused(store) -> bool:
    r = _r(store)
    return bool(r and r.get(K_PAUSED))


# ---------------------------------------------------------------- placing the call
def _place(store, req: dict, notify) -> tuple[bool, str]:
    url = os.getenv("DIALORA_CALLBACK_WEBHOOK", "").strip()
    if not url:
        return False, "DIALORA_CALLBACK_WEBHOOK is not set in Render"
    r = _r(store)
    if r and r.get(K_RECENT + req["phone"]):
        return False, "already called this number in the last 24 hours"
    body = {
        "name": req["name"], "phone": req["phone"], "email": req.get("email", ""),
        "service": req.get("service") or "not given", "reason": req.get("reason") or "not given",
        "source": req.get("source") or "call-me form",
        "consent": "Ticked: " + CONSENT_TEXT,
    }
    if not body["email"]:
        body.pop("email")
    try:
        resp = requests.post(url, json=body, timeout=30)
    except Exception as exc:
        return False, f"Dialora did not answer ({type(exc).__name__})"
    if resp.status_code >= 300:
        return False, f"Dialora refused it ({resp.status_code}: {resp.text[:150]})"
    if r:
        r.setex(K_RECENT + req["phone"], 24 * 3600, "1")
    return True, "call requested"


def _alert(notify, req: dict, status: str) -> None:
    notify(
        "CALL-ME REQUEST\n\n"
        f"{req['name']} ({req.get('service') or 'no service given'})\n"
        f"Phone: {req['phone']}\nEmail: {req.get('email') or 'none'}\n"
        f"Reason: {req.get('reason') or 'none'}\nSource: {req.get('source') or 'call-me form'}\n\n"
        f"Status: {status}"
    )


def handle(store, payload: dict, notify) -> dict:
    """Process one form submission. Returns what the page shows."""
    p = payload or {}
    if _clean(p.get("website")):          # honeypot filled: a bot. Pretend all is well.
        return {"ok": True, "message": "Thank you."}
    name = _clean(p.get("name"), 80)
    phone = normalise_phone(p.get("phone"))
    consent = str(p.get("consent", "")).lower() in ("yes", "on", "true", "1")
    if not name or not phone:
        return {"ok": False, "message": "Please give your name and a UK phone number we can call."}
    if not consent:
        return {"ok": False, "message": "Please tick the box to say Grace may call you."}
    email = _clean(p.get("email"), 120)
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        email = ""
    req = {
        "name": name, "phone": phone, "email": email,
        "service": _clean(p.get("service"), 120), "reason": _clean(p.get("reason"), 400),
        "source": _clean(p.get("source"), 60) or "call-me form",
        "consent": CONSENT_TEXT, "at": datetime.now(UK).isoformat(timespec="seconds"),
    }
    _log(store, req)

    if paused(store):
        _queue(store, req)
        _alert(notify, req, "calls are PAUSED (callme resume to send). Request saved.")
        return {"ok": True, "message": "Thank you. We'll be in touch shortly."}
    if not in_hours():
        when = next_opening()
        _queue(store, req)
        _alert(notify, req, f"outside calling hours, Grace will ring {_when_text(when)}")
        return {"ok": True, "message": f"Thank you, {name.split()[0]}. Grace will call you {_when_text(when)}."}
    ok, why = _place(store, req, notify)
    _alert(notify, req, "Grace is ringing now" if ok else f"NOT called: {why}")
    if ok:
        return {"ok": True, "message": f"Thank you, {name.split()[0]}. Grace will ring you in the next few minutes "
                                       f"from a Well-Led Network number."}
    return {"ok": True, "message": "Thank you. We've got your request and will call you shortly."}


def _queue(store, req: dict) -> None:
    r = _r(store)
    if r:
        r.rpush(K_QUEUE, json.dumps(req))


def flush(store, notify) -> int:
    """Send waiting requests once calling hours open. Called from the 10-minute keep-awake ping."""
    r = _r(store)
    if not r or paused(store) or not in_hours():
        return 0
    sent = 0
    for _ in range(10):
        raw = r.lpop(K_QUEUE)
        if not raw:
            break
        req = json.loads(raw)
        ok, why = _place(store, req, notify)
        notify(f"Queued call-me request for {req['name']} ({req['phone']}): "
               + ("Grace is ringing now." if ok else f"NOT called: {why}"))
        sent += ok
        time.sleep(2)
    return sent


def command(text: str, store) -> str | None:
    t = text.strip().lower()
    if not t.startswith("callme"):
        return None
    r = _r(store)
    if t == "callme pause":
        if r:
            r.set(K_PAUSED, "1")
        return "Call-me calls are PAUSED. Requests are still saved and alerted. Send 'callme resume' to restart."
    if t == "callme resume":
        if r:
            r.delete(K_PAUSED)
        return "Call-me calls are LIVE again. Waiting requests go out at the next 10-minute check in calling hours."
    waiting = r.llen(K_QUEUE) if r else 0
    recent = [json.loads(x) for x in (r.lrange(K_LOG, 0, 4) if r else [])]
    lines = [f"Call-me: {'PAUSED' if paused(store) else 'LIVE'}, {waiting} waiting, "
             f"webhook {'set' if os.getenv('DIALORA_CALLBACK_WEBHOOK') else 'NOT SET'}, "
             f"calling hours {'open' if in_hours() else 'closed'} now."]
    for x in recent:
        lines.append(f"- {x['at'][:16].replace('T', ' ')} {x['name']} {x['phone']} ({x.get('service') or '-'})")
    return "\n".join(lines)


# ---------------------------------------------------------------- the page
def page(action: str = "/call-me") -> str:
    e = html.escape
    return PAGE.replace("{{ACTION}}", e(action)).replace("{{CONSENT}}", e(CONSENT_TEXT))


PAGE = """<!DOCTYPE html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Talk to us about your CQC report</title>
<meta name="robots" content="noindex">
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--navy:#0f2a44;--teal:#0b5d63;--ink:#14161a;--muted:#4a5563;--line:#d6dde4;--bg:#f5f7f9;--accent:#c8553d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 Inter,system-ui,sans-serif}
header{background:var(--navy);color:#fff;padding:18px max(5vw,16px);display:flex;align-items:center;gap:14px}
.mark{font:600 20px/1 'Source Serif 4',Georgia,serif;letter-spacing:.5px;border:2px solid #fff;padding:6px 8px;border-radius:4px}
header span{opacity:.9}
main{max-width:1080px;margin:0 auto;padding:clamp(24px,4vw,56px) max(5vw,16px);display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:clamp(24px,4vw,56px)}
h1{font:600 clamp(30px,4.4vw,44px)/1.08 'Source Serif 4',Georgia,serif;margin:0 0 16px;text-wrap:balance;color:var(--navy)}
.lead{font-size:18px;color:var(--muted);margin:0 0 22px}
.steps{list-style:none;padding:0;margin:0 0 24px;display:grid;gap:12px}
.steps li{display:grid;grid-template-columns:34px minmax(0,1fr);gap:12px;align-items:start}
.steps b{display:grid;place-items:center;width:34px;height:34px;border-radius:50%;background:var(--teal);color:#fff;font-weight:600}
.who{border-top:1px solid var(--line);padding-top:18px;color:var(--muted);font-size:15px}
form{background:#fff;border:1px solid var(--line);border-radius:10px;padding:clamp(20px,3vw,32px);box-shadow:0 10px 30px rgba(15,42,68,.08)}
label{display:block;font-weight:600;margin:0 0 6px}.opt{font-weight:400;color:var(--muted)}
input,select,textarea{width:100%;font:16px Inter,system-ui,sans-serif;padding:12px 14px;border:1px solid #b9c3cd;border-radius:6px;background:#fff;color:var(--ink);margin-bottom:16px}
textarea{min-height:84px;resize:vertical}
input:focus,select:focus,textarea:focus{outline:3px solid rgba(11,93,99,.35);border-color:var(--teal)}
.consent{display:grid;grid-template-columns:24px minmax(0,1fr);gap:10px;align-items:start;background:#eef5f5;border-radius:6px;padding:12px;margin-bottom:18px;font-size:15px}
.consent input{width:22px;height:22px;margin:2px 0 0}
button{width:100%;min-height:52px;border:0;border-radius:6px;background:var(--accent);color:#fff;font:600 17px Inter,system-ui,sans-serif;cursor:pointer}
button:disabled{opacity:.6}
.msg{margin-top:14px;font-weight:500}.msg.err{color:#a3261b}.msg.ok{color:var(--teal)}
.hp{position:absolute;left:-9999px}
.small{font-size:13px;color:var(--muted);margin-top:12px}
footer{background:var(--navy);color:#dfe7ee;padding:22px max(5vw,16px);font-size:14px}
footer a{color:#fff}
@media(max-width:760px){main{grid-template-columns:minmax(0,1fr)}}
</style></head><body>
<header><div class="mark">WLN</div><span>Well-Led Network</span></header>
<main>
<section>
<h1>Talk your CQC report through with someone who has been there</h1>
<p class="lead">Leave your details and Grace, our AI assistant, will ring you to understand where you are and book a free 30-minute call with Yeukai Kajidori.</p>
<ul class="steps">
<li><b>1</b><span>Grace calls you, usually within minutes in working hours, and asks a few short questions about your report.</span></li>
<li><b>2</b><span>She books a free, no-obligation 30-minute call with Yeukai at a time that suits you.</span></li>
<li><b>3</b><span>On that call you'll get a clear view of what the findings mean and where to start first.</span></li>
</ul>
<p class="who">Yeukai spent fifteen years as a CQC Registered Manager and has twenty-three years in UK health and social care. He founded the Well-Led Network to help providers get back to Good and stay there.</p>
</section>
<form id="f" method="post" action="{{ACTION}}" novalidate>
<label for="name">Your name</label><input id="name" name="name" autocomplete="name" required>
<label for="phone">Best phone number</label><input id="phone" name="phone" type="tel" autocomplete="tel" inputmode="tel" required>
<label for="email">Email <span class="opt">(for the booking confirmation)</span></label><input id="email" name="email" type="email" autocomplete="email">
<label for="service">Service name and type <span class="opt">(e.g. Oak House, supported living)</span></label><input id="service" name="service">
<label for="reason">What would you like to talk about? <span class="opt">(optional)</span></label>
<textarea id="reason" name="reason" placeholder="For example: we were rated Requires Improvement in Safe and Well-led last month."></textarea>
<input class="hp" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">
<input type="hidden" name="source" id="source" value="call-me page">
<div class="consent"><input type="checkbox" id="consent" name="consent" value="yes" required><label for="consent" style="font-weight:400;margin:0">{{CONSENT}}</label></div>
<button id="b" type="submit">Ask Grace to call me</button>
<p class="msg" id="m" role="status" aria-live="polite"></p>
<p class="small">Calls are made Monday to Friday 9am to 7:30pm and Saturday 10am to 4pm. Grace will always say she is an AI assistant. We only use your details to arrange this call.</p>
</form>
</main>
<footer>Well-Led Network is a trading name of The Kajidori Collective Ltd, registered in England and Wales, company no. 17556769. More free tools: <a href="https://www.welllednetwork.com/blog">https://www.welllednetwork.com/blog</a></footer>
<script>
{const f=document.getElementById('f'),b=document.getElementById('b'),m=document.getElementById('m');
const q=new URLSearchParams(location.search);if(q.get('src'))document.getElementById('source').value=q.get('src').slice(0,60);
f.addEventListener('submit',async ev=>{ev.preventDefault();m.className='msg';m.textContent='';
if(!f.name.value.trim()||!f.phone.value.trim()){m.className='msg err';m.textContent='Please give your name and phone number.';return}
if(!f.consent.checked){m.className='msg err';m.textContent='Please tick the box to say Grace may call you.';return}
b.disabled=true;b.textContent='Sending...';
try{const r=await fetch(f.action,{method:'POST',body:new FormData(f)});const j=await r.json();
m.className='msg '+(j.ok?'ok':'err');m.textContent=j.message||'Thank you.';if(j.ok){f.reset();b.textContent='Request sent';return}}
catch(e){m.className='msg err';m.textContent='Something went wrong. Please call us on 01865 530324.'}
b.disabled=false;b.textContent='Ask Grace to call me';});}
</script></body></html>"""
