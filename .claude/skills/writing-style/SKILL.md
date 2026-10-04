---
name: writing-style
description: "The house style for anything the group writes down for itself - policies, procedures, handbooks, briefing notes, memos, process docs, rules files and summaries. Use it for drafting, revising, tightening, auditing or converting any internal document, and to strip AI tells out of any draft. docs/COMMS.md applies it to the agent's messages."
---

# House style

## The voice in one line

Write as the group's own finance lead would: someone who knows how the books actually run, sets a rule down once with the reason for it, and assumes the reader will follow a rule they understand.

The model is a colleague explaining how things work. A template pack, a consultant's deliverable and a contract are what a draft should not sound like.

Everything below serves that line. Where a rule here pulls against it, follow the line.

## What this covers

Everything the group writes down for itself: policies and procedures, and also briefing notes, memos, process docs, the files in `rules/` and summaries.

The voice, the signature moves, the mechanics, the naming ladder, the do-not-write list and The AI Tells apply to all of it without exception.

Two things belong only in a policy or procedure: the local-requirements paragraph and the closing `### Review` section. A briefing note or a summary gets neither. Adding them to a briefing note is the same mistake as leaving them out of a policy.

Chat messages are not covered here on their own terms. For the accounting agent, `docs/COMMS.md` applies this style to every message it sends and lists where the message rules differ.

The do-not-write list and The AI Tells below also govern how you write **back** to the person who asked, in chat or in a covering note. A reply that fails them fails whatever it is attached to.

## Before you write

Two things decide whether the draft will be any good, and both happen before the first paragraph.

**Know what the organisation is.** Read `config/group.toml` (the entities, their currencies and countries) and `rules/GROUP.md` (what each entity does) before writing anything about the group. A sentence that describes a different kind of business, such as one appealing to customers the group does not have or a brand it does not market, is inherited template text and is wrong.

Those facts are background for you. Do not paste them into the document as a list of things the group is not. Where a document has to state what the group is, one sentence does it: "OpCo US is the group's US operating company and employs the US team."

**Know who holds each power the document hands out.** Documents fail quietly when nobody is named. If you do not know who approves an exception, who receives a report, or who owns a control, write **[owner to be named]** in bold so it is visible in the draft. Ask if the user is there to ask. Never write "senior management" or "the designated senior owner" to cover the gap.

If the document describes a control that does not exist yet, say so in it: "This control is not yet built." A policy that asserts a control the group does not have is worse than one that admits the gap.

## Say it the way you would say it out loud

This section decides whether the draft reads professional or reads generated, and it outranks every stylistic move below it.

Before a sentence goes in, ask whether the finance director, the controller or the bookkeeper would say it in that form in a meeting. If they would not, it does not go in, however elegant it looks on the page.

**Do not give an abstract noun a physical verb.** This is the loudest tell in the whole style, because no practitioner talks this way and every language model does.

- Not "approvals nest". Write "a manager's approval limit has to sit inside the controller's, and the controller's inside the board's"
- Not "the threshold is enforced in the system". Write "Xero rejects a payment run that would exceed the limit"
- Not "coverage sits at the main bank". Write "the daily check runs on the main bank accounts, and nowhere else"
- Not "the clock runs from receipt". Write "the 5 working days are counted from the day the invoice arrives"
- Not "the gap is visible rather than silent". Write what the gap is, or cut the sentence
- Not "the lock reaches every entity". Write "the lock date is set in every entity's Xero organisation"

The compression saves four words and costs the reader the mechanism. In a policy the mechanism is the point, so pay the four words.

The same fault in another shape is the noun built from a verb: "on identification of an error" for "when you find an error", "prior to posting" for "before posting", "in the event of a rejection" for "if Xero rejects it". Use the verb.

