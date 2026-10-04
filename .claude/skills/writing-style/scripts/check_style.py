#!/usr/bin/env python3
"""Mechanical house-style checker for the group's own documents.

Usage:
    python3 scripts/check_style.py path/to/policy.md
    python3 scripts/check_style.py path/to/policy.md --quiet        # findings only
    python3 scripts/check_style.py notes.md --not-policy           # no Review section check
    python3 scripts/check_style.py policy.md --avoid customers,clients
                                    # words that are wrong for THIS group
    python3 scripts/check_style.py policy.md --british              # flag -ize/-or spellings
    python3 scripts/check_style.py policy.md --review-text "This policy is reviewed every 12 months."
    python3 scripts/check_style.py policy.md --local-law-text "Where local law is stricter, it applies."
                                    # the group's own standard wording, checked only when given

Catches the failures that are mechanically detectable: em dashes, currency
symbols, banned vocabulary, bullets ending in full stops, negation-knockdown
patterns, unnamed authority, inherited template language (plus any words given
with --avoid), missing Review section. Opt-in: American spellings (--british),
and the group's standard Review and local-law wording (--review-text,
--local-law-text).

It cannot see the failures that matter most: a rule with no reason attached,
a paragraph that would fit any organisation anywhere, a section that does not
close short. It prints those as a reminder at the end. Read the draft yourself.

Exit code 1 if there are errors, 0 otherwise, 2 on bad usage.
"""

import argparse
import re
import sys
from pathlib import Path

ERROR, WARN, INFO = "error", "warn", "info"

# (severity, label, compiled pattern, fix hint)
LINE_RULES = [
    (ERROR, "em dash or en dash", re.compile("[\u2013\u2014]"),
     'use a spaced hyphen " - ", a colon, or a full stop'),

    (ERROR, "currency symbol before number", re.compile(r"[$£€¥]\s?\d"),
     'currency follows the number: "300 USD per night"'),

    (ERROR, "word-then-digit number", re.compile(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|fourteen|thirty)\s*\(\s*\d+\s*\)", re.I),
     'plain digits: "5 working days"'),

    (ERROR, "italics", re.compile(r"(?<![\*\w])\*(?!\*)[^*\n]{1,120}\*(?!\*)|(?<![_\w])_(?!_)[^_\n]{1,120}_(?!_)"),
     "emphasis is bold only"),

    (ERROR, "heading level", re.compile(r"^(#|##|####+)\s+\S"),
     "headings are ### and Title Case; sub-headings are plain bold"),

    (ERROR, "consultant vocabulary", re.compile(
        r"\b(robust|comprehensive|streamlin\w*|leverag\w*|stakeholders?|seamless\w*|"
        r"holistic|best-in-class|deep dive|touch base|circle back)\b", re.I),
     "cut it or say the plain thing"),

    (ERROR, "hedge doing the work of a decision", re.compile(
        r"\b(it is expected that|employees (should|are expected to) endeavour|"
        r"should endeavour to|where appropriate|as appropriate|"
        r"(staff|employees) are encouraged to|it is recommended that)\b", re.I),
     "make the decision and state it flatly"),

    (ERROR, "announcing your own clarity", re.compile(
        r"(principle is simple|put simply|to be clear,|simply put|in essence,|"
        r"it is worth noting|it should be noted)", re.I),
     "if the sentence is simple the reader will notice"),

    (ERROR, "inherited template language", re.compile(
        r"\b(service users?|the communities we serve|our brand|valued customers|"
        r"our customers and partners)\b", re.I),
     "template text about a different kind of business; find the real reason"),

    (ERROR, "unnamed authority", re.compile(
        r"\b(senior management|the appropriate manager|the relevant manager|"
        r"designated senior (owner|manager|person)|the relevant committee|"
        r"(the )?appropriate governance forum|governance forum|named roles|"
        r"the appropriate authority|relevant authorities within the (Group|firm|organisation))\b", re.I),
     "name the role that owns it"),

    (WARN, "going forward / in order to", re.compile(r"\b(going forward|in order to)\b", re.I),
     'cut "going forward"; "in order to" becomes "to"'),

    (WARN, "filler adjective", re.compile(
        r"\b(key|critical|significant|notable|crucial|vital)\b", re.I),
     "usually filler; cut unless genuinely load-bearing"),

    (WARN, "aspirational padding", re.compile(
        r"(not only .{0,40} but (also )?celebrat|committed to fostering|"
        r"strive to (create|foster|build)|we are passionate about|"
        r"aligned with the strategic objectives)", re.I),
     "a commitment that carries no rule is padding"),

    (WARN, "negation set up to be knocked down", re.compile(
        r"(is not the (test|point|issue|question)|"
        r"\bthis is not a [a-z ]{1,30}, it is\b|"
        r"\bit(’|')?s not (about|that) [a-z ]{1,30}, it(’|')?s\b|"
        r"\bnot a preference\b)", re.I),
     "write the positive statement and stop"),

    (ERROR, "tacked-on corrective", re.compile(
        r"(,\s*(and\s+)?not (after|afterwards|later|once|retrospectively|the other way|when)\b|"
        r"\brather than (after|once|when) (the event|it has|you have|something has|anything has|the fact)|"
        r"\bnot (after|once) the (event|fact|decision|change)\b|"
        r"\bnot the other way (round|around)\b)", re.I),
     "the sentence already said it; cut the tail"),

    (ERROR, "elaborating negative list", re.compile(
        r"(\bNo [a-z][a-z ]{2,30},\s*no [a-z][a-z ]{2,30},\s*no\b|"
        r"\bNo [a-z][a-z ]{2,30},\s*no [a-z][a-z ]{2,30}\.|"
        r"\bnot? [a-z][a-z ]{2,25},\s*nor [a-z][a-z ]{2,25},\s*nor\b)", re.I),
     "the statement before this said it; delete the list"),

    (ERROR, "document explaining its own existence", re.compile(
        r"(this (policy|document) (is|exists)[^.\n]{0,40}(required|mandated|because)|"
        r"\bnamed policy\b|"
        r"required (by|under) (the |our )?(auditors?|lenders?|banks?|insurers?|board|(banking )?covenants?)|"
        r"in (the|our) [A-Za-z ]{0,20}(filing|audit file|compliance file|policy register)|"
        r"(we are|the Group is) required to (have|maintain) (this|a written) (policy|document))", re.I),
     "write the duty, deadline or notification route; not that the document is on someone's list"),

    (INFO, '"the company"', re.compile(r"\bthe company\b", re.I),
     'in a multi-entity group name the entity or say "the Group". Fine in "the company\'s money"'),

    (INFO, '"Purpose:" label', re.compile(r"^\s*\**\s*(purpose|scope|objective)\s*:?\s*\**\s*$", re.I),
     "open with an unheaded paragraph instead"),
]

