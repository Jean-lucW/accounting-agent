#!/usr/bin/env python3
"""Builds docs/images/src/architecture-overview.svg (then rendered to docs/images/architecture-overview.png), the one-page data-flow diagram.

Edit the boxes below and run: python3 docs/images/src/build_overview.py, then render the SVG to PNG at 2x (any browser screenshot or rsvg-convert -z 2).
"""
from pathlib import Path
import textwrap
W, H = 1600, 1035
FONT = "Helvetica Neue, Helvetica, Arial, sans-serif"
C = {  # fill, stroke, text
 "in":   ("#eef2f7", "#5b6b82", "#1f2a3a"),
 "core": ("#e3f0ff", "#2f6fbf", "#10325c"),
 "you":  ("#fff4cc", "#d98c00", "#4a3300"),
 "out":  ("#e6f6ea", "#2f8f46", "#123d1e"),
 "safe": ("#fde8e8", "#c0392b", "#5c1410"),
 "ai":   ("#efe7fb", "#7048b8", "#2e1a52"),
}
o = []
def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
def text(x, y, s, size=13, weight="normal", fill="#222", anchor="start"):
    o.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{esc(s)}</text>')
def box(x, y, w, h, kind, title, body="", tsize=15, bsize=12.5, r=10, center=False):
    f, s, t = C[kind]
    o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{f}" stroke="{s}" stroke-width="1.6"/>')
    cx = x + w/2 if center else x + 14
    anc = "middle" if center else "start"
    text(cx, y + 24, title, tsize, "bold", t, anc)
    chars = max(10, int((w - 28) / (bsize * 0.52)))
    yy = y + 24 + tsize*0.5 + 12
    for para in body.split("\n"):
        for line in textwrap.wrap(para, chars) or [""]:
            text(cx, yy, line, bsize, "normal", t, anc); yy += bsize + 4
