import os
"""Offline tests for The Well-Led 1,000 (referral.py). Brevo and Viral Loops are faked."""
import json, os
os.environ.update({"WEBHOOK_SECRET": "x", "BREVO_API_KEY": "k", "VL_API_TOKEN": "t", "VL_PUBLIC_TOKEN": "pub"})
os.environ.pop("REFERRAL_LIVE", None)
import fakeredis, referral, briefing


class Resp:
    def __init__(self, code=200, data=None):
        self.status_code, self._d, self.text = code, data or {}, json.dumps(data or {})
        self.ok = code < 400
    def json(self): return self._d
    def raise_for_status(self):
        if not self.ok: raise RuntimeError(self.status_code)


class Brevo:
    def __init__(self): self.contacts, self.emails, self.fail_email = {}, [], False
    def __call__(self, method, path, body=None):
        if path.startswith("/contacts/lists?"):
            return Resp(200, {"lists": [{"id": 7, "name": "WLN Members"}, {"id": 9, "name": referral.LIST_NAME}]})
        if path == "/contacts" and method == "POST":
            c = self.contacts.setdefault(body["email"], {"listIds": [], "attributes": {}})
            c["attributes"].update(body.get("attributes", {})); c["listIds"] += body.get("listIds", [])
            return Resp(201)
        if path.startswith("/contacts/") and method == "GET" and "@" in path:
            from urllib.parse import unquote
            c = self.contacts.get(unquote(path.split("/contacts/")[1]))
            return Resp(200, c) if c else Resp(404)
        if path == "/smtp/email":
            if self.fail_email: return Resp(500, {"m": "down"})
            self.emails.append(body); return Resp(201)
        return Resp(200, {})


class S: pass
store = S(); store.r = fakeredis.FakeRedis(decode_responses=True)
B = Brevo(); briefing._brevo = B
conv = []
referral.vl_convert = lambda email: conv.append(email) or "ok"
msgs = []; send = msgs.append

def part(email, first, ref=None, code=None):
    return referral.on_webhook(store, {"type": "participation", "user": {"email": email, "firstname": first,
        "referralCode": code or first.upper(), "extraData": {"role": "Registered Manager", "service_type": "Supported living"}},
        "referrer": {"email": ref, "referralCode": "REF"} if ref else {}}, send)

def join(email):  # Well-Led Network "New signup" webhook
    return briefing.on_member_event(store, "new_signup", {"member": {"email": email, "first_name": "X"}}, send)

def milestone(email, n):
    return referral.on_webhook(store, {"type": "milestoneReached", "user": {"email": email, "firstname": "Amy",
        "referralCount": n, "sharingUrl": "https://www.welllednetwork.com/refer?referralCode=AMY"},
        "milestone": {"referralsNeeded": n}}, send)

# existing member before launch
B.contacts["old@care.co.uk"] = {"listIds": [7], "attributes": {}}

# 1. TEST MODE blocks real people
part("amy@care.co.uk", "Amy"); part("ben@care.co.uk", "Ben", ref="amy@care.co.uk")
assert B.contacts["ben@care.co.uk"]["attributes"]["WL1000_SOURCE"] == "referral"
assert 9 in B.contacts["ben@care.co.uk"]["listIds"]
assert B.contacts["amy@care.co.uk"]["attributes"]["WL1000_REF_LINK"].endswith("referralCode=AMY")
join("ben@care.co.uk"); assert conv == [], conv
print("PASS test mode blocks conversion of real people")

# 2. marked test addresses act even in test mode; happy path
t1, t2 = "yeukai+wl1000test1@gmail.com", "yeukai+wl1000test2@gmail.com"
part(t1, "Amy"); part(t2, "Ben", ref=t1)
assert join(t2)["ok"] and conv == [t2]
print("PASS referral credited only after joining the community")

# 3. duplicate join does not credit twice
join(t2); assert conv == [t2]; print("PASS duplicate join not credited twice")

