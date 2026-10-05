# Architecture

How the agent is put together: how data gets in, how it is processed, what
comes out, and **where you put your own information**. The written checklist
for adapting the agent to your group is [ADAPTING.md](ADAPTING.md), and the full list of inputs is [INPUTS.md](INPUTS.md).

Colour key used in every diagram: **yellow** you fill in, **red** secrets or
safety, **blue** shipped agent machinery, **purple** Claude's decisions,
**grey** external services, **green** what the agent produces.

To re-render a diagram after editing its Mermaid source below, save the block
to a file and run `npx @mermaid-js/mermaid-cli -i diagram.mmd -o
docs/images/<name>.png -s 2 -b white`. The overview image is built by
`python3 docs/images/src/build_overview.py`, then the SVG is rendered to PNG
at 2x.

---

## 1. The whole system on one page

![How data gets in, gets processed, and gets out](docs/images/architecture-overview.png)

**In one sentence:** Slack messages and cron start headless Claude Code
sessions on a server. Each session loads one domain's skill, reads your
group's structure from `config/group.toml` and your bookkeeping judgement
from `rules/`, works through Python clients against Xero, Gmail, Drive and
the banks, writes Excel workbooks, and reports back into one Slack channel.

---

## 2. Where YOUR information goes

Three places, and only three. Everything else is shipped code and method.

![Where your information goes](docs/images/your-information.png)

<details><summary>Diagram source (Mermaid, edit this and re-render)</summary>

```mermaid
flowchart TB
    G["config/group.toml<br/><b>STRUCTURE</b>"]:::you
    R["rules/*.md<br/><b>JUDGEMENT</b>"]:::you
    S[".env and token files<br/><b>ACCESS</b>"]:::secret

    subgraph GDETAIL["config/group.toml holds"]
        G1["[company]<br/>name, currency, inbox"]:::you
        G2["[[entities]]<br/>Xero org names, codes, currencies"]:::you
        G3["[[entities.bank_accounts]]<br/>provider, slug, CSV cut-over"]:::you
        G4["[entities.payroll_controls]<br/>control accounts"]:::you
        G5["[[intercompany]]<br/>every loan pair and its codes"]:::you
        G6["[slack]<br/>channel and the three tiers"]:::you
        G7["[accounts_payable] [reports]<br/>[drive] [[domains]]"]:::you
        G1 ~~~ G2 ~~~ G3 ~~~ G4 ~~~ G5 ~~~ G6 ~~~ G7
    end

    subgraph RDETAIL["rules/ holds"]
        R1["GROUP.md<br/>which entity recognises which cost"]:::you
        R2["EXPENSES.md<br/>coding, tax, prepay, accrue"]:::you
        R3["ALLOCATION.md<br/>shared cost splits"]:::you
        R4["SUPPLIERS.md<br/>the supplier database"]:::you
        R5["PAYROLL.md<br/>staff and payroll logic"]:::you
        R6["INTERCOMPANY.md<br/>interco routing policy"]:::you
        R7["entities/KEY.md<br/>one file per entity"]:::you
        R1 ~~~ R2 ~~~ R3 ~~~ R4 ~~~ R5 ~~~ R6 ~~~ R7
    end

    subgraph SDETAIL["Access"]
        S1["Xero app ID and secret<br/>.xero/tokens.json"]:::secret
        S2["Slack bot and app tokens"]:::secret
        S3["Gmail and Drive OAuth tokens"]:::secret
        S4["Revolut keys, Mercury tokens<br/>per bank slug"]:::secret
        S5["Anthropic API key"]:::secret
        S1 ~~~ S2 ~~~ S3 ~~~ S4 ~~~ S5
    end

    G --> G1
    R --> R1
    S --> S1

    G7 -->|"read by every script<br/>via accounting_agent.config"| CODE["Feeds, reports, interco workbook,<br/>Slack permissions, schedule prompts"]:::code
    R7 -->|"read by the skills<br/>at decision time"| JUDGE["Which entity, which account,<br/>which supplier rule, how to split,<br/>how to book payroll"]:::code
    S5 -->|"read by src/accounting_agent"| CONN["Connectors"]:::code

    classDef you fill:#fff3bf,stroke:#e67700,stroke-width:2px,color:#000
    classDef secret fill:#ffe3e3,stroke:#c92a2a,stroke-width:2px,color:#000
    classDef code fill:#e7f5ff,stroke:#1971c2,color:#000
```

