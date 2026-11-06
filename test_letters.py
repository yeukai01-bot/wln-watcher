"""Offline test of the Stannp radar letters: builds the PDF, lists, holds, caps and posts with Stannp mocked.
Run: python test_letters.py  (no network, no keys needed; writes test_letter.pdf to look at)."""
import os
import sys
import tempfile
from unittest import mock

os.environ.update({"STATE_FILE": os.path.join(tempfile.mkdtemp(), "state.json"), "APPROVALS_KEY": "k",
                   "STANNP_API_KEY": "test-key", "LETTERS_CAP": "5", "LETTERS_START": "2026-01-01"})
os.environ.pop("REDIS_URL", None)

import letter_pdf  # noqa: E402
import letters  # noqa: E402
from store import Store  # noqa: E402

REVIEW = {
    "found": ["(Well-led) The governance breach continues and a second warning notice was served.",
              "(Effective) Training gaps were known but not acted on.",
              "(Safe) Staff giving medicines had not had competency checks."],
    "working": "Relatives describe kind staff and better communication, and care plans now give a clearer picture of needs.",
    "underneath": ["The pattern suggests audits are planned but not yet finding problems on their own.",
                   "Training records are out of date, so gaps stay invisible."],
    "fix_first": ["Close the training gaps", "Check medicines competency", "Make audits find problems"],
    "evidence": ["Training matrix all green", "Competency records for every medicines giver"],
    "fixes": [{"title": "Close the known training gaps", "why": "CQC found the gaps were known but not acted on.",
               "steps": ["List every gap with a date.", "Book the courses this week.", "Check certificates on file."],
               "proof": "The matrix shows no red by the end of the month."}] * 3,
    "questions": ["Show me your training matrix.", "How do you know staff giving medicines are competent?",
                  "What did your last audit find?"],
}


def prospect(i, **k):
    p = {"id": f"p{i}", "location_id": f"1-{i}", "location_name": f"Test Home {i}", "rating": "Requires improvement",
         "report_date": "4 November 2026", "registered_manager": "JULIA ECCLESTON", "address_lines": ["132 Albany Road", "Redruth", "Cornwall"],
         "postcode": "TR15 2HZ", "radar_date": "2026-11-07", "review": REVIEW, "serious_notice": "", "created": i}
    p.update(k)
    return p


def main():
    pdf, pages = letter_pdf.build_pdf(prospect(1))
    open("test_letter.pdf", "wb").write(pdf)
    assert pdf.startswith(b"%PDF") and 2 <= pages <= 4, pages
    old, _ = letter_pdf.build_pdf(prospect(9, report_date="1 September 2026"))
    assert old.startswith(b"%PDF")

    s = Store()
    s.put_prospect(prospect(1))
    s.put_prospect(prospect(2, serious_notice="We urgently suspended the provider until 15 January 2027."))
    s.put_prospect(prospect(3, large_org=True))
    s.put_prospect(prospect(4))
    msg = letters.list_letters(s, "https://x")
    assert "L1. Test Home 1" in msg and "HELD" in msg and "Left out (large organisation): Test Home 3" in msg, msg

    calls = []

    def fake_post(url, auth=None, data=None, files=None, timeout=None):
        calls.append(data)
        r = mock.Mock(status_code=200)
        r.json.return_value = {"success": True, "data": {"id": 99, "cost": "1.90", "pdf": "https://proof"}}
        return r

    with mock.patch.object(letters.requests, "post", fake_post):
        first = letters.post(s, "all")
        assert "Test run passed" in first and calls[-1]["test"] == "true", first
        real = letters.post(s, "all")
    assert calls[-1]["test"] == "false" and "addons" not in calls[-1], calls[-1]   # second class: no FIRST_CLASS addon
    assert "Test Home 1 (£1.90)" in real and "Test Home 4 (£1.90)" in real, real
    assert "held (Test Home 2)" in real, real                                       # held one not sent with 'all'
    assert s.get_prospect("p2").get("letter_status") != "posted"
    assert abs(letters.spent(s) - 3.80) < 0.01
    s.put_prospect(prospect(5))
    letters.list_letters(s, "https://x")
    with mock.patch.object(letters.requests, "post", fake_post):
        capped = letters.post(s, "all")
    assert "cap of £5 reached" in capped, capped                                    # 3.80 + 2.30 guess > 5
    print("letters test passed:", pages, "pages")


if __name__ == "__main__":
    sys.exit(main())
