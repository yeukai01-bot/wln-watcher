"""The printed radar letter: page 1 is the letter, the following pages are the free CQC report review.

Same design as the hand-built batch of 6 Nov 2026: Well-Led Network logo top right, recipient address inside
Stannp's A4 address window (x 21-117mm, y 44.5-87mm from the top), two QR codes (book a call, WhatsApp me),
company footer, and a branded review with "Was this shared with you?" and a closing panel.

build_pdf(p) returns the PDF as bytes. p is a radar prospect with p["review"] written by review.write_review().
"""
from __future__ import annotations

import io
import os
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily, stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, NextPageTemplate, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

HERE = Path(__file__).resolve().parent / "assets"
_F = HERE / "fonts"
pdfmetrics.registerFont(TTFont("C", str(_F / "Carlito-Regular.ttf")))
pdfmetrics.registerFont(TTFont("CB", str(_F / "Carlito-Bold.ttf")))
pdfmetrics.registerFont(TTFont("CI", str(_F / "Carlito-Italic.ttf")))
registerFontFamily("C", normal="C", bold="CB", italic="CI", boldItalic="CB")

NAVY = colors.HexColor("#142B4C"); GOLD = colors.HexColor("#B88A2E"); CREAM = colors.HexColor("#F4EBD7")
INK = colors.HexColor("#1F2430"); GREY = colors.HexColor("#5B6170")

CALL_URL = os.getenv("LETTER_CALL_URL", "https://shor.by/review-call")
WHATSAPP = os.getenv("LETTER_WHATSAPP", "07907 904199")
WA_NUMBER = "44" + re.sub(r"\D", "", WHATSAPP).lstrip("0")
WA_GROUP = "https://shor.by/wln-whatsapp"
SITE = "www.welllednetwork.com"
SENDER = ["71-75 Shelton Street", "Covent Garden", "London WC2H 9JQ", "www.welllednetwork.com/blog", WHATSAPP,
          "kajidoricollective@gmail.com"]
OLD_REPORT_DAYS = int(os.getenv("LETTER_OLD_REPORT_DAYS", "42"))

st = lambda name, **k: ParagraphStyle(name, fontName=k.pop("font", "C"), textColor=k.pop("color", INK), **k)
BODY = st("b", fontSize=11, leading=15.2, spaceAfter=7)
H_LET = st("hl", font="CB", fontSize=13, leading=17, color=NAVY, spaceAfter=8, spaceBefore=4)
R_TITLE = st("rt", font="CB", fontSize=17, leading=21, color=NAVY, spaceAfter=2)
R_SUB = st("rs", fontSize=9.5, leading=12.5, color=GREY, spaceAfter=6)
R_H = st("rh", font="CB", fontSize=11.5, leading=14, color=NAVY, spaceBefore=7, spaceAfter=3)
R_B = st("rb", fontSize=9.6, leading=12.6, spaceAfter=3)
R_BUL = st("rbul", fontSize=9.6, leading=12.4, leftIndent=11, bulletIndent=1, spaceAfter=1.5)
R_FIXT = st("rft", font="CB", fontSize=10.2, leading=13, color=INK, spaceBefore=3, spaceAfter=1)
R_PROOF = st("rp", font="CI", fontSize=9.2, leading=12, color=GREY, leftIndent=11, spaceAfter=3)
BOX = st("box", fontSize=9, leading=12, color=NAVY)
PANEL = st("pn", fontSize=9.8, leading=13.2, color=colors.white)
PANEL_H = st("pnh", font="CB", fontSize=12, leading=15, color=GOLD, spaceAfter=3)
CAP = st("cap", fontSize=7.8, leading=9.5, color=GREY, alignment=1)


def esc(t) -> str:
    return str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _title(name: str) -> str:
    return " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in (name or "").split())


def _qr(data: str) -> ImageReader:
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    q.add_data(data)
    q.make(fit=True)
    buf = io.BytesIO()
    q.make_image(fill_color=(20, 43, 76), back_color="white").save(buf, format="PNG")
    buf.seek(0)
    return buf


def address_lines(p: dict) -> list[str]:
    manager = _title(p.get("registered_manager") or "")
    lines = [manager, "Registered Manager"] if manager else ["The Registered Manager"]
    return lines + [p.get("location_name", "")] + list(p.get("address_lines") or []) + [p.get("postcode", "")]


def report_age_days(p: dict) -> int | None:
    m = re.findall(r"\d{1,2} \w+ \d{4}", p.get("report_date") or "")
    try:
        return min((date.today() - datetime.strptime(x, "%d %B %Y").date()).days for x in m) if m else None
    except ValueError:
        return None