</details>

| You want to... | Edit | Picked up by |
|---|---|---|
| Add or remove an entity | `config/group.toml` `[[entities]]` + a `rules/entities/<KEY>.md` | every feed, report, the interco matrix, Slack prompts, the schedule |
| Add a bank account | `[[entities.bank_accounts]]` (+ credentials in `.env`) | bank feed, bills report, bank fees, phantom check |
| Add an intercompany loan | `[[intercompany]]` | interco recon, the matrix, one new line-by-line tab, FX revaluation |
| Say who books which cost | `rules/GROUP.md`, `rules/ALLOCATION.md` | the bookkeeping run (`xero-bills`) |
| Code a cost category to an account | `rules/EXPENSES.md` | the bookkeeping run, the review |
| Teach it a supplier | `rules/SUPPLIERS.md` | the bookkeeping run, the bills report |
| Change payroll handling | `rules/PAYROLL.md` + `[entities.payroll_controls]` | bookkeeping, the payroll control check |
| Give someone Slack access | `[slack.admins]` / `[slack.users]` / `[slack.readonly]` | the listener, on restart |

Admins can also change `rules/` from Slack: a ruling sent to the bot is
written into the right rules file by the run and committed on the server
(`deploy/commit-writeback.sh`).

---

## 3. Repository map

![Repository map](docs/images/repo-map.png)

<details><summary>Diagram source (Mermaid, edit this and re-render)</summary>

```mermaid
flowchart LR
    ROOT["repo root"]:::code
    ROOT --> CLM["CLAUDE.md<br/>rulebook every session reads"]:::code
    ROOT --> ARC["ARCHITECTURE.md<br/>this file"]:::code
    ROOT --> CONF["config/<br/>group.example.toml<br/>group.toml"]:::you
    ROOT --> RULES["rules/<br/>README, GROUP, EXPENSES, ALLOCATION,<br/>SUPPLIERS, PAYROLL, INTERCOMPANY,<br/>entities/"]:::you
    ROOT --> INP["INPUTS.md<br/>START HERE: every piece of<br/>information you provide"]:::you
    ROOT --> ADP["ADAPTING.md<br/>where each piece goes,<br/>setup path"]:::you
    ROOT --> DOCS["docs/<br/>COMMS, BANKING,<br/>REVIEW_METHOD, INTERCOMPANY_RECON,<br/>XERO and REVOLUT API references,<br/>setup/, bookkept/ (runtime registers,<br/>server only, gitignored)"]:::code
    ROOT --> CL[".claude/<br/>settings.json, skills/,<br/>hooks/, agents/"]:::code
    ROOT --> SRC["src/accounting_agent/<br/>config, xero/, revolut/, mercury/,<br/>gmail_auth, gdrive, publish, readonly"]:::code
    ROOT --> SCRIPTS["scripts/<br/>runners the skills call"]:::code
    ROOT --> DEP["deploy/<br/>session runners, cron, systemd,<br/>install, preflight, sync"]:::code
    ROOT --> DAT["data/<br/>runtime state, gitignored"]:::out
    ROOT --> SEC[".env, .xero/, .gmail/,<br/>.gdrive/, .revolut/"]:::secret

    classDef you fill:#fff3bf,stroke:#e67700,stroke-width:2px,color:#000
    classDef secret fill:#ffe3e3,stroke:#c92a2a,stroke-width:2px,color:#000
    classDef code fill:#e7f5ff,stroke:#1971c2,color:#000
    classDef out fill:#ebfbee,stroke:#2f9e44,color:#000
```

</details>

---

## 4. Domains, skills and what they load

One domain per run. A run that is doing bookkeeping does not read report
logic, and the other way round, so a decision in one is never made on the
other's rules. Domains are listed in `config/group.toml` `[[domains]]`; add
your own there.

![Domains and skills](docs/images/domains.png)

<details><summary>Diagram source (Mermaid, edit this and re-render)</summary>