**Do not give a record a person's verb, or a person a record's verb.** A record, a log or a register does not carry, hold, capture, bear or own anything. It contains things, it records them, it lists them. And a person does not hold a record either: a person is responsible for it. This needs its own line because it survives the check above. "Carries" and "holds" read as ordinary English until you notice that nothing is being carried and nobody is holding anything.

- Not "the record carries the time, the person and what had failed". Write "the record contains the time, the person and what had failed"
- Not "the controller holds the record". Write "the controller is responsible for the records"
- Not "the controller sees every limit and change". Write "the controller is responsible for the log of limits and changes"
- Not "the register owns the approval status". Write "the register lists the approval status"
- Not "the pack sits with the board". Write "the board gets the pack"

Where the point is who is accountable, say that plainly: X is responsible for the logs. Where the point is what is written down, say what the record contains. Do not fuse the two into a verb that does neither.

**Name the thing, do not gesture at it.** "The relevant approval", "the appropriate route", "the applicable threshold" are placeholders that survived into the final draft. Name the approval, the route, the number.

**One aphorism per document, two at most.** "Ceilings, not targets." "One invoice, one bill." A compressed line like that is voice when it is rare and a manifesto when it is on every section. A document with twelve of them does not read as an organisation writing something down, it reads as someone performing a style. If you have written three, keep the best and expand the others into ordinary sentences.

**Make the paragraphs lumpy.** Four lines of procedure next to twelve lines with a table in the middle next to a single line naming an owner. Real documents are uneven because the subjects are uneven. When every paragraph comes out the same length with the same cadence, the evenness itself gives the draft away even though every individual sentence is fine. If three paragraphs in a row have the same shape, one of them is padded and one of them is compressed, so fix both.

**Dull is allowed.** A document earns its keep by being specific, not by being quotable. The procedural middle should read flat. Save the voice for the opener, the carve-outs and the places where a rule cuts against a real pressure.

## The four-beat paragraph

This is the default unit of house prose, and a default is not a template. Nearly every good paragraph does the same four things in the same order.

1. **State the rule flatly.** Short declarative. No hedging.
2. **Give the reason in a short clause.** Usually under ten words, attached with a comma or a full stop rather than "because" or "in order to".
3. **Name the carve-out as normal, not exceptional.** If there is an obvious human exception, say it out loud and say it is fine.
4. **Close with a sentence under ten words.**

The reason clause is what separates this house style from every other policy document. A rule with a reason attached gets followed by people who understand it; a rule without one gets followed only when someone is watching. Give the reason even when it seems obvious to you, and keep it to a clause.

The carve-out beat matters as much. Naming the exception yourself is how the document signals it was written by someone who has done the job. A policy that pretends there are no awkward cases invites people to hide the awkward cases.

Worked example:

> Every purchase goes on a company card or through a purchase order, never a personal card. Reimbursements are slow to process and easy to lose. A personal card is fine in an emergency or where a supplier will not take ours. **Send the receipt the same day.**

Another:

> **The guiding principle is that you are expected to spend the company's money as if it were your own.** The limits below are ceilings, not targets.

Not every paragraph needs all four beats. A prohibition list does not. But if a paragraph has none of them, it is template text and should be cut or rewritten.

**Do not run the four beats through every paragraph in the document.** Applied mechanically it produces the uniform cadence described above, which is the fault the reader notices first. Most of a policy is procedure and belongs in plain sentences with no beats at all. Use the full four where a rule needs defending, and let the rest be flat.

## Signature moves

These are what make the text sound like the organisation rather than like a policy. Use them deliberately, do not use all of them in the same paragraph, and count how many times each one appears in the finished document. Any of them three times over is no longer a voice.

**Colon then principle.** Setup clause, colon, the actual rule in plain words. The colon has to carry substance on both sides.

- "The standard is the same in every entity: a cost sits where the benefit is."
- "If you cannot get a receipt, treat it like any other gap: tell the bookkeeper the same day and say what the charge was for."

