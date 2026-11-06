"""Offline tests for the weekly briefing (Brevo). Run: python test_briefing.py"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.update({"BREVO_API_KEY": "k", "WEBHOOK_SECRET": "s", "TELEGRAM_CHAT_ID": "1"})

import briefing  # noqa: E402

UK = ZoneInfo("Europe/London")


class R:  # tiny Redis stand-in
    def __init__(self): self.kv, self.sets = {}, {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v): self.kv[k] = v
    def smembers(self, k): return self.sets.get(k, set())
    def sadd(self, k, v): self.sets.setdefault(k, set()).add(v); return 1


class S:
    def __init__(self): self.r = R()


class TG:
    def __init__(self): self.msgs = []
    def send_message(self, t): self.msgs.append(t)


class Resp:
    def __init__(self, code=200, body=None): self.status_code, self._b = code, body or {}
    ok = property(lambda self: self.status_code < 400)
    text = property(lambda self: json.dumps(self._b))
    def json(self): return self._b
    def raise_for_status(self): assert self.ok


calls = []


def fake_brevo(method, path, body=None):
    calls.append((method, path, body))
    if path.startswith("/contacts/lists?"): return Resp(200, {"lists": [{"id": 7, "name": "WLN Members"}]})
    if path == "/contacts/lists/7": return Resp(200, {"uniqueSubscribers": 169})
    if path == "/emailCampaigns" and method == "POST": return Resp(201, {"id": 555})
    return Resp(204)


briefing._brevo = fake_brevo
GOOD = {"subject": "Get your fit person interview right", "preheader": "What CQC listens for, and how to prepare.",
        "headline": "Walk into your fit person interview ready",
        "hook": "You know those managers who pass first time? It isn't luck, and it isn't a script. They prepare evidence.",
        "context": "CQC wants to hear how you would run the service — in your own words.",
        "steps": [{"title": "Know your plan.", "text": "Explain it."}, {"title": "Bring examples.", "text": "Real ones."},
                  {"title": "Practise aloud.", "text": "Twice."}],
        "closing": "I spent 15 years as a CQC Registered Manager.",
        "also": [{"id": "fit-person-practice", "line": "Ten minutes."}, {"id": "nope", "line": "x"}, {"id": "whatsapp", "line": "Ask."}]}

import ai  # noqa: E402
import sources  # noqa: E402

ai._ask = lambda s, u, max_tokens=0: json.dumps(GOOD)
pages = {"https://shor.by/fit-person-interview": "x" * 2000}
sources.page_text = lambda u, limit=18000: pages.get(u, "")

# 1. before the start date, and on non-Mondays: nothing happens
st, tg = S(), TG()
assert briefing.monday(st, tg, now=datetime(2026, 11, 9, 5, 0, tzinfo=UK)) == "not a briefing Monday"
assert briefing.monday(st, tg, now=datetime(2026, 12, 8, 5, 0, tzinfo=UK)) == "not a briefing Monday"
assert not tg.msgs

# 2. first Monday: campaign scheduled Tue 07:30 UK to the members list, Yeukai told, topic used
res = briefing.monday(st, tg, now=datetime(2026, 12, 7, 5, 0, tzinfo=UK))
assert res == "scheduled campaign 555", res
camp = next(b for m, p, b in calls if p == "/emailCampaigns")
assert camp["scheduledAt"] == "2026-12-08T07:30:00+00:00", camp["scheduledAt"]
assert camp["recipients"] == {"listIds": [7]} and camp["sender"]["email"] == "support@welllednetwork.com"
h = camp["htmlContent"]
for must in ("Founder, Well-Led Network", "https://shor.by/wln-work-with-me", "{{ unsubscribe }}", "contact.FIRSTNAME",
             "https://shor.by/fit-person-interview", "https://shor.by/fit-person-practice", "17556769", "wln-whatsapp"):
    assert must in h, must
assert "—" not in h and "nope" not in h
assert any(p == "/emailCampaigns/555/sendTest" for m, p, b in calls)
assert "briefing hold" in tg.msgs[-1] and "169 members" in tg.msgs[-1]
assert "fit-person-interview" in st.r.smembers(briefing.K_USED)

# 3. same Monday again: no second campaign
assert briefing.monday(st, tg, now=datetime(2026, 12, 7, 6, 0, tzinfo=UK)) == "already drafted this week"

# 4. hold and go
assert "Held" in briefing.handle("briefing hold", st)
assert ("PUT", "/emailCampaigns/555/status", {"status": "suspended"}) in calls
assert json.loads(st.r.get(briefing.K_WEEK + "2026-12-07"))["status"] == "held"
out = briefing.handle("Briefing go", st)
assert out.startswith("Back on") or out.startswith("Sending now"), out
assert "Weekly" in briefing.handle("briefing", st) or "briefing" in briefing.handle("briefing", st).lower()
assert briefing.handle("brief 12", st) is None and briefing.handle("1 3", st) is None
assert "/wln/members/" in briefing.handle("briefing setup", st)

# 5. next Monday: next article; unreadable pages are skipped and reported
pages["https://shor.by/care-home-audits"] = ""
pages["https://www.welllednetwork.com/blog/prepare-care-staff-for-cqc-interviews"] = "y" * 3000
calls.clear(); tg.msgs.clear()
assert briefing.monday(st, tg, now=datetime(2026, 12, 14, 5, 0, tzinfo=UK)) == "scheduled campaign 555"
assert "care-home-audits" in tg.msgs[-1]

# 6. members: bulk import, new signup, deleted account, junk
calls.clear(); tg.msgs.clear()
r = briefing.on_member_event(st, "", {"members": [{"email": "a@x.co.uk", "firstName": "Ann"}, {"email": "bad"}]}, tg.send_message)
assert r["added"] == 1 and r["total"] == 2 and "imported 1 of 2" in tg.msgs[-1]
added = [b for m, p, b in calls if p == "/contacts"]
assert added[0]["email"] == "a@x.co.uk" and added[0]["listIds"] == [7] and added[0]["attributes"]["FIRSTNAME"] == "Ann"
r = briefing.on_member_event(st, "new_sign_up", {"data": {"user": {"email": "B@Y.com", "name": "Bob Lee"}}}, tg.send_message)
assert r["result"] == "added"
assert [b for m, p, b in calls if p == "/contacts"][-1]["attributes"] == {"FIRSTNAME": "Bob", "LASTNAME": "Lee"}
r = briefing.on_member_event(st, "account_deleted", {"email": "b@y.com"}, tg.send_message)
assert r["result"] == "removed"
assert briefing.on_member_event(st, "", {"hello": 1}, tg.send_message)["result"].startswith("skipped")

# 7. an incomplete AI reply is refused rather than sent
ai._ask = lambda s, u, max_tokens=0: json.dumps({**GOOD, "steps": GOOD["steps"][:2]})
try:
    briefing.write("text", "https://x")
    raise AssertionError("should have refused")
except ValueError:
    pass

# 8. web routes: member token and preview
import web  # noqa: E402

web.store = st
c = web.app.test_client()
assert c.post("/wln/members/wrong", json={"email": "c@z.com"}).status_code == 404
resp = c.post(f"/wln/members/{briefing.member_token()}", json={"email": "c@z.com"},
              headers={"Origin": "https://www.welllednetwork.com"})
assert resp.status_code == 200 and resp.headers["Access-Control-Allow-Origin"] == "https://www.welllednetwork.com"
assert c.options(f"/wln/members/{briefing.member_token()}").status_code == 204
pv = c.get(f"/briefing/2026-12-07/{briefing.preview_token('2026-12-07')}")
assert pv.status_code == 200 and b"Hi Yeukai" in pv.data and b"{{" not in pv.data
assert c.get("/briefing/2026-12-07/nope").status_code == 404
print("BRIEFING TESTS PASSED")
