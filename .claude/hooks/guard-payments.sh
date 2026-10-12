#!/usr/bin/env bash
# PreToolUse guard: the agent must NEVER create or allocate a Xero payment.
#
# Rule: xero skill safety rule 7 / CLAUDE.md / rules/GROUP.md. The agent
# creates the AUTHORISED bill with its document attached and leaves it UNPAID;
# a person matches the imported bank statement line to it by hand. An
# agent-created payment is a phantom unreconciled bank entry that duplicates
# the real feed line.
#
# Covers two entry points:
#   Bash            - inline/heredoc python that calls a payment function
#   Write/Edit *.py - writing a script that would do it when later executed
#                     (.md is deliberately exempt so the rule can be documented)
# Payment DELETION is not blocked but is downgraded to "ask", because deleting
# a payment that is already reconciled destroys the user's own matching work.
set -uo pipefail

# Fail closed: without jq the payload cannot be read, so nothing is let through.
if ! command -v jq >/dev/null 2>&1; then
  cat >/dev/null
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"install jq: the payment guard cannot run without it. Every Bash, Write and Edit call is denied until jq is on PATH (apt-get install jq, brew install jq)."}}'
  exit 0
fi

payload=$(cat)
tool=$(printf '%s' "$payload" | jq -r '.tool_name // ""')

case "$tool" in
  Bash)
    subject=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""')
    ;;
  Write|Edit|MultiEdit)
    path=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // ""')
    case "$path" in
      *.py)
        subject=$(printf '%s' "$payload" | jq -r \
          '[.tool_input.content, .tool_input.new_string, (.tool_input.edits[]?.new_string)]
           | map(select(. != null)) | join("\n")')
        ;;
      *) exit 0 ;;
    esac
    ;;
  *) exit 0 ;;
esac

CREATE='create_payment|create_payments_batch|create_batch_payment'
CREATE="$CREATE"'|allocate_prepayment|allocate_overpayment|allocate_credit_note'
CREATE="$CREATE"'|mark_reconciled|is_reconciled[[:space:]]*=[[:space:]]*True'
CREATE="$CREATE"'|(put|post|request)\([^)]{0,40}Payments'
REMOVE='delete_payment|delete_batch_payment'

emit() { # $1 = decision, $2 = reason
  jq -n --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:$d,permissionDecisionReason:$r}}'
  exit 0
}

if printf '%s' "$subject" | grep -qE "$CREATE"; then
  emit deny "BLOCKED by project rule (xero skill safety rule 7): never create or allocate a payment, and never mark a bill paid or reconciled. Your job ends at an AUTHORISED bill with the source document attached and AmountDue still equal to the total. A person matches the imported bank statement line to it by hand in Xero. A payment you create is a phantom unreconciled bank entry that duplicates the real feed line. Do NOT rewrite this call to evade the check, and do not move the code into a file to run later: that is blocked too. Remove the payment step and say which bills you left unpaid. Payroll bills cleared from control accounts (rules/PAYROLL.md): the agent posts the bill; clearing it from the control accounts is a payment the user posts by hand. Propose the amount per control account, never post it, even with approval."
fi

if printf '%s' "$subject" | grep -qE "$REMOVE"; then
  emit ask "CAUTION: this deletes a Xero payment. Deleting a payment that is already reconciled destroys the user's own bank-matching work and re-opens the bill. Confirm the payment is unreconciled (IsReconciled == false) AND was agent-created before proceeding."
fi

exit 0