```mermaid
flowchart TB
    subgraph BK["Domain: Bookkeeping (skill xero-bills)"]
        XB["xero-bills<br/>invoice intake, coding, AUTHORISED bills"]:::code
        BP["bill-payments<br/>open bills vs bank lines"]:::code
        XR["xero-reconcile<br/>bank reconciliation"]:::code
        XV["xero-review<br/>health check"]:::code
        XI["xero-interco<br/>line-by-line interco recon"]:::code
        XF["xero-interco-fx<br/>FX revaluation: draft daily,<br/>post on an admin's Slack instruction"]:::code
        IE["agent: invoice-extract<br/>one document to JSON"]:::code
    end

    subgraph RP["Domain: Standing reports (skill reports)"]
        RC["reports<br/>catalogue router"]:::code
        RB["report-bills"]:::code
        RPP["report-prepayments"]:::code
        RA["report-accruals"]:::code
        RD["report-deposits"]:::code
    end

    subgraph ALL["Loaded alongside any domain"]
        X["xero<br/>API reference, patterns, safety rules"]:::code
        OI["outstanding-items<br/>the register of open items"]:::code
        WS["writing-style<br/>how every message reads"]:::code
        SA["sync-all<br/>laptop = git = server"]:::code
    end

    XB --> IE
    XB --> BP
    RC --> RB & RPP & RA & RD

    XB -.reads.-> RULESX["rules/GROUP, EXPENSES, ALLOCATION,<br/>SUPPLIERS, PAYROLL, INTERCOMPANY,<br/>entities/*"]:::you
    XV -.reads.-> RULESX
    XI -.reads.-> CFGX["config/group.toml [[intercompany]]"]:::you
    BP -.reads.-> CFGX2["config bank accounts, AP settings"]:::you

    classDef you fill:#fff3bf,stroke:#e67700,stroke-width:2px,color:#000
    classDef code fill:#e7f5ff,stroke:#1971c2,color:#000
```

</details>

---

## 5. A bookkeeping run, step by step

![A bookkeeping run step by step](docs/images/bookkeeping-run.png)

<details><summary>Diagram source (Mermaid, edit this and re-render)</summary>

```mermaid
flowchart TD
    START(["Trigger:<br/>cron 'daily' or admin 'run ...' in Slack"]):::ext
    H["UserPromptSubmit hook injects<br/>the bookkeeping checklist"]:::code
    L1["Load skills: xero-bills + xero"]:::code
    BF["Read data/bankfeed/ENTITY.md<br/>then bankfeed.py refresh all"]:::code
    BI["billfeed.py refresh all<br/>every AUTHORISED unpaid bill"]:::code
    LG["ledger_prune.py, read docs/bookkept/LEDGER.md"]:::code
    SW["7-day completeness sweep<br/>Slack app messages + Gmail inbox"]:::code
    EX["Fan out invoice-extract<br/>(Haiku) per document"]:::code
    DEC{"Decide per invoice<br/>using rules/"}:::you
    DUP{"Duplicate guard<br/>already in Xero?"}:::code
    POST["Create AUTHORISED bill<br/>+ attach document<br/>+ idempotency key"]:::code
    LBL["Gmail label, ledger line,<br/>remove bank feed line"]:::code
    Q["Query / manual / blocked<br/>outstanding.py add"]:::code
    BPAY["bill-payments check<br/>open bill paid by another entity?<br/>payer spend money to interco loan"]:::code
    RF["bankfeed.py refresh all (again)"]:::code
    PH{"check_no_phantom_payments.py<br/>PASS?"}:::code
    REP(["Report to the Slack channel:<br/>header + report in thread"]):::out
    FIX["Stop and report the failure"]:::out

    START --> H --> L1 --> BF --> BI --> LG --> SW --> EX --> DEC
    DEC -->|"clear"| DUP
    DEC -->|"unclear"| Q
    DUP -->|"no"| POST --> LBL
    DUP -->|"yes"| LBL
    LBL --> BPAY
    Q --> BPAY
    BPAY --> RF --> PH
    PH -->|"yes"| REP
    PH -->|"no"| FIX

    classDef you fill:#fff3bf,stroke:#e67700,stroke-width:2px,color:#000
    classDef code fill:#e7f5ff,stroke:#1971c2,color:#000
    classDef ext fill:#f1f3f5,stroke:#495057,color:#000
    classDef out fill:#ebfbee,stroke:#2f9e44,color:#000
```

</details>

The agent **never creates a payment**. It stops at an AUTHORISED, unpaid
bill with its document attached; the bank feed imports into Xero on its own,
and a person matches the statement line to the bill.

---

## 6. The bank as the source of truth

![The bank as the source of truth](docs/images/bank-truth.png)

<details><summary>Diagram source (Mermaid, edit this and re-render)</summary>

