Part of the `xero` skill, read when its SKILL.md says it applies. It is as
binding as the skill itself, and the skill's safety rules apply to
everything here.

## Talking to people on Slack

Runs may be triggered from Slack and questions go back the same way. Full model
in the `xero-bills` skill, section 0. The short version:

- **Admins** (`config.slack().admins`) start runs and receive the report. A run
  belongs to whoever started it: that is who gets its questions and its
  summary.
- **Users** (`config.slack().users`) supply information and answer questions,
  but cannot start a run and never see the report. DM them directly for the
  thing only they know; then tell the admin what they said and what you did
  with it. **Read-only** members (`config.slack().readonly`) may also ask
  `query <question>`, answered by a read-only session.
- Who to chase for a missing invoice: `config.section("slack").get("chase_routing", {})`,
  applied per the `xero-bills` skill, "Missing invoices". Never message anyone
  in `config.section("slack").get("never_message", [])`.
- Ask questions *as you hit them*, not batched at the end, and never block
  waiting for an answer. Replies arrive as context on the next run.
- Post with `chat.postMessage` and a bare user ID as `channel`, using
  `SLACK_BOT_TOKEN` from `.env`. `conversations.open` may fail with
  `missing_scope`; posting to a bare user ID works either way.
- **The channel** (`config.slack().channel_id`, named
  `config.slack().channel_name`) takes a bookkeeping run's report (headline top
  level, whole report in its thread) and any manual action the user has to
  take by hand, the `bill-payments` reconcile-to-intercompany line above all,
  in the same shape from every run: a `MANUAL ITEMS (date, time)` headline
  top level, the items as the first reply in its thread. Nothing else goes
  there: a query, a focused run, a status reply and a chase all stay with the
  person who asked. Rules in `docs/COMMS.md`, "Where a message goes".
