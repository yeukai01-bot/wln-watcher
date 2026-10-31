"""Offline tests for Expert Session follow-ups (approved but not booked) and guest email replies."""
import json, os, sys
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["CLUSTDOC_API_TOKEN"] = "t"
import expert_sessions as es


class FakeRedis:
    def __init__(s): s.h = {}
    def hget(s, k, f): return s.h.get(k, {}).get(f)
    def hset(s, k, f, v): s.h.setdefault(k, {})[f] = v
    def hdel(s, k, f): s.h.get(k, {}).pop(f, None)
    def hexists(s, k, f): return f in s.h.get(k, {})
    def hgetall(s, k): return dict(s.h.get(k, {}))


class Store:
    def __init__(s): s.r = FakeRedis()


class Resp:
    def __init__(s, data, code=200): s._d, s.status_code = data, code
    def json(s): return s._d


APPS = {"101": {"id": 101, "contact": {"firstname": "Kevin", "lastname": "Humphrys", "email": "ceo@oaklandcare.co.uk"}},
        "102": {"id": 102, "contact": {"firstname": "Ann", "lastname": "Lee", "email": "ann@care.co.uk"}}}
es._cd = lambda m, p, **kw: Resp({"data": APPS[p.rsplit("/", 1)[1]]}) if p.rsplit("/", 1)[1] in APPS else Resp({}, 404)
emails, msgs = [], []
es._email = lambda c, subj, text: emails.append((c["email"], subj)) or "emailed"
es._tag = lambda *a: None
send = msgs.append

st = Store()
UK = es.UK
now = datetime.now(UK)
st.r.hset(es.DONE, "101", "approved " + (now - timedelta(days=4)).strftime("%d %b %H:%M"))  # old-style, no APPROVED_AT
st.r.hset(es.DONE, "102", "approved x")
st.r.hset(es.APPROVED_AT, "102", (now - timedelta(days=1)).isoformat())
st.r.hset(es.DONE, "103", "declined 1 Nov 10:00")

# day 4: Kevin gets nudge 1 (parsed from the old DONE text), Ann (1 day) nothing, declined ignored
out = es.chase(st, send, force=True)
assert out == ["chase1 101"], out
assert emails == [("ceo@oaklandcare.co.uk", "Your Expert Session: pick your Thursday")], emails
assert "guest stop 101" in msgs[-1]
# same run again: nothing more
assert es.chase(st, send, force=True) == []

# day 8: nudge 2; day 11: Telegram to Yeukai, no third email
st.r.hset(es.APPROVED_AT, "101", (now - timedelta(days=8)).isoformat())
assert es.chase(st, send, force=True) == ["chase2 101"] and emails[-1][1].startswith("Last reminder")
st.r.hset(es.APPROVED_AT, "101", (now - timedelta(days=11)).isoformat())
n_mail = len(emails)
assert es.chase(st, send, force=True) == ["chase3 101"] and len(emails) == n_mail
assert "Worth a quick call" in msgs[-1]
assert es.chase(st, send, force=True) == []

# a booking in the calendar matched by email stops the follow-ups
st.r.hset(es.APPROVED_AT, "102", (now - timedelta(days=5)).isoformat())
st.r.hset("wln:expert_booked", "uid1", json.dumps({"start": "x", "name": "Ann Lee", "email": "ANN@care.co.uk"}))
assert es.chase(st, send, force=True) == [] and st.r.hexists(es.BOOKED_APPS, "102")
print("CHASE TESTS PASSED")

# weekend / night: no follow-ups unless forced
st2 = Store()
st2.r.hset(es.DONE, "102", "approved x"); st2.r.hset(es.APPROVED_AT, "102", (now - timedelta(days=4)).isoformat())
real_now = es.datetime
class Sat(datetime):
    @classmethod
    def now(cls, tz=None): return datetime(2026, 10, 31, 12, 0, tzinfo=tz)
es.datetime = Sat
assert es.chase(st2, send) == []
es.datetime = real_now
print("QUIET HOURS TEST PASSED")

# guest email replies
st3 = Store()
st3.r.hset(es.DONE, "101", "approved 31 Oct 17:54")
assert es.on_guest_reply(st3, "Kevin <ceo@oaklandcare.co.uk>", "Re: x", "Please put me on the waiting list\n\nOn Sat, Yeukai wrote:\n> book here", send) == "guest waitlisted"
assert st3.r.hexists("wln:expert_waitlist", "101")
st4 = Store()
st4.r.hset(es.DONE, "101", "approved 31 Oct 17:54")
r = es.on_guest_reply(st4, "ceo@oaklandcare.co.uk", "Re: x", "5 November works for me\n\nOn Sat Yeukai wrote:\n> waiting list", send)
assert r == "guest reply" and json.loads(st4.r.hget(es.CHASE, "101"))["stop"] is True, r
assert "5 November works for me" in msgs[-1] and "waiting list" not in msgs[-1].split("They said:")[1].split("\n")[0]
assert es.chase(st4, send, force=True) == []  # paused
assert es.handle("guest restart 101", st4).startswith("Follow-ups restarted")
assert json.loads(st4.r.hget(es.CHASE, "101")) == {"stop": False, "n": 0}
assert es.on_guest_reply(st4, "stranger@x.com", "hi", "waiting list", send) == ""
assert es.handle("guest booked 101", st4).startswith("Noted") and st4.r.hexists(es.BOOKED_APPS, "101")
assert "101: booked" in es.handle("approved", st4)
print("GUEST REPLY TESTS PASSED")