Do not use the colon to announce that a principle is coming, or to tell the reader the rule is simple. "The guiding principle is simple:" fails both ways. "The guiding principle is that..." says the same thing without the drumroll.

**"X rather than Y."** The most frequent tic in the set. It reframes an obligation as a choice already made, which is why it lands softer than an instruction and sticks harder.

- "expected rather than optional"
- "agreed rather than assumed"
- "ask rather than posting a guess"
- "a coding question rather than a disciplinary one"

The comma variant compresses it further: "ceilings, not targets" / "Breakfast comes with the hotel, not the meal allowance". It works because both halves land in three or four words. It stops working the moment the negation gets its own full stop and the answer arrives in the next sentence, which is the negation-knockdown failure described below.

Y has to be a real alternative action somebody might take: working around a missing tool, posting quietly, assuming rather than agreeing. Where Y is only the timing the sentence already gave, negated, cut it. If in doubt, cut it.

- Write: "Agree it with your line manager before you book."
- Not: "Agree it with your line manager before you book, not after."
- Write: "Tell your line manager in advance."
- Not: "Tell your line manager in advance rather than after the event."

**The reason as a bare fragment.** Drop the connective and let the reason stand as its own short sentence.

- "Prices climb closer to the date"
- "Once the period is locked nobody can post into it"
- "A receipt left in a jacket pocket is a receipt nobody can match"

Each of those names something concrete that happens. A bare fragment that names an abstraction instead ("The exposure is real", "The risk is asymmetric") is the compression fault from the section above wearing this move as a disguise.

**The stated non-policing principle.** Say what the organisation will not do, so the reader knows where the line actually is. This is the single highest-trust move available, and it only works if it is true.

- "Nobody checks what you ordered for lunch within the allowance."
- "Reporting a coding mistake promptly will never itself be a disciplinary matter."
- "We trust you to spend within the limits without asking each time."

**Naming the awkward thing.** Where a rule cuts against a commercial or social pressure, say so out loud. The reader already knows about the pressure; pretending it does not exist is what makes policies unbelievable.

- "No one is expected to approve a supplier's invoice early to protect the relationship."
- "If a receipt is lost and you are not sure how to handle it, speak to the bookkeeper."

**Very short imperative closers.** End a section on one.

- "If it happens, report it."
- "Ask before you book."
- "If in doubt, ask the controller first."

Two of these in a document, three in a long one. Not one per section. A closer earns its place where the section has told the reader something they might get wrong. After a table of retention periods it is decoration.

## Let the specifics show

The best sentences are ones only this organisation could have written. They are not decorative. Concrete detail is how the reader knows the rule was written for their actual job, and it is what stops a policy being ignored as generic.

- "the month-end close, which runs on the third working day"
- "a card charge in OpCo US for a cost HoldCo bears"
- "the Xero lock date, moved after each quarter's review"
- "a supplier, contractor, landlord or visiting auditor"
- "the bank feed and the bill feed"

Reach for the organisation's own nouns: its entities by name, its systems, its close calendar, its bank accounts, its suppliers, its approval chain. When a rule touches something the group actually does, name it.

The test: **if a paragraph you have written would fit any organisation anywhere, it is not finished.**

## Structure of a document

Every document:

- **Open with an unheaded paragraph**, one to four sentences. Say what the document is for and who it applies to. No "Purpose:" label
- **Headings are `###` and bold.** Title Case. Sub-headings within a section are plain bold, not `###`
- **Bullets do not end with full stops.** Even multi-sentence bullets. Be consistent within the document
- **Where a list is prohibitions, name it as one.** "What must not be posted", "The following travel expenses are not reimbursable"
- **Use a table where the content is a set of items with the same handful of attributes**, such as entities with a currency and a role, or suppliers with what they provide and how they are coded. Prose for anything with a reason in it

The repo's own .md files use sentence-case headings (see `docs/COMMS.md`, "Where this file wins"); the Title Case rule applies to standalone documents written for people outside the repo.