def arrow(x1, y1, x2, y2, color="#55606e", width=2, dash=False):
    d = ' stroke-dasharray="6 5"' if dash else ""
    o.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}"{d} marker-end="url(#ah)"/>')
def line(pts, color="#55606e", width=2):
    p = " ".join(f"{a},{b}" for a, b in pts)
    o.append(f'<polyline points="{p}" fill="none" stroke="{color}" stroke-width="{width}"/>')
def step(x, y, n):
    o.append(f'<circle cx="{x}" cy="{y}" r="13" fill="#2f6fbf"/>')
    text(x, y + 5, str(n), 14, "bold", "#fff", "middle")

o.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
o.append('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#55606e"/></marker></defs>')
o.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
text(W/2, 42, "Accounting Agent: how data gets in, gets processed, and gets out", 26, "bold", "#1a1a1a", "middle")
text(W/2, 68, "Every arrow is a real data flow. Yellow boxes are the only things an adopting company writes.", 14, "normal", "#555", "middle")

# column headers
for x, w, label, kind in [(30, 300, "DATA IN", "in"), (400, 800, "THE AGENT (runs on your server)", "core"), (1270, 300, "DATA OUT", "out")]:
    text(x + w/2, 104, label, 16, "bold", C[kind][1], "middle")

# ---- inputs
ins = [
 ("Slack", "Admins, users and read-only users: commands, questions, answers, invoice files"),
 ("Gmail inbox", "Supplier invoices and receipts emailed to the accounting inbox"),
 ("Bank APIs (read-only)", "Revolut and Mercury: every settled movement on every account"),
 ("Bank statement CSVs", "For banks without an API, or history before the API cut-over"),
 ("Google Drive", "Payroll documents, contracts and staff files"),
 ("Xero (read)", "Bills, invoices, journals, balances, chart of accounts, per entity"),
]
iy = []
for i, (t, b) in enumerate(ins):
    y = 120 + i * 112
    box(30, y, 300, 96, "in", t, b)
    iy.append(y + 48)

# ---- outputs
outs = [
 ("Xero (write)", "AUTHORISED bills with the document attached, spend money, intercompany journals. Never a payment."),
 ("Excel workbooks", "Bills payable, prepayments, accruals, deposits, intercompany (one tab per loan), bank fees"),
 ("Google Drive", "Every workbook published to the agent's folder, overwritten each build"),
 ("Slack channel", "Run reports, manual items, the outstanding list, answers in the asking thread"),
 ("Gmail", "Processed invoice emails labelled as bookkept"),
 ("Git repo", "Admin rulings written back into rules/ and committed, so the agent learns"),
]
oy = []
for i, (t, b) in enumerate(outs):
    y = 120 + i * 112
    box(1270, y, 300, 96, "out", t, b)
    oy.append(y + 48)

# ---- server frame
o.append('<rect x="400" y="116" width="800" height="680" rx="16" fill="#f7faff" stroke="#2f6fbf" stroke-width="2" stroke-dasharray="2 0"/>')

# row 1: triggers
step(425, 160, 1); text(445, 165, "TRIGGER", 13, "bold", C["core"][1])
box(430, 178, 360, 86, "core", "Slack listener", "Checks the whitelist (admin / user / read-only). One agent per Slack thread.")
box(810, 178, 370, 86, "core", "Cron schedule", "Daily bookkeeping chain, bills report, balance reports, twice-weekly review.")
# row 2: runner
step(425, 292, 2); text(445, 297, "START", 13, "bold", C["core"][1])
box(430, 308, 750, 58, "core", "Session runner", "Lock, log, Xero rate-limit check, then starts a headless Claude Code session for ONE domain.", bsize=12.5)
arrow(610, 264, 610, 306); arrow(995, 264, 995, 306)
# row 3: think
step(425, 394, 3); text(445, 399, "DECIDE", 13, "bold", C["ai"][1])
box(430, 410, 230, 70, "you", "config/group.toml", "STRUCTURE: entities, banks, loans, Slack tiers", bsize=12)
box(430, 492, 230, 70, "you", "rules/", "JUDGEMENT: who books what, coding, suppliers, payroll", bsize=12)
box(690, 410, 490, 152, "ai", "Claude Code session", "")
box(705, 445, 145, 104, "ai", "CLAUDE.md", "The rulebook every session reads", tsize=13, bsize=11.5)
box(860, 445, 150, 104, "ai", "Skills", "One workflow per domain: bookkeeping, reports, interco", tsize=13, bsize=11.5)
box(1020, 445, 148, 104, "ai", "invoice-extract", "Reads many documents in parallel, decides nothing", tsize=13, bsize=11.5)
arrow(805, 366, 805, 408)
arrow(660, 445, 688, 445); arrow(660, 527, 688, 527)
# row 4: act
step(425, 590, 4); text(445, 595, "ACT", 13, "bold", C["core"][1])
tools = [("Connectors", "Xero, Gmail, Drive, Revolut, Mercury"), ("Feeds", "Bank feed and bill feed per entity"),
         ("Builders", "Reports, interco recon, AP check"), ("Registers", "Intake ledger, outstanding items")]
for i, (t, b) in enumerate(tools):
    box(430 + i * 190, 606, 176, 82, "core", t, b, tsize=14, bsize=12)
arrow(935, 562, 935, 604)
# row 5: safety band
step(425, 716, 5); text(445, 721, "GUARDRAILS (every step)", 13, "bold", C["safe"][1])
box(430, 732, 750, 52, "safe", "", "", r=8)
text(805, 755, "Hooks block any payment and any write in read-only sessions  ·  bank clients are GET-only", 12.5, "normal", C["safe"][2], "middle")
text(805, 773, "Duplicate check before every create  ·  run ends only when the phantom-payment check prints PASS", 12.5, "normal", C["safe"][2], "middle")

# ---- input wiring: Slack -> listener; others -> bus -> connectors
arrow(330, iy[0], 428, iy[0] + 33)
line([(330, iy[1]), (365, iy[1])]); 
for y in iy[2:]: line([(330, y), (365, y)])
line([(365, iy[1]), (365, iy[-1])])
line([(365, 647), (380, 647)]); arrow(380, 647, 428, 647)

# ---- output wiring: builders/connectors -> bus -> outputs; session -> Slack
line([(1180, 647), (1235, 647)])
line([(1235, oy[0]), (1235, oy[-1])])
for y in oy: arrow(1235, y, 1268, y)
line([(1180, 486), (1235, 486)])

# ---- legend
lx, ly = 30, 820
text(lx, ly, "KEY", 13, "bold", "#333")
for i, (k, label) in enumerate([("in", "Where data comes from"), ("core", "Agent machinery (shipped)"), ("ai", "Claude: the decisions"),
                                ("you", "You fill this in"), ("out", "What the agent produces"), ("safe", "Safety enforced in code")]):
    x = lx + 60 + i * 255
    f, s, t = C[k]
    o.append(f'<rect x="{x}" y="{ly-13}" width="22" height="16" rx="3" fill="{f}" stroke="{s}" stroke-width="1.5"/>')
    text(x + 30, ly, label, 13, "normal", "#333")

# ---- worked example strip
ey = 860
text(30, ey, "EXAMPLE: one supplier invoice, end to end", 16, "bold", "#1a1a1a")
steps = [
 ("Arrives", "PDF emailed to the inbox, or dropped in Slack", "in"),
 ("Read", "invoice-extract turns it into supplier, date, amount, tax lines", "ai"),
 ("Decide", "Entity and account chosen from rules/; unsure means a Slack question", "you"),
 ("Check", "Already in Xero? The duplicate guard says no", "safe"),
 ("Post", "AUTHORISED bill in the right entity, PDF attached, email labelled", "out"),
 ("Match", "Bank feed shows who paid; paid by another entity gets the interco leg", "core"),
 ("Report", "Line in the Slack run report; a person reconciles the bank line", "out"),
]
bw, gap = 206, 18
for i, (t, b, k) in enumerate(steps):
    x = 30 + i * (bw + gap)
    box(x, ey + 18, bw, 92, k, f"{i+1}. {t}", b, tsize=14, bsize=12)
    if i < len(steps) - 1:
        arrow(x + bw + 2, ey + 64, x + bw + gap - 2, ey + 64)
text(W/2, H - 22, "Detail diagrams for each part follow below in ARCHITECTURE.md. Setup: docs/setup/. Adapting to your group: ADAPTING.md.", 13, "normal", "#666", "middle")
o.append("</svg>")
open(Path(__file__).with_name("architecture-overview.svg"), "w").write("\n".join(o))
print("written")