def _letter_page(c, d):
    H = A4[1]
    c.saveState()
    c.setFillColor(NAVY); c.rect(0, H - 14 * mm, A4[0], 14 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("CB", 11); c.drawString(20 * mm, H - 9 * mm, "THE WELL-LED NETWORK")
    c.setFillColor(GOLD); c.setFont("C", 9); c.drawRightString(A4[0] - 20 * mm, H - 9 * mm, "Yeukai Kajidori  ·  Supporting UK care leaders")
    c.drawImage(str(HERE / "wln_logo.png"), 163 * mm, H - 43 * mm, width=27 * mm, height=27 * mm)
    lines = getattr(d, "addr_lines", [])
    fs = 10
    while fs > 8 and max([stringWidth(a, "C", fs) for a in lines] or [0]) > 88 * mm:
        fs -= 0.5
    lead = min(fs * 1.2, (39 * mm) / max(len(lines), 1))
    c.setFillColor(INK); c.setFont("C", fs)
    y = H - 47 * mm - fs
    for a in lines:
        c.drawString(25 * mm, y, a); y -= lead
    c.setFont("CB", 9); c.setFillColor(NAVY); y = H - 50 * mm
    c.drawString(128 * mm, y, "The Well-Led Network"); c.setFont("C", 8.8); c.setFillColor(GREY)
    for a in SENDER:
        y -= 10.8; c.drawString(128 * mm, y, a)
    y -= 16; c.setFont("C", 9.5); c.drawString(128 * mm, y, datetime.now().strftime("%B %Y"))
    y -= 12.5; c.setFont("CB", 9.5); c.setFillColor(INK); c.drawString(128 * mm, y, "Private and confidential")
    c.setFillColor(GREY); c.setFont("C", 7.5)
    c.drawString(20 * mm, 13.5 * mm, "The Well-Led Network is a trading name of The Kajidori Collective Ltd, registered in England and Wales, company no. 17556769.")
    c.drawString(20 * mm, 10 * mm, "Registered office: 71-75 Shelton Street, Covent Garden, London WC2H 9JQ. I found your service's details on its public CQC report.")
    c.drawString(20 * mm, 6.5 * mm, f"If you would rather not hear from me again, email kajidoricollective@gmail.com or call {WHATSAPP} and I will remove you straight away.")
    c.restoreState()


def _review_page(c, d):
    c.saveState()
    c.setFillColor(NAVY); c.rect(0, A4[1] - 16 * mm, A4[0], 16 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("CB", 12); c.drawString(18 * mm, A4[1] - 9 * mm, "THE WELL-LED NETWORK")
    c.setFillColor(GOLD); c.setFont("C", 9); c.drawString(18 * mm, A4[1] - 13.2 * mm, "Free tools for UK care leaders")
    c.setFillColor(colors.white); c.drawRightString(A4[0] - 18 * mm, A4[1] - 10 * mm, "Free CQC report review")
    c.setStrokeColor(GOLD); c.setLineWidth(0.8); c.line(18 * mm, 13 * mm, A4[0] - 18 * mm, 13 * mm)
    c.setFillColor(GREY); c.setFont("C", 7.8)
    c.drawString(18 * mm, 8.5 * mm, f"Free from The Well-Led Network, by Yeukai Kajidori  ·  https://{SITE}  ·  WhatsApp: https://shor.by/fb-wa")
    c.drawRightString(A4[0] - 18 * mm, 8.5 * mm, f"Review page {c.getPageNumber() - 1}")
    c.restoreState()


def _fixes(rv: dict) -> list[dict]:
    out = []
    for i, f in enumerate((rv.get("fixes") or [])[:3]):
        if isinstance(f, dict) and f.get("title"):
            out.append({"title": f.get("title", ""), "why": f.get("why", ""), "steps": list(f.get("steps") or [])[:3],
                        "proof": f.get("proof", "")})
    if len(out) < 3:  # older reviews only have the short fix_first list
        ev = rv.get("evidence") or []
        out = [{"title": t, "why": "", "steps": [], "proof": ev[i] if i < len(ev) else ""}
               for i, t in enumerate((rv.get("fix_first") or [])[:3])]
    return out


def _story(p: dict, rv: dict, logrows: int):
    name = esc(p.get("location_name", "your service"))
    manager = _title(p.get("registered_manager") or "")
    E = []
    greet = f"Dear {manager.split()[0]}," if manager else "Dear Registered Manager,"
    E.append(Paragraph(greet, BODY))
    E.append(Paragraph("Your CQC report, and the three things I would fix first", H_LET))
    E.append(Paragraph("You know those registered managers whose next report comes back Good? It is not luck, and it is not a bigger budget. "
                       "They fix what keeps creating the findings, not just the findings themselves.", BODY))
    when = esc(p.get("report_date") or "recently")
    age = report_age_days(p)
    if age is not None and age >= OLD_REPORT_DAYS:
        E.append(Paragraph(f"I have read the CQC report for {name}, published on {when}. By now the action plan will have gone to CQC, "
                           "and the hard part has started: making the changes stick while the day job carries on. That is where most plans stall, "
                           "quietly, a few weeks in. So I have written you a short, free review of what I think sits underneath the findings, "
                           "and the three things I would make sure are working before CQC comes back. It is enclosed.", BODY))
    else:
        E.append(Paragraph(f"I have read the CQC report for {name}, published on {when}. I know how heavy a report like that feels "
                           "for a manager and a team who work hard every day. So I have written you a short, free review of what I think sits underneath it, "
                           "and the three things I would put first. It is enclosed.", BODY))
    E.append(Paragraph("Why listen to me? I was a CQC Registered Manager for fifteen years. I took a failing service from embargo back to green with its "
                       "local authority, and led another to Preferred Provider status with zero deficiencies. I have sat in your chair.", BODY))
    wa_text = quote("Hi Yeukai, I got your CQC letter. I'm ... from ...")
    q1 = [Image(_qr(CALL_URL), width=23 * mm, height=23 * mm), Paragraph("Book a call", CAP)]
    q2 = [Image(_qr(f"https://wa.me/{WA_NUMBER}?text={wa_text}"), width=23 * mm, height=23 * mm), Paragraph("WhatsApp me", CAP)]
    cta = Paragraph("There is no catch. If you would like to talk it through, I have set aside time for 30-minute calls with managers who receive this letter. "
                    f"Scan the first code, or go to <b>{CALL_URL}</b>, to pick a time that suits you. "
                    f"Or, if you are in the middle of a busy day, scan the WhatsApp code (or message <b>{WHATSAPP}</b>) with your name and your service, "
                    "and I will come back to you personally within 48 hours.", BODY)
    ct = Table([[cta, q1, q2]], colWidths=[112 * mm, 26 * mm, 26 * mm], hAlign="LEFT")
    ct.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (0, 0), 0), ("ALIGN", (1, 0), (-1, -1), "CENTER")]))
    E += [ct, Spacer(1, 3 * mm), Paragraph("Warm regards,", BODY), Spacer(1, 10 * mm),
          Paragraph("<b>Yeukai Kajidori</b><br/>Founder, The Well-Led Network<br/>www.welllednetwork.com/blog", st("sig", fontSize=10.5, leading=13.5)),
          Spacer(1, 5 * mm),
          Paragraph("<b>P.S.</b> Want quick answers to your CQC questions? Join the Well-Led Network, my free WhatsApp group for UK care leaders. "
                    f"Managers post their questions and I answer them there: {WA_GROUP}", st("ps", fontSize=10, leading=13.5))]
    E += [NextPageTemplate("R"), PageBreak()]

    # ---- the review ----
    to = manager or "the Registered Manager"
    E.append(Paragraph(f"Free CQC report review: {name}", R_TITLE))
    E.append(Paragraph(f"Prepared by Yeukai Kajidori for {esc(to)}, from the CQC report published {when}. Rating: <b>{esc(p.get('rating', ''))}</b>. "
                       "This is my reading of a public report, written to help, not an official CQC document.", R_SUB))
    box = Table([[Paragraph(f"<b>Was this shared with you?</b> Get a free review of your own service's report: <b>{CALL_URL}</b>  ·  "
                            f"More free tools: https://{SITE}/library", BOX)]], colWidths=[A4[0] - 41 * mm])
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), CREAM), ("BOX", (0, 0), (-1, -1), 0.6, GOLD),
                             ("LEFTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    E += [box, Spacer(1, 1 * mm)]
    E.append(Paragraph("1. What the report says, in plain words", R_H))
    for f in rv.get("found") or []:
        E.append(Paragraph(esc(re.sub(r"^\(([^)]*)\)\s*", r"\1: ", f)), R_BUL, bulletText="•"))
    if rv.get("working"):
        E.append(Paragraph("2. What is already working, so build on it", R_H))
        E.append(Paragraph(esc(rv["working"]), R_B))
    n = 3 if rv.get("working") else 2
    E.append(Paragraph(f"{n}. What I think sits underneath it", R_H))
    for u in rv.get("underneath") or []:
        E.append(Paragraph(esc(u), R_BUL, bulletText="•"))
    n += 1
    fixes = _fixes(rv)
    E.append(Paragraph(f"{n}. The three things I would fix first", R_H))
    for i, fx in enumerate(fixes, 1):
        blk = [Paragraph(f"{i}. {esc(fx['title'])}", R_FIXT)]
        if fx["why"]:
            blk.append(Paragraph(esc(fx["why"]), R_B))
        blk += [Paragraph(esc(s), R_BUL, bulletText="›") for s in fx["steps"]]
        if fx["proof"]:
            blk.append(Paragraph("You will know it is working when: " + esc(fx["proof"]), R_PROOF))
        E.append(KeepTogether(blk))
    n += 1
    if len(fixes) == 3:
        f1, f2, f3 = [re.sub(r"[.\s]+$", "", f["title"]) for f in fixes]
        rows = [["Week", "What to do"],
                ["1", f"Start fix 1: {f1}. Name one owner and put the first check in the diary."],
                ["2", f"Start fix 2: {f2}. Keep fix 1 running every week."],
                ["3", f"Start fix 3: {f3}. Check the first two are still being done."],
                ["4", "Re-check all three. Write a one-page progress note for your provider or nominated individual, and keep it as evidence for your next inspection."]]
        rows = [[Paragraph(f"<b>{a}</b>" if i == 0 else a, R_B), Paragraph(f"<b>{b}</b>" if i == 0 else esc(b), R_B)] for i, (a, b) in enumerate(rows)]
        pt = Table(rows, colWidths=[16 * mm, A4[0] - 57 * mm])
        pt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), CREAM), ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9CFB8")),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        E.append(KeepTogether([Paragraph(f"{n}. Your first 30 days", R_H), pt]))
        n += 1
    qs = rv.get("questions") or []
    if qs:
        E.append(KeepTogether([Paragraph(f"{n}. Questions an inspector is likely to ask you next time", R_H),
                               Paragraph("Practise answering these with evidence in your hand, not from memory:", R_B)]
                              + [Paragraph(esc(q), R_BUL, bulletText="•") for q in qs[:3]]))
        n += 1
    if logrows:
        lr = [[Paragraph(f"<b>{h}</b>", R_B) for h in ["What needs fixing", "Owner", "Due", "Checked", "Closed"]]]
        lr += [[""] * 5 for _ in range(logrows)]
        lt = Table(lr, colWidths=[78 * mm, 30 * mm, 21 * mm, 21 * mm, 19 * mm], rowHeights=[None] + [7.2 * mm] * logrows, hAlign="LEFT")
        lt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), CREAM), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CFC3A6"))]))
        E.append(KeepTogether([Paragraph(f"{n}. Your improvement log: copy this and keep it on the office wall", R_H), lt]))
    panel = Table([[Paragraph("Want help putting this in place?", PANEL_H)],
                   [Paragraph("This is the order I use in the 90-Day Improvement Reset: find the root cause, fix it in the right order, embed the change, "
                              "and prove it is working. I run it with registered managers in the Requires Improvement to Good Accelerator, so you are not "
                              f"building the plan alone. Book a free 30-minute call to talk through your report: <b>{CALL_URL}</b>", PANEL)],
                   [Paragraph(f"Free WhatsApp group for UK care leaders: {WA_GROUP}  ·  Free tools: https://{SITE}/library", PANEL)]],
                  colWidths=[A4[0] - 41 * mm])
    panel.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                               ("TOPPADDING", (0, 0), (0, 0), 8), ("BOTTOMPADDING", (0, -1), (-1, -1), 8)]))
    E += [Spacer(1, 2 * mm), KeepTogether([panel])]
    return E


def _render(p: dict, rv: dict, logrows: int) -> tuple[bytes, int]:
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, title=f"Letter and free CQC report review: {p.get('location_name', '')}",
                          author="Yeukai Kajidori, The Well-Led Network")
    lf = Frame(20 * mm, 18 * mm, A4[0] - 40 * mm, A4[1] - 110 * mm, id="l")
    rf = Frame(18 * mm, 16 * mm, A4[0] - 36 * mm, A4[1] - 36 * mm, id="r")
    doc.addPageTemplates([PageTemplate("L", [lf], onPage=_letter_page), PageTemplate("R", [rf], onPage=_review_page)])
    doc.addr_lines = address_lines(p)
    doc.build(_story(p, rv, logrows))
    data = buf.getvalue()
    return data, page_count(data)


def build_pdf(p: dict) -> tuple[bytes, int]:
    """Returns (pdf bytes, page count). Keeps the letter to 3 pages where the review allows, by shrinking
    the blank improvement log; never more than 4 pages."""
    rv = p.get("review") or {}
    best = None
    for rows in (16, 12, 8, 4, 0):
        data, pages = _render(p, rv, rows)
        best = (data, pages)
        if pages <= 3:
            break
    return best


def page_count(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", data))