Policies and procedures only:

- **If the group's entities sit in more than one country and local law touches the topic**, close the opener with the local-requirements paragraph. Default wording, which a group may replace with its own: "Where the law of a country an entity operates in sets a different or stricter requirement, that requirement applies in that entity and this policy is read with it." To have the checker confirm a group's standard wording is present, pass it with `--local-law-text`.
- **Close with `### Review`.** Default wording, which a group may replace with its own: "This policy is reviewed every 12 months, and sooner when the group's entities, systems or approval limits change." The checker requires the section; it checks the wording only when you pass the group's standard text with `--review-text`.
- **FAQ is optional.** Include it only if there are real questions people ask. Do not manufacture them

Briefing notes and summaries take neither the local-requirements paragraph nor `### Review`. Where such a document turns on facts that could change, give it a section listing those facts and what each one triggers. That section replaces every counterfactual you were tempted to write into the analysis.

## Mechanics

**Never use em dashes.** Use a spaced hyphen " - ", a colon, or a full stop.

**Spaced hyphen** does two jobs: dash substitute ("Rail is fine and often quicker - use it where it makes sense") and condition-outcome separator in lists ("Flights under 5 hours - Economy"). Use sparingly.

**Numbers are plain digits.** "14 days", "5 hours", "2 weeks' notice". Never the word-then-digit form "five (5) working days".

**Currency follows the number in a document.** "300 USD per night", "45 GBP per day". Never "$300" or "£45". Add "or local equivalent" where a limit applies across entities. (Messages put the code in front, per `docs/COMMS.md`.)

**Metric units.** ISO dates in tables and files.

**Spelling follows the group's convention, consistently.** A group that writes British English uses -ise and -our (recognise, authorised, organisation, behaviour) and runs the checker with `--british`, which flags -ize and -or spellings.

**No italics.** Emphasis is bold only. No "*Note: ...*" asides.

**Bold whole sentences** for the one principle a section turns on. Do not scatter bold across ordinary nouns.

**Contractions are fine** in second-person and FAQ text. Avoid them in prohibition text: "Do not post" beats "Don't post".

## Naming the group

There is a register ladder. Pick deliberately, and do not switch inside a paragraph.

| Form | Use for |
|---|---|
| **The group's full name** (`config.company_name()`) | Opening scope sentence, formal duties and commitments |
| **the Group** | Default in body text and possessives. "the Group's US entity" |
| **the organisation** | The sharpest, most-voiced sentences only |
| **we / our** | Principles, commitments, anything stating a view |
| **the company** | Avoid when the group has several companies; name the entity instead |
| **An unexplained abbreviation** | Never |

**Name the entity when the rule is about one.** A rule about where a cost is booked names the entity by its short name (`config.entity(...).short`), never "the company".

**Second person for anything the reader has to do.** "Book at least 14 days ahead where you can." Third person for what the group does, and for procedural steps involving other parties.

**Name the person and give them the verb.** Where a step has an owner, that owner is the subject of the sentence and the verb is one they actually perform. "The controller reviews the payroll journal monthly" beats "the payroll journal is reviewed monthly by the controller". The passive hides the person who has to do the work, which is the one fact the reader needed.

## Do not write

Vocabulary and constructions that are wrong in a house document whatever else is right about it.