```mermaid
flowchart LR
    subgraph PERBANK["Per bank account in config/group.toml"]
        direction TB
        C1["statement_csv<br/>data/statements/*.csv"]:::you
        C2["csv_until<br/>cut-over date"]:::you
        C3["provider + slug<br/>revolut / mercury / csv"]:::you
    end

    C1 -->|"movements on or before csv_until"| MERGE
    C3 -->|"movements after csv_until,<br/>read-only API"| MERGE
    MERGE["Statement lines"]:::code

    MERGE --> BANKFEED["data/bankfeed/ENTITY.md<br/>every line not yet in Xero"]:::out
    XEROB["Xero: AUTHORISED unpaid bills"]:::ext --> BILLFEED["data/billfeed/ENTITY.md"]:::out

    BANKFEED --> MATCH{"bill-payments check<br/>bills_report.py"}:::code
    BILLFEED --> MATCH
    MATCH -->|"same entity paid"| M1["MATCH IN XERO<br/>user reconciles by hand"]:::out
    MATCH -->|"other entity paid"| M2["INTERCOMPANY<br/>payer's interco leg posted,<br/>user reconciles to interco"]:::out
    MATCH -->|"no movement"| M3["UNPAID"]:::out
    MATCH -->|"ambiguous"| M4["VERIFY / DUPLICATE?"]:::out

    classDef you fill:#fff3bf,stroke:#e67700,stroke-width:2px,color:#000
    classDef code fill:#e7f5ff,stroke:#1971c2,color:#000
    classDef ext fill:#f1f3f5,stroke:#495057,color:#000
    classDef out fill:#ebfbee,stroke:#2f9e44,color:#000
```

</details>

When Xero and the bank disagree, the bank wins and Xero is what gets
corrected. Goal: every cash movement on every bank account becomes a bill,
spend money or transfer in Xero. Method: [docs/BANKING.md](docs/BANKING.md).

---

## 7. Intercompany: driven entirely by config

Add an `[[intercompany]]` block and the next run reconciles it and gives it
its own tab. Nothing in code names an entity or an account.

![Intercompany reconciliation](docs/images/intercompany.png)

<details><summary>Diagram source (Mermaid, edit this and re-render)</summary>

```mermaid
flowchart LR
    P["config/group.toml<br/>[[intercompany]]<br/>a, b, flavour, accounts"]:::you
    E["config/group.toml<br/>[[entities]] base currencies"]:::you
    S["[intercompany_settings]<br/>tolerances, FX account"]:::you

    P & E & S --> REC["scripts/interco_recon.py<br/>rebuild each side's GL from documents,<br/>tie to the Balance Sheet,<br/>match transaction by transaction"]:::code
    REC --> CLS["Classify every break<br/>one-sided, wrong flavour,<br/>FX translation, timing, missing mirror"]:::code
    CLS --> MAT["scripts/interco_matrix.py"]:::code
    MAT --> WB["Intercompany Reconciliation.xlsx"]:::out
    WB --> T1["Summary: entity x entity matrix"]:::out
    WB --> T2["One line-by-line tab<br/>per configured pair"]:::out
    CLS --> BRK["scripts/interco_breaks.py<br/>open items; confirmed matches<br/>paired first on the next run"]:::code
    BRK --> REC
    CLS --> FX["scripts/interco_fx.py<br/>draft FX revaluation<br/>(refuses while anything is one-sided)"]:::code
    FX --> POST["run post the fx interco journals<br/>admin, from Slack, never cron:<br/>every ready pair at the last month end"]:::you

    classDef you fill:#fff3bf,stroke:#e67700,stroke-width:2px,color:#000
    classDef code fill:#e7f5ff,stroke:#1971c2,color:#000
    classDef out fill:#ebfbee,stroke:#2f9e44,color:#000
```

</details>

---

## 8. Slack: three tiers, one agent per thread

![Slack permission tiers](docs/images/slack-tiers.png)

<details><summary>Diagram source (Mermaid, edit this and re-render)</summary>