# Opt-in (--british): American spellings in a group that writes British English.
AMERICANISM_RULE = (WARN, "americanism", re.compile(
    r"\b([a-z]{3,}iz(e|es|ed|ing|ation|ations)|[a-z]{3,}yz(e|es|ed|ing)|"
    r"behavior\w*|color\w*|favor(?!ite\b)\w*|honor\w*|labor\w*|neighbor\w*|"
    r"endeavor\w*|rumor\w*|humor\w*)\b", re.I),
    "British spelling: -ise and -our")

AMERICANISM_ALLOW = {
    "size", "sizes", "sized", "sizing", "resize", "resized", "resizes", "resizing",
    "downsize", "downsized", "downsizing", "upsize", "capsize", "capsized",
    "prize", "prizes", "prized", "seize", "seizes", "seized", "seizing",
    "citizen", "citizens", "maize",
    "analysis", "franchise", "franchises", "advertise", "advertises", "advertised",
    "advertising", "supervise", "supervises", "supervised", "supervising",
    "comprise", "comprises", "comprised", "compromise", "compromised", "compromises",
    "exercise", "exercises", "exercised", "exercising", "surprise", "surprised",
    "surprises", "revise", "revised", "revises", "revising", "devise", "devised",
    "wise", "likewise", "otherwise", "clockwise", "improvise", "improvised",
    "raise", "raises", "raised", "praise", "noise", "poise", "arise", "arises",
    "arising", "promise", "promises", "promised", "premise", "premises",
    "expertise", "merchandise", "enterprise", "enterprises", "paradise",
}

VOICE_CHECKLIST = [
    "Does every rule have a reason attached, in a clause rather than a sentence?",
    "Would any paragraph fit any organisation anywhere? If so it is not finished.",
    "Does each section close on a sentence under ten words?",
    "Are the obvious human carve-outs named, and named as normal rather than exceptional?",
    "Does the last paragraph restate the document? Cut it and close on the last concrete point.",
    "Does any sentence end by correcting a reader who has not done anything yet? Cut the tail.",
    "Does the document explain why it exists rather than stating the rule?",
    "Is any control described as existing when it does not?",
    "Is anything shorter available? The first draft is always long.",
]


def in_code_block(lines):
    """Return a set of line indexes inside fenced code blocks."""
    inside, fenced = False, set()
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            inside = not inside
            fenced.add(i)
            continue
        if inside:
            fenced.add(i)
    return fenced


def avoid_rule(words):
    """A rule for the words that are wrong for this group (--avoid a,b,c)."""
    words = [w.strip() for w in words if w.strip()]
    if not words:
        return None
    pattern = re.compile(r"\b(" + "|".join(re.escape(w) + "s?" for w in words) + r")\b", re.I)
    return (ERROR, "wrong-organisation language", pattern,
            "this group has none of these; find the real reason")


def _norm(s):
    return " ".join(s.lower().split())