- "It is expected that", "employees should endeavour to", "where appropriate" used to dodge a decision
- "Robust", "comprehensive", "streamline", "leverage", "stakeholders", "seamless", "holistic", "best-in-class"
- "Deep dive", "touch base", "circle back", "going forward", "in order to" (just "to")
- Filler adjectives: key, critical, significant, notable
- Unnamed authority. If you do not know who holds a power, write **[owner to be named]**
- Controls described as if they exist when they do not
- Aspirational padding: "diversity is not only valued but celebrated"
- **An abstract noun doing something physical.** Approvals nest, the threshold is enforced, coverage sits, the clock runs, the gap is visible, the lock reaches. Name the mechanism instead. See "Say it the way you would say it out loud"
- **A record given a person's verb, or a person given a record's verb.** The record carries, the register owns, the pack sits with, the controller holds the record. A record contains things and a person is responsible for it. See "Say it the way you would say it out loud"
- **Overclaiming a control's reliability.** "Any one route is enough on its own", "no code path skips it", "the only control that still works", "a duplicate is never posted". Each of these asserts something nobody has tested, and in several cases the next paragraph admits the opposite. State what the control does and let the gap table carry what it does not
- **The document explaining why it exists.** "This policy is kept in the audit file." "Required by our lenders." A policy states the rule. Where an outside requirement is operative, meaning there is a duty, a deadline or a notification route, write the duty. Do not write that the document itself is on someone's list

## The AI tells

Everything above can be fixed with a find and replace. This section cannot, and it is what actually gives a draft away. Each entry is a shape rather than a phrase, which is why the checker catches only some of them and why the read-through in the workflow is not optional.

They apply to the document, to the covering message, and to how you write back to the person who asked. A reply that fails this section fails whatever it is attached to.

### Shapes that fill space

**Negation set up to be knocked down.** "The supplier's name is not the test. What matters is Y." "This is not a preference, it is how the books work." Write Y and stop. This is the most common way a draft in this style goes wrong, because the pattern feels like voice and reads like a trailer for a sentence that has not arrived yet. The mirrored forms fail the same way: "not just X, but Y", "less about X than about Y", "X is only half the story".
Test - delete the negative half. If the sentence still says everything it needs to, it always did.

**The tacked-on corrective.** "...before anything changes, not after." "...rather than after the event." It scores a point off an imagined reader who was about to do the wrong thing. State the requirement and stop.
Test - is the tail only the thing the sentence already said, negated?

**The ruled-out branch.** The hypothetical that does not apply, followed by the news that it does not apply. "If OpCo EU held a sales tax registration in the US, that would be a live recovery question. With no US registration, that question does not arise on the current facts." Nobody acts on either sentence. State the rule, then state where we land: "OpCo EU holds no US registration, so US sales tax on its invoices is part of the cost." Where a fact would change the answer, it belongs in the monitoring list as a trigger, not in the analysis as a counterfactual.
Test - does a reader do anything differently having read it?

**The elaborating negative list.** A flat statement followed by a run of things it is not. "HoldCo is a holding company with no operations of its own. No sales, no stock, no customers." The first sentence said it. Delete the second.
Test - the statement before the list already carried it.

**The participial tail.** An -ing clause bolted onto a finished sentence to make it sound consequential. "All journals are reviewed, ensuring accountability across the Group." "Requests go to the controller, allowing decisions to be made quickly." The tail is either obvious or a claim the document has not earned.
Test - cut everything from the comma. If the sentence lost nothing, it was decoration.

**The trailing actor clause.** The same shape carrying a real fact, which makes it worse than decoration. A finished sentence, a comma, then a second person bolted on with a bare -ing verb and no clause of their own. "The accruals are reviewed monthly by the controller and approved by the finance director, the board receiving the pack." "The change goes to the finance director, the auditor being copied." Nobody says this out loud, and it buries a real obligation in the weakest position in the sentence, which is where the person who has to do it will miss it.

Give every actor their own clause, with the person as the subject and a verb they perform. "The controller reviews the accruals monthly, the finance director approves them, and the board gets the pack."
Test - is every named person the subject of a verb, or is one of them dangling off the end of somebody else's sentence?

**The closing summary.** A final paragraph or sentence that restates what the document just said, or a covering note that recaps its own contents. Close on the last concrete point.
Test - does the last paragraph contain a fact, a rule or an owner that appears nowhere else?

### Shapes that give away the author

