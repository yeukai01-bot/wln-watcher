# Well-Led Network article brief (for Yeukai Kajidori)

## Who writes, who reads
- Author: Yeukai Kajidori. 23 years in UK health and social care, 15 years as a CQC Registered Manager, led 2 services to a Good rating, secured £2.5m+ a year in local-authority contracts. Now runs The Well-Led Network (welllednetwork.com), a free community for UK adult social care leaders.
- Readers: Registered Managers, Nominated Individuals, owners and people applying to register a care service in England. Capable, time-poor, under regulatory pressure, often alone with it. They arrive from a Facebook/LinkedIn comment, often mid-crisis.
- Bottom line purpose: be the most useful, factual page on the subject so readers trust Yeukai, download the gated lead magnet (free Well-Led Network account), and some go on to book help.

## Voice rules (non-negotiable)
- British English, first person, calm authority, warm, plain, human. Never hype. No emojis.
- NO dashes of any kind used as punctuation (no em dash, no en dash, no spaced hyphen). Use commas, full stops, colons or restructure. Hyphens inside compound words (well-led, one-to-one) are fine.
- Facts only, from primary sources (legislation.gov.uk, cqc.org.uk, gov.uk, Skills for Care, DHSC, Home Office). Cite the source by name in a closing "Sources:" line. Never invent facts, figures, cases or quotes.
- Avoid controversy: no opinions on policy, no criticism of CQC, government, unions or other providers, no political commentary. If a topic has a contested side, state the facts neutrally and leave it.
- Never put an attributed quote in the mouth of CQC or any regulator/person. Paraphrase guidance ("CQC's guidance says that...").
- Never state a specific inspection timeline as fact. Where CQC itself publishes a statutory period (e.g. 28 days to appeal, 10 working days for representations), you may state it with the source.
- Never claim CQC approval, guaranteed compliance, guaranteed rating or guaranteed registration.
- Short honest line near the top: general information, not legal advice on an individual case.
- Give the full practical answer away. Real, specific steps a reader can use today, not directional hints.
- Articles must stand alone: a reader with no context understands and benefits fully.
- Headlines: name the moment and the stakes in the first few words (proven winners: "A CQC Warning Notice Has Landed: The Two Clocks That Start Running", "Leaving the Registered Manager Role: An Honest Map", "The Reasonable Adjustment Digital Flag: What Care Providers Need to Do Now"). Never vague story titles.
- Where it fits, Yeukai's own experience in the first person ("I have sat with that letter"), without naming former employers and without inventing specific anecdotes. Keep such lines general and true to a long-serving RM.

## Structure that works (from the best-performing article)
1. Opening: the moment the reader is in, 2 to 4 short paragraphs, then what the article covers. One line: general information not advice.
2. LEAD MAGNET BLOCK, placed early (after the first main section at the latest): a heading "Take the [tool] rather than [doing it by hand]" , 2 sentences on what the tool does, then the link line "Download the [name]" and "Free. It sits in the Well-Led Network library, so you will need an account, which is also free."
3. Body sections with plain headings: what it is legally, what the rules/guidance say, the clocks/deadlines, the trap people fall into.
4. "What to actually do, in order": numbered, concrete steps.
5. "The mistakes that cost people": short bolded-lead bullets.
6. "On paying someone" (optional): honest view on where outside help earns its money, narrow and specific. This builds trust and brings clients.
7. Repeat the lead magnet block.
8. "The last thing": a calm close. Then two invitations:
   - "If you are in the middle of this and want to think it through with people who have carried the same responsibility, bring it to Ask Questions in the Well-Led Network. I read it daily and reply."
   - "If you would rather map your own position one to one, you can book a Free CQC Readiness Strategy Call."
9. "Sources:" line naming the legislation and guidance used, plus "This article is general information and is not legal advice on any individual case."

Length: 1,600 to 2,600 words.

## Output format for each article
Write a markdown file with this front matter block then the body in markdown (## for section headings, **bold**, bullet and numbered lists):

```
TITLE: ...
SLUG: short-url-slug (lowercase, hyphens, 3 to 7 words)
SHORBY: short-slug (1 to 3 words, e.g. section-31)
CATEGORY: one of Enforcement, Readiness, Governance, Business, Workforce
META: one-sentence meta description under 155 characters
LEAD_MAGNET_NAME: human name, e.g. "Section 31 Urgent Response Checklist"
LEAD_MAGNET_FILE: filename, e.g. Section-31-Urgent-Response-Checklist.docx (or .xlsx)
---
(body)
```
In the body, write the lead magnet link as: [Download the Section 31 urgent response checklist](LEAD_MAGNET_LINK) and the calls to action as [Ask Questions](ASK_LINK) and [book a Free CQC Readiness Strategy Call](CALL_LINK). Placeholders will be replaced at publish time.

## Lead magnets
Each article gets one genuinely useful tool (checklist, tracker, template, workbook) that is the "next step" for the reader. Build it as a real file:
- .docx with python-docx (install with pip --break-system-packages if missing) for checklists/templates: branded title "The Well-Led Network", tool name, one-line purpose, how to use it, the tool itself (tables with tick boxes, fields to fill), a short "sources" line and a footer line "General information, not legal advice. welllednetwork.com". British English, no dashes as punctuation.
- .xlsx with openpyxl for trackers/calculators: working formulas (e.g. WORKDAY with England bank holidays 2026 and 2027 listed on a sheet), clear input cells, instructions tab.
The tool must deliver exactly what the article promises about it. Save files into /home/claude/wln/magnets/.

## Existing articles on the site (do NOT duplicate; you may link-reference the topic in passing)
A CQC Warning Notice Has Landed; The envelope that arrived on a Tuesday (Notice of Proposal, 28 days); Nine weeks of nothing; Your audit says this was checked in March; The Draft Report and the Ten Working Days (factual accuracy); The Draft Report Has Landed; The PIR Arrived Just After Your Inspection; The Framework Is Changing (CQC assessment framework); When the Assessment Happens Off Site; How to Buy a Mock Inspection; Business Continuity Plan; DoLS After the 2026 Supreme Court Ruling; Unwise Decisions and Positive Risk; The Reasonable Adjustment Digital Flag; When the Provider Will Not Act (RM escalation); Holding Both Roles (RM and NI); Leaving the Registered Manager Role; Who Investigates a Complaint About the RM; Supervision People Want to Attend; Hiring for Retention; When to Hire Your Next Office Person; Why Registration Applications Come Back; The Fit Person Interview; The Financial Forecast That Gets Your Registration Through; The Financial Viability Statement; The Second Branch (registering a location); Selling Your Care Business; CQC Training Matrix; Care Home Audits (x2); Quality Assurance That Earns Its Place; Complaints Audit; Medication Error Learning Review; Medicines Stock Discrepancies; You Said We Did (feedback); Switching Care Software; You Won the Tender; Families Hiring Carers Direct; Where Domiciliary Care Work Comes From; When a Client's Home Is Unsafe; When CHC Funding Ends; What AI Can Safely Take Off an RM's Desk.
