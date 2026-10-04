# Rewriting and auditing an existing document

Most document work is rewriting, not drafting. The documents that need it are usually a generic HR, IT or finance template with a few good paragraphs someone wrote from scratch buried inside.

The job is to find those paragraphs, keep them, and rebuild everything else around them. The reason this is harder than drafting is that template language looks like policy. It has headings, it has "will", it has an air of authority, and it says nothing. It survives edits because nothing about it looks broken.

## Order to work in

1. **Read the whole document before changing anything.** Mark the paragraphs that already carry house voice. Those are the calibration for the rest, and they usually tell you what the organisation actually decided
2. **Extract the actual rules.** Under the template language there is normally a short list of real decisions: a limit, a notice period, an approval, a prohibition. Write them down as a bare list. Anything in the document that is not one of these and not a reason for one of these is padding
3. **Check the rules are true.** Template documents assert controls the organisation does not have and name authorities that do not exist. Where a control is not built, say so in the new draft. Where nobody holds a power, write **[owner to be named]**
4. **Rebuild each section as four-beat paragraphs** where a rule needs defending. Rule, reason, carve-out, short close. Leave procedure flat
5. **Cut what is left.** A rewrite that comes out longer than the original has usually failed
6. **Run `scripts/check_style.py`**, then read once for voice
7. **Report what changed** and what you could not resolve

## Strip list

These are the layers of inherited template text, roughly oldest first. All of them go.

**Wrong-organisation language.** Any reference to a kind of business the group is not: customers it does not have, service users, consumers, "the communities we serve", brand and marketing where the group has nothing to market, public reputation as a commercial asset. Where the template appeals to customer trust, the real reason is usually the group's own money, its banking and supplier relationships, or its standing with its auditors and lenders. Find the real reason and write that instead. What counts as wrong depends on the group: read `rules/GROUP.md` first, and pass the words that do not fit to the checker with `--avoid`.

**Unnamed authority.** "Senior management", "the appropriate manager", "the designated senior owner", "the relevant committee", "named roles". Either name the person or role, or write **[owner to be named]**.

**Asserted controls.** "All access is logged and reviewed monthly", "training is tracked centrally", "an annual attestation is required". If it is not built, write "This control is not yet built."

**Empty obligation verbs.** "It is expected that", "employees should endeavour to", "staff are encouraged to", "will be aligned with", "will collaborate to". Replace with a flat declarative, or cut the sentence if there is no rule under it.

**Consultant vocabulary.** Robust, comprehensive, streamline, leverage, stakeholders, seamless, holistic, best-in-class, deep dive, touch base, circle back, going forward, "in order to".

**Aspirational padding.** "Diversity is not only valued but celebrated", "we are committed to fostering an environment where". Commitments are fine when they carry a rule. Commitments that carry nothing go.

**Restated endings.** A final paragraph summarising the policy. Close on the last concrete point, then `### Review`.

**Tacked-on correctives.** "...in advance, not after the event." "...before anything changes, rather than once it has." The sentence had already said it. Cut the tail.

**Elaborating negative lists.** A statement followed by a run of things it is not: "a holding company with no operations of its own. No sales, no stock, no customers." Keep the statement, delete the list.

**The policy explaining why it exists.** "This is a named policy in the audit file", "required under our banking covenants", "this policy is mandated by". Where the requirement is operative, meaning it creates a duty, a deadline or a notification route, write the duty. Otherwise cut it.

**Ruled-out branches.** The hypothetical that does not apply, and the sentence saying it does not apply ("that question does not arise on the current facts", "if X were the case, that would be a live question"). Keep the rule and the conclusion. Where a change of facts would change the answer, move it to the monitoring list as a trigger.

**Records and people holding each other's verbs.** "The record carries", "the register owns", "X holds the record", "the pack sits with". A record contains things and a person is responsible for it. This is a two-word fix and it is the difference between a sentence someone would say and one nobody would.

**Mechanics.** Em dashes, currency symbols before numbers, "five (5) working days", American -ize and -or spellings where the group writes British English (`--british`), italics, bullets ending in full stops, `##` or `#` headings in a standalone document.

## What to keep

Do not rewrite for the sake of it. Keep:

- Paragraphs that already do the four beats. Leave the good ones alone even if you would have phrased them differently. Churning them loses the person who wrote them
- Legally load-bearing wording: statutory notice periods, defined terms that appear in contracts, wording HR or counsel has signed off. Where wording looks legally deliberate but reads badly, flag it rather than rewriting it
- The local-requirements paragraph, in the group's wording
- The `### Review` section, in the group's wording
- Real thresholds and numbers. Reformat them ("$300" becomes "300 USD"), never change them

## Audit mode

Sometimes the ask is a review rather than a rewrite. Give findings, not a new document.

Order by how much they matter, which is roughly: wrong facts about the organisation, asserted controls that do not exist, unnamed authority, missing reasons, then mechanics. Quote the offending sentence, say what is wrong in one line, and give the replacement. Mechanical findings can be grouped into a single line each with a count rather than listed one by one.

Do not pad the audit to look thorough. Six findings that matter beat twenty that include every bullet with a full stop on it.

## Reporting a rewrite

Short. What you changed at the level of substance, then what is open.

Something like:

> Rewrote all 6 sections. Cut the customer-trust framing throughout, HoldCo has no customers - replaced with the reason that matters, the group's own cash. Two controls in the original are not built, both now say so. Approval owner for exceptions is marked **[owner to be named]**, section 4. Kept the notice period wording as-is, looks like it came from the contracts. 1100 words down to 620.

Do not list every mechanical fix. Do not close with a summary of the policy. Do not explain your reasoning about the style guide or ask the reader to confirm your reading of it. State what you did. If in doubt, ask one direct question.