**Announcing your own clarity.** "The guiding principle is simple:", "Put simply,", "To be clear,", "It is worth noting that", "Importantly," where nothing was unclear and nothing else was unimportant. If the sentence is simple the reader will notice.

**The colon reveal.** Setup, colon, one dramatic noun phrase. "The real constraint: the lock date." The colon-then-principle move in Signature Moves needs substance on both sides. A bare noun after the colon is a headline, not a sentence.

**The rule of three.** Three parallel items where the group has two, or four, or one. Triads are the single most reliable sign that a list was generated rather than counted. Lists should be lopsided: two items, or five of visibly different lengths, because that is what the facts look like.
Test - did I write three because there are three, or because three sounded finished?

**Stacked hedges.** "It may be worth considering whether...", "generally tends to", "in most cases usually". One hedge does the hedging. Two means the writer did not want to find out the answer.

**Synonym cycling.** If the word is "bill", write "bill" every time. Rotating through "bill", "invoice", "document", "item" to avoid repetition reads as padding and makes a rule ambiguous.

**Manufactured transitions.** "Additionally", "Moreover", "Furthermore", "That said", "Notably", "Crucially", "Ultimately". They imply a logical turn the paragraphs are not making. Start the sentence.

**The correction echoed back.** Answering an instruction or an edit by listing it back as noun-phrase fragments and closing on a short line. "Understood on the edits - I've read them. The tails explaining why a thing matters, the X framing, Y instead of Z. Cutting all of that." Nothing in it is new to the person who wrote the instruction. Do the thing. Say what you did afterwards, or say nothing.

**The acknowledgement opener.** "Understood.", "Got it.", "Noted.", "Fair point.", "You're right." Start with the work.

**The staccato fragment list in prose.** Three noun phrases separated by full stops, standing in for a sentence. "The tails. The framing. The word choice." Write the sentence.

### The one that matters most

A paragraph that would fit any organisation anywhere is the tell that survives every fix above. It is also the only one that cannot be caught by pattern. Read each paragraph and ask whether the group appears in it: an entity, an office, a system, a bank, a close date, a real number. If not, the paragraph is not finished.

### Negative control, three directions

If your draft reads like either of these, start again:

> "Finance activities will be aligned with the strategic objectives and needs of the organisation."

> "The finance team will collaborate with all managers to aggregate reporting needs and prioritise them based on organisational objectives."

Nothing there states a view, gives a reason, names a carve-out, or ends short.

There is a second failure mode at the opposite extreme, and it is the one a well-intentioned draft in this style actually falls into. If your draft reads like this, start again too:

> "Approvals nest. The manager limit binds first, the controller limit second, the board limit last. The threshold is enforced in the system rather than by judgement, and the gap is visible rather than silent. One invoice, one approval."

Every sentence there is short, every one is confident, and a reader learns nothing they can act on. Nobody says "approvals nest".

The third is the quietest, and the hardest to catch, because the sentences are grammatical and every fact in them is right:

> "The accruals are reviewed monthly by the controller and approved by the finance director, the board receiving the pack. The record carries the date, the person and what had changed, and the controller holds it."

It fails because the two people who do the work are hidden in "by" phrases, the third is dangling off a comma in an -ing clause, and the record has been given a verb that belongs to a person. Write it as: "The controller reviews the accruals monthly, the finance director approves them, and the board gets the pack. The record contains the date, the person and what had changed. The controller is responsible for the records."

## Length

Shorter is better. A good single-topic policy runs a few hundred words and covers everything. If a rule takes two sentences, do not write four.

Length by document type, as a target rather than a limit: a single-topic operational policy (travel, expenses, device use) runs 300 to 600 words. A conduct or behaviour policy that has to cover reporting routes and carve-outs runs 600 to 1000. A briefing note or summary runs 150 to 400; where it needs tables, the tables take what they need and the prose holds to those limits. Anything over 1200 words of prose needs a reason you could defend out loud. A rules file in `rules/` is as long as the rules are, one rule per paragraph or row, with no padding between them.