def check(text, policy=True, avoid=(), british=False, review_text=None, local_law_text=None):
    lines = text.split("\n")
    fenced = in_code_block(lines)
    findings = []
    rules = list(LINE_RULES)
    if british:
        rules.append(AMERICANISM_RULE)
    extra = avoid_rule(avoid)
    if extra:
        rules.append(extra)

    for i, line in enumerate(lines, start=1):
        if (i - 1) in fenced:
            continue
        stripped = line.strip()

        for severity, label, pattern, hint in rules:
            for m in pattern.finditer(line):
                hit = m.group(0)
                if label == "americanism" and hit.lower().strip() in AMERICANISM_ALLOW:
                    continue
                if label == "italics" and stripped.startswith("|"):
                    continue  # table pipes trip the underscore branch
                findings.append((severity, i, label, hit.strip(), hint))

        # bullets must not end with a full stop
        if re.match(r"^\s*([-*+]|\d+\.)\s+\S", line) and stripped.endswith("."):
            findings.append((ERROR, i, "bullet ends with a full stop",
                             stripped[-40:], "bullets do not end with full stops, even multi-sentence ones"))

        # bold sub-heading promoted to a heading, or a ### that is not Title Case
        if re.match(r"^###\s+\S", stripped):
            heading = re.sub(r"^###\s+|\*+", "", stripped).strip()
            words = [w for w in re.findall(r"[A-Za-z][\w'/-]*", heading)]
            minor = {"a", "an", "and", "as", "at", "but", "by", "for", "if", "in",
                     "nor", "of", "on", "or", "the", "to", "up", "via", "with", "is", "not"}
            bad = [w for k, w in enumerate(words)
                   if w[0].islower() and not (k and w.lower() in minor)]
            if bad:
                findings.append((WARN, i, "heading not Title Case", heading,
                                 "### headings are Title Case"))

    # document-level checks
    doc_findings = []
    body = "\n".join(l for k, l in enumerate(lines) if k not in fenced)

    if policy:
        if not re.search(r"^###\s+\**Review\**\s*$", body, re.M):
            doc_findings.append((ERROR, 0, "no Review section",
                                 "", "close with ### Review: when and why the document is reviewed"))
        elif review_text and _norm(review_text) not in _norm(body):
            doc_findings.append((WARN, 0, "Review wording differs from the given --review-text",
                                 "", review_text))
    if local_law_text and _norm(local_law_text) not in _norm(body):
        doc_findings.append((WARN, 0, "local-law wording missing or differs from --local-law-text",
                             "", local_law_text))

    first_heading = next((k for k, l in enumerate(lines) if l.strip().startswith("#")), len(lines))
    opener = " ".join(l.strip() for l in lines[:first_heading] if l.strip()
                      and not l.strip().startswith("#"))
    opener = re.sub(r"^[#*\s]+", "", opener)
    if not opener:
        doc_findings.append((ERROR, 1, "no unheaded opening paragraph",
                             "", "open with 1 to 4 sentences saying what the document is for and who it applies to"))
    elif len(re.findall(r"[.!?]", opener)) > 5:
        doc_findings.append((WARN, 1, "opener longer than 4 sentences",
                             opener[:60] + "...", "one to four sentences"))

    words = len(re.findall(r"\b[\w'-]+\b", body))
    findings = doc_findings + findings
    return findings, words


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", metavar="FILE", help="documents to check")
    ap.add_argument("--quiet", action="store_true", help="findings only, no reminder checklist")
    ap.add_argument("--not-policy", action="store_true", help="skip the Review section check")
    ap.add_argument("--avoid", action="append", default=[], metavar="WORDS",
                    help="comma-separated words that are wrong for this group")
    ap.add_argument("--british", action="store_true",
                    help="flag American spellings (-ize, -or) for a group that writes British English")
    ap.add_argument("--review-text", default=None, metavar="TEXT",
                    help="the group's standard Review wording; checked only when given")
    ap.add_argument("--local-law-text", default=None, metavar="TEXT",
                    help="the group's standard local-law wording; checked only when given")
    opts = ap.parse_args()
    quiet = opts.quiet
    policy = not opts.not_policy
    avoid = [w for chunk in opts.avoid for w in chunk.split(",")]
    args = opts.paths

    exit_code = 0
    for arg in args:
        path = Path(arg)
        if not path.exists():
            print(f"not found: {path}")
            exit_code = 2
            continue

        findings, words = check(path.read_text(encoding="utf-8"), policy=policy, avoid=avoid,
                                british=opts.british, review_text=opts.review_text,
                                local_law_text=opts.local_law_text)
        order = {ERROR: 0, WARN: 1, INFO: 2}
        findings.sort(key=lambda f: (order[f[0]], f[1]))

        print(f"\n{path}  -  {words} words")
        print("=" * 72)

        if not findings:
            print("no mechanical findings")
        for severity, line_no, label, hit, hint in findings:
            where = f"line {line_no}" if line_no else "document"
            shown = f'  "{hit}"' if hit else ""
            print(f"[{severity:5}] {where:>10}  {label}{shown}")
            print(f"{'':19}-> {hint}")

        errors = sum(1 for f in findings if f[0] == ERROR)
        if errors and exit_code == 0:
            exit_code = 1

        if words > 1200:
            print(f"\n{words} words. Over 1200 needs a reason you could defend out loud.")

        if not quiet:
            print("\nThe checker cannot see these. Read the draft and answer them:")
            for q in VOICE_CHECKLIST:
                print(f"  - {q}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
