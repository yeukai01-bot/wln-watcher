"""Offline end-to-end test: fake sources, fake Claude, real pipeline logic."""
import json, os, sys, types
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["STATE_FILE"] = "/tmp/wln_state_test.json"
if os.path.exists("/tmp/wln_state_test.json"): os.remove("/tmp/wln_state_test.json")

import requests
now = datetime.now(timezone.utc)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
ATOM = f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>Oliver McGowan training: code of practice now in force</title><link href="https://www.gov.uk/guidance/omt-code"/><updated>{iso(now-timedelta(hours=5))}</updated><summary>Providers must ensure staff receive training.</summary></entry>
<entry><title>Old item</title><link href="https://www.gov.uk/old"/><updated>{iso(now-timedelta(days=9))}</updated><summary>x</summary></entry>
</feed>"""
LEG = f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>The Health and Social Care Act 2008 (Regulated Activities) (Amendment) Regulations 2026</title><link href="https://www.legislation.gov.uk/uksi/2026/999"/><updated>{iso(now-timedelta(hours=3))}</updated></entry><entry><title>The Road Traffic Order 2026</title><link href="https://www.legislation.gov.uk/uksi/2026/1000"/><updated>{iso(now)}</updated></entry></feed>"""
HTML = """<html><body><main><a href="/news/mandatory-training-requirement-learning-disability-and-autism">Mandatory training requirement on learning disability and autism</a><a href="/about">About us page link</a></main></body></html>"""
PAGE = "<html><body><main>" + ("<p>The code of practice says providers must train all staff. Tier 1 and Tier 2 apply.</p>" * 40) + "</main></body></html>"

class R:
    def __init__(s, body, status=200): s.content=body.encode(); s.text=body; s.status_code=status
    def raise_for_status(s):
        if s.status_code>=400: raise requests.HTTPError(str(s.status_code))
def fake_get(url, **kw):
    if "skillsforcare" in url: return R("", 503)          # prove one failing source doesn't stop the run
    if "legislation.gov.uk" in url: return R(LEG)
    if url.endswith(".atom") or ".atom?" in url: return R(ATOM)
    if "guidance/omt-code" in url: return R(PAGE)
    return R(HTML)
requests.get = fake_get

import ai, telegram_io as telegram, main
sent = []
telegram.send_message = lambda t: sent.append(("msg", t))
telegram.send_document = lambda f, c, cap="": sent.append(("doc", f, c, cap))
def fake_triage(items, existing):
    out=[]
    for i in items:
        if "Road" in i["title"]: continue
        hot = "McGowan" in i["title"] or "Mandatory" in i["title"]
        out.append({"id": i["id"], "urgency": 5 if hot else 2, "client_pull": 4 if hot else 1, "product": "Inspection Ready Registered Manager",
                    "controversy": "high" if "Regulated Activities" in i["title"] else "none", "controversy_note": "test",
                    "already_covered": "", "headline": "Oliver McGowan Training Is Now Mandatory: What Care Providers Must Do", "angle": "a", "lead_magnet": "b", "key_date": ""})
    return out
ai.triage = fake_triage
ai.draft = lambda *a, **k: "TITLE: X\n---\nBody with fact."
ai.fact_check = lambda md, src: {"supported": 5, "unsupported": 1, "controversy_flags": [], "corrected_markdown": md + "\n[checked]"}

assert main.run() == 0
msgs = [s for s in sent if s[0]=="msg"]; docs = [s for s in sent if s[0]=="doc"]
report = msgs[0][1]; print(report)
assert "Oliver McGowan Training Is Now Mandatory" in report
assert "Set aside for you" in report and "Regulated Activities" in report
assert "Skills for Care" in report and "failed" in report
assert "Old item" not in report and "Road Traffic" not in report
assert report.count("Oliver McGowan Training Is Now Mandatory")==1, "dedupe"
assert len(docs)==1 and docs[0][2].endswith("[checked]")
# second run: everything already seen
sent.clear(); main.run()
assert "No new developments" in sent[0][1], sent
print("\nALL TESTS PASSED")

# ---------------- two-way replies ----------------
os.environ.update({"WEBHOOK_SECRET": "s3cret", "TELEGRAM_CHAT_ID": "42", "APPROVALS_KEY": "k"})
ai.draft_and_check = lambda topic: ("# drafted " + topic["headline"], {"supported": 4, "unsupported": 0, "controversy_flags": []}, "")
import web, time as _t
web.telegram.send_message = lambda t: sent.append(("msg", t))
web.telegram.send_document = lambda f, c, cap="": sent.append(("doc", f, c, cap))
c = web.app.test_client()
def say(text, chat=42):
    sent.clear()
    r = c.post("/telegram/s3cret", json={"message": {"chat": {"id": chat}, "text": text}})
    assert r.status_code == 200
    _t.sleep(0.3)
    return [s[1] for s in sent if s[0] == "msg"]

# latest list saved by first run has topic 1 with a draft
assert web.store.latest()["topics"][0]["draft"].endswith("[checked]")
assert c.post("/telegram/wrong", json={}).status_code == 404               # wrong secret rejected
assert say("1", chat=999) == []                                              # strangers ignored
out = say("1 7")
assert "Approved for publishing" in out[0] and "Not on" in out[0], out
assert "already approved" in say("1")[0]
assert "Waiting to be published" in say("queue")[0]
assert c.get("/approvals").status_code == 403                               # key required
ap = c.get("/approvals", headers={"X-Key": "k"}).get_json()
assert len(ap) == 1 and ap[0]["draft"].endswith("[checked]")
r = c.post(f"/approvals/{ap[0]['id']}/done", headers={"X-Key": "k"}, json={"live_url": "https://www.welllednetwork.com/blog/x", "short_url": "shor.by/x"})
assert r.status_code == 200 and any("Live:" in s[1] for s in sent if s[0] == "msg")
assert c.get("/approvals", headers={"X-Key": "k"}).get_json() == []
assert "Nothing is waiting" in say("queue")[0]
# a topic without a draft gets drafted in the background
lst = web.store.latest(); lst["topics"].append({**lst["topics"][0], "number": 2, "headline": "Second topic", "draft": None}); web.store.save_latest(lst)
sent.clear(); c.post("/telegram/s3cret", json={"message": {"chat": {"id": 42}, "text": "2"}}); _t.sleep(1)
assert any(s[0] == "doc" and "Second topic" in s[3] for s in sent), sent
assert "Cancelled topic 2" in say("cancel 2")[0]
assert c.get("/approvals", headers={"X-Key": "k"}).get_json() == []
assert "did not understand" in say("hello there")[0]
print("REPLY SERVICE TESTS PASSED")