Shortness is not the same as compression. Cut whole sentences that do no work, rather than squeezing a working sentence until the mechanism disappears from it.

## Workflow

1. **Establish the facts first.** Limits, thresholds, notice periods, who approves what, which entities are in scope, whether any local law applies, what the current status of each item is. If the user has not supplied them, ask rather than inventing plausible-looking numbers. A document with invented thresholds is worse than a short one with `[owner to be named]` and a gap flagged.
2. **Sketch the sections** before writing prose. Four to seven `###` sections carries most documents.
3. **Draft.** Use the four beats where a rule needs defending and plain sentences everywhere else.
4. **Run the checker.** `python3 .claude/skills/writing-style/scripts/check_style.py <file>` catches the mechanical failures: em dashes, currency symbols, banned words, bullets ending in full stops, negation-knockdown patterns, tacked-on correctives, elaborating negative lists, the document explaining its own existence, unnamed authority, inherited template language (and any words you pass with `--avoid`). Opt-in checks: American spellings with `--british`, and the group's own standard Review and local-law wording with `--review-text` and `--local-law-text`. Run it every time rather than only when you are unsure. Two known gaps: it assumes every document is a policy, so ignore its missing-`### Review` finding on a briefing note, rules file or summary (or pass `--not-policy`); and it does not catch ruled-out branches, so check for those yourself using the test in The AI Tells.
5. **Read it aloud, or at least sound it out.** This pass catches the register failures, and the checker cannot do any of it. Four questions: is there a sentence here nobody in the organisation would actually say, is any abstraction doing something physical, has a record been given a verb that belongs to a person, and do three paragraphs in a row have the same shape? Then count the aphorisms and the short closers and cut back to two of each.
6. **Find every name and check it is doing something.** Each named person or role should be the subject of a verb they perform. Rewrite anyone who turns up only in a "by X" phrase or dangling off a comma in an -ing clause.
7. **Run the slop pass.** The checker sees none of the shapes in The AI Tells that are counted rather than matched: rule-of-three lists, participial tails, trailing actor clauses, synonym cycling, manufactured transitions, the closing summary. Read the draft once looking only for those. Count the items in every list and ask whether the number came from the facts.
8. **Read the draft once for voice.** Does every rule that needs one have a reason attached, would any paragraph fit any organisation anywhere, and is the procedural middle allowed to be flat?
9. **Read your covering message too.** It is the first thing the reader sees, and The AI Tells applies to it in full. No acknowledgement opener, no echoing the instruction back, no fragment list, no recap of what you just attached.
10. **Cut.** The first draft is always long. Look for the sentence that repeats the one before it in different words.

## Output

Ask where it should land if the user has not said. A rule the agent follows goes in `rules/` or `docs/` in this repo. A document for people goes to a markdown file when the user wants to review a draft first, or to whatever document tool the user names.

## Rewriting an existing document

Rewrites are more common than new drafts, and the failure mode is different: template language survives because it looks like policy. Read `references/rewrite.md` before starting one. It has the strip list, the order to work in, and how to report what changed. It applies to any document, not only policies.

One warning specific to rewrites. When you replace a phrase that breaches one rule, check the replacement against this whole file before you write it. "The controller sees both" is vague, but "the controller holds the record" is worse, and swapping the first for the second is a rewrite that went backwards. If you cannot find a phrasing someone would say out loud, leave the original and flag it.

## Reference files

- `references/rewrite.md` - rewrite and audit mode. Read before revising an existing document
- `references/worked-examples.md` - one full policy in house style, plus before-and-after pairs. Read when you want to calibrate the voice, especially for a document type you have not written before
- `scripts/check_style.py` - mechanical checker. Run on every draft, with the two gaps noted in step 4 of the workflow