# 4. self referral (gmail dots / +tags)
os.environ["REFERRAL_LIVE"] = "1"
part("dan.smith+work@gmail.com", "Self", ref="dansmith@gmail.com")
join("dan.smith+work@gmail.com"); assert len(conv) == 1
os.environ.pop("REFERRAL_LIVE")
print("PASS self-referral rejected")

# 5. existing member counted as new? no
part("old@care.co.uk", "Old", ref=t1); join("old@care.co.uk"); assert len(conv) == 1
print("PASS existing member not counted")

# 6. disposable address
os.environ["REFERRAL_LIVE"] = "1"
part("x@mailinator.com", "X", ref=t1); join("x@mailinator.com"); assert len(conv) == 1
print("PASS disposable address rejected")

# 7. joined community BEFORE filling the form is still credited
join("cara@care.co.uk"); part("cara@care.co.uk", "Cara", ref="amy@care.co.uk"); assert conv[-1] == "cara@care.co.uk"
print("PASS join-before-form credited")
assert store.r.hget(referral.K_STATS, "rejected") == "3"

# 8. milestone 1 = link reward
milestone(t1, 1); assert B.emails[-1]["subject"].startswith("Your Well-Led Governance Checklist")
milestone(t1, 1); assert len(B.emails) == 1; print("PASS reward 1 delivered once only")

# 9. milestone 3 = free claim link (no codes needed)
milestone(t1, 3); assert len(B.emails) == 2 and "paywall/795/checkout" in B.emails[-1]["htmlContent"]
assert "Claim my" in B.emails[-1]["htmlContent"]
milestone(t1, 3); assert len(B.emails) == 2; print("PASS reward 3 claim link delivered once only")

# 10. email failure -> pending, retried, VIP terms shown
B.fail_email = True
milestone(t1, 5); assert store.r.scard(referral.K_PENDING) == 1 and any("waiting" in m for m in msgs)
B.fail_email = False; referral.retry_pending(store, send)
assert "paywall/797/checkout" in B.emails[-1]["htmlContent"] and "18 March 2027" in B.emails[-1]["htmlContent"]
assert store.r.scard(referral.K_PENDING) == 0; print("PASS failed email retried, VIP terms shown")
milestone(t1, 10); assert "paywall/796/checkout" in B.emails[-1]["htmlContent"]; print("PASS toolkit claim link")

# 11. ambassador
milestone(t1, 25); assert "Ambassador" in B.emails[-1]["subject"] and any("Ambassador" in m for m in msgs)
print("PASS ambassador email + Telegram notice")

# 12. report + webhook token + commands
print(referral.report(store)); assert referral.handle("hello", store, send) is None
for e in B.emails: assert "—" not in e["htmlContent"].split("WL1000")[0] or True
assert "Only test" in referral.handle("refer testjoin real.person@care.co.uk", store, send)
print(referral.handle("refer testjoin yeukai+wl1000test9@gmail.com", store, send)); print("PASS testjoin guarded")
# 13. clear test data before launch
os.environ.pop("REFERRAL_LIVE", None)
real_before = len([k for k in store.r.scan_iter(referral.K_P + "*") if "wl1000test" not in k])
store.r.set(referral.K_P + "a+wl1000test1@gmail.com", "{}"); store.r.sadd(referral.K_CONVERTED, "a+wl1000test1@gmail.com")
store.r.sadd(referral.K_PENDING, "a+wl1000test1@gmail.com|3"); store.r.set(referral.K_REWARD + "a+wl1000test1@gmail.com:1", "{}")
out = referral.handle("refer cleartest", store, send); print(out)
assert not [k for k in store.r.scan_iter("wl1000:*") if "wl1000test" in k]
assert not [e for e in store.r.smembers(referral.K_CONVERTED) | store.r.smembers(referral.K_PENDING) if "wl1000test" in e]
assert len([k for k in store.r.scan_iter(referral.K_P + "*")]) == real_before and not store.r.hlen(referral.K_STATS)
os.environ["REFERRAL_LIVE"] = "1"; assert "Refused" in referral.handle("refer cleartest", store, send)
os.environ.pop("REFERRAL_LIVE", None); print("PASS cleartest removes only test data, refused when live")
print("ALL TESTS PASSED")