```mermaid
flowchart TB
    MSG["Slack message"]:::ext --> WL{"On the whitelist?<br/>config [slack]"}:::you
    WL -->|"no"| IGN["Ignored entirely"]:::out
    WL -->|"admin"| A["Every command:<br/>run, query, status, notes, clear,<br/>consensus. Rulings change rules/.<br/>Receives run reports."]:::code
    WL -->|"user"| U["Captured as shared input, acked.<br/>No commands. Evidence, never instruction."]:::code
    WL -->|"read-only"| R["User + 'query question'.<br/>Read-only session answers in thread."]:::code

    A --> TH{"Top-level message<br/>or thread reply?"}:::code
    TH -->|"top level"| NEW["New agent: own session,<br/>lock and log (run-thread.sh)"]:::code
    TH -->|"in thread"| RES["Same agent resumes,<br/>queued if mid-turn"]:::code
    R --> RQ["run-query.sh<br/>AGENT_READONLY=1"]:::code

    classDef you fill:#fff3bf,stroke:#e67700,stroke-width:2px,color:#000
    classDef code fill:#e7f5ff,stroke:#1971c2,color:#000
    classDef ext fill:#f1f3f5,stroke:#495057,color:#000
    classDef out fill:#ebfbee,stroke:#2f9e44,color:#000
```

</details>

---

## 9. Safety: enforced in code, not prompts

![Safety enforced in code](docs/images/safety.png)

<details><summary>Diagram source (Mermaid, edit this and re-render)</summary>

```mermaid
flowchart LR
    subgraph PAY["Payment ban"]
        direction TB
        P1["Hook: Bash pattern deny<br/>.claude/settings.json"]:::code
        P2["Hook: guard-payments.sh<br/>Bash and any .py written"]:::code
        P3["Library: no payment writers<br/>in xero/banking.py"]:::code
        P4["Exit check:<br/>check_no_phantom_payments.py"]:::code
    end
    subgraph RO["Read-only sessions"]
        direction TB
        R1["Transport: AGENT_READONLY=1<br/>XeroClient refuses non-GET"]:::code
        R2["CLI: Write/Edit disallowed"]:::code
        R3["Hook: guard-readonly.py"]:::code
    end
    subgraph BANKS["Bank APIs"]
        direction TB
        B1["Revolut and Mercury clients<br/>refuse every non-GET"]:::code
    end
    subgraph DUPS["Duplicates"]
        direction TB
        D1["Xero duplicate guard<br/>before every create"]:::code
        D2["Idempotency key on every create"]:::code
    end

    classDef code fill:#e7f5ff,stroke:#1971c2,color:#000
```

</details>

---

## 10. The schedule (UTC)

![The schedule](docs/images/schedule.png)

<details><summary>Diagram source (Mermaid, edit this and re-render)</summary>

```mermaid
flowchart LR
    subgraph DAILY["Mon-Sat 02:00 'daily' (chained, one session at a time)"]
        direction LR
        D1["Bookkeeping run"]:::code --> D2["Interco recon<br/>(read-only)"]:::code --> D3["Accounts payable check"]:::code
    end
    D3 --> BILLS["Mon-Fri 'bills'<br/>Bills Payable.xlsx<br/>+ UNPAID DM"]:::out
    D3 --> BAL["Mon-Sat 'balance-reports'<br/>prepayments, accruals, deposits,<br/>bank fees, summary post"]:::out
    REV["Wed + Sun 10:00 'review'<br/>full bookkeeping review"]:::code
    SWEEP["every 10 min retry-sweep<br/>re-runs jobs deferred by the<br/>Xero daily rate limit"]:::code
    PH["23:00 nightly phantom payment check"]:::code

    classDef code fill:#e7f5ff,stroke:#1971c2,color:#000
    classDef out fill:#ebfbee,stroke:#2f9e44,color:#000
```

</details>

Jobs are chained, not run side by side, so two sessions are never in Xero at
once. Times and prompts: `deploy/crontab.example`, `deploy/run-scheduled.sh`.

---

## 11. Three copies of the repo

![Three copies of the repo](docs/images/repo-copies.png)

<details><summary>Diagram source (Mermaid, edit this and re-render)</summary>

```mermaid
flowchart LR
    LAP["Laptop<br/>(the bridge)"]:::code <-->|"push / pull"| GIT["Git host (private)<br/>remote: origin"]:::ext
    LAP <-->|"ssh: collect write-backs,<br/>fast-forward"| SRV["Server<br/>remote: server"]:::code
    SRV -.->|"commits admin rulings<br/>commit-writeback.sh"| SRV

    classDef code fill:#e7f5ff,stroke:#1971c2,color:#000
    classDef ext fill:#f1f3f5,stroke:#495057,color:#000
```

</details>

`deploy/sync-all.sh --dry-run` shows which copy has what; without the flag it
collects the server's uncommitted work, merges, pushes and fast-forwards the
server so all three are one commit.
