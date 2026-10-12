# Xero Accounting API Reference

Comprehensive reference for the Xero Accounting API, compiled for the accounting agent. Sourced from developer.xero.com and the official XeroAPI/Xero-OpenAPI specification (accounting spec v17). Compiled 2026-08-18. Items marked "(verify)" could not be fully confirmed against current official documentation and should be validated against a live tenant or Xero support before being relied on.

**Use cases this reference supports:**

- Bank reconciliation support (the agent pre-codes the Xero side so a person can match the statement lines; it never creates payments or reconciles)
- Journal entries (creating, posting, voiding manual journals)
- Posting, correcting, and editing transactions (invoices, bills, payments, bank transactions)
- Reviewing accounts against pre-set criteria across all group entities (reports, journals feed, chart of accounts)

## Contents

1. [API Foundations](#1-api-foundations), app types, OAuth 2.0, scopes, conventions, rate limits, errors, SDKs
2. [Banking & Reconciliation](#2-banking--reconciliation), bank transactions, payments, transfers, prepayments/overpayments, bank feeds, what "reconcile" means via API
3. [Sales & Purchases](#3-sales--purchases), invoices, credit notes, quotes, purchase orders, repeating invoices, items, correction patterns
4. [Ledger & Organisation Setup](#4-ledger--organisation-setup), manual journals, journals feed, accounts, tax rates, tracking, currencies, organisation, users, budgets
5. [Contacts, Reports & Platform Features](#5-contacts-reports--platform-features), contacts, reports, attachments, history & notes, webhooks, adjacent APIs

## Key constraints to know up front

- **This agent never pays.** The payment, batch payment and allocation endpoints are documented below because the agent reads them, but the code (`src/accounting_agent/xero/banking.py`) deliberately has no helper that creates a payment, allocates a prepayment, overpayment or credit note, or force-marks a transaction reconciled, and the project hooks block attempts to add one. Bills stay AUTHORISED and unpaid; a person matches the bank statement line to them in Xero.

- **There is no public API for bank statement lines or the reconcile action.** The Bank Feeds API (which creates statement lines) is a closed API restricted to approved financial institutions. Practical automation: pre-create fully coded BankTransactions/Payments so a human only clicks "OK"/Match in the UI, or set `IsReconciled=true` on transactions for accounts with no feed/import.
- **One access token serves all connected organisations** (standard OAuth app): each API call targets one entity via the `xero-tenant-id` header. Custom Connections are one-org-each and only sold in UK/AU/NZ/US.
- **Rate limits are per tenant** (60/min, 5,000/day), so a multi-entity group effectively multiplies capacity, but per-entity daily sweeps must budget calls.
- **Scope migration (2026):** Xero is moving to granular scopes; broad scopes (e.g. `accounting.transactions`) are deprecated but usable until September 2027. New Custom Connections lose `accounting.journals.read` from 29 April 2026, a reason to prefer a standard OAuth app for a multi-entity agent.
- Attachment size limits are stated inconsistently in Xero's own docs, 10 MB vs 25 MB on the attachments/invoices pages, and 3 MB on the manual journals and accounts pages (Xero docs conflict, verify); treat the lowest figure quoted for the endpoint as the safe ceiling.

---
## 1. API Foundations

Everything in this section is sourced from the official Xero developer documentation (developer.xero.com), verified August 2026. It covers authentication, app types, scopes, multi-tenant handling, request conventions, rate limits, error handling and SDKs, the plumbing every other section depends on.

### 1.1 App types and authorization flows

Xero supports three integration models, all managed from **My Apps** (https://developer.xero.com/app/manage):

| | Code flow (standard) | PKCE flow | Custom Connection |
|---|---|---|---|
| Grant type | `authorization_code` | `authorization_code` + PKCE (S256) | `client_credentials` |
| Best for | Web server apps that can securely store a client secret | Mobile/desktop (native) apps that can't store a secret (SPAs not currently supported) | Back end, machine-to-machine integrations |
| Connection limit | 25 tenants (unlimited after app certification) | 25 tenants (unlimited after certification) | **One organisation per connection** |
| Eligible for Xero App Store | Yes | Yes | No |
| Offline access | Yes (refresh tokens via `offline_access`) | Yes | Yes (re-request tokens with client credentials; no refresh token issued) |
| Cost | Free | Free | Monthly fee on the Xero organisation: AUD $10/m inc GST, NZD $10/m ex GST, GBP £5/m ex VAT, USD $5/m ex tax |
| Regional availability | Global | Global | **UK, AU, NZ and US Xero organisations only** |

Key facts for a multi-entity accounting-automation project:

- **Standard/PKCE apps connect to multiple organisations**: a user goes through the OAuth consent flow once per organisation (or selects multiple orgs in one consent), and each connected org becomes a *tenant* addressable with the same access token plus a per-request `xero-tenant-id` header. Uncertified apps are capped at **25 tenant connections**; certification (app partner program) removes the cap.
- **Uncertified app tiers**: new apps default to the Starter tier with 5 connections; the Core tier allows up to 50 connections; App Store listing requires Plus tier or above. Each organisation/practice can connect a maximum of **two uncertified apps** (no limit on certified apps). Custom Connection apps do not count toward this two-app limit.
- **Custom Connections** are a premium option using `client_credentials`. Each Custom Connection connects to exactly **one** Xero organisation; to reach multiple organisations you either buy one Custom Connection per org (customers can now purchase as many as needed per organisation) or use the code/PKCE flow instead. They cannot integrate with Xero Practice Manager or Xero HQ. The Xero **Demo Company can be connected free** for development.
- **Custom Connection setup flow**: create the app in My Apps ("Custom connection" type), select scopes and an authorising user, that user receives an email and grants consent (their org must hold a Custom Connection subscription, purchased at https://connect.xero.com/custom or via the org's Connected Apps page), then you retrieve the client_id/client_secret.
- **Scope changes for Custom Connections (important)**: from 29 April 2026 all custom connections have granular scopes; **new custom connections no longer have access to `accounting.journals.read`**; existing connections may keep broad scopes until September 2027. If general-ledger journal access matters (it does for reconciliation/review work), factor this into the choice of app type.
- Native (desktop/mobile) apps connecting directly to the API are required to use PKCE. PKCE differs from the code flow only in that the authorize URL adds `code_challenge` (= BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))) and `code_challenge_method=S256`, the token exchange sends `code_verifier` instead of a client secret, and revocation requires only the client_id.

An **OpenID Connect Discovery** document is published at `https://identity.xero.com/.well-known/openid-configuration`.

### 1.2 OAuth 2.0 flow details

**Endpoints**

| Purpose | URL |
|---|---|
| Authorize | `https://login.xero.com/identity/connect/authorize` |
| Token | `https://identity.xero.com/connect/token` |
| Revocation | `https://identity.xero.com/connect/revocation` |
| Connections (tenants) | `https://api.xero.com/connections` |

**Authorization request** (code flow):

```
https://login.xero.com/identity/connect/authorize?response_type=code
  &client_id=YOURCLIENTID
  &redirect_uri=YOURREDIRECTURI
  &scope=openid profile email accounting.transactions offline_access
  &state=123
```

- `redirect_uri` must be https (for testing `http://localhost/` is allowed; `http://127.0.0.1` is not).
- `state` is optional but recommended against CSRF/forgery.
- The returned authorization `code` may be exchanged **once** and expires **5 minutes** after issuance.

**Token exchange** (POST to token endpoint, `Content-Type: application/x-www-form-urlencoded`):

- Header: `Authorization: Basic base64(client_id:client_secret)`
- Body: `grant_type=authorization_code&code=...&redirect_uri=...` (PKCE: add `code_verifier`, no Basic auth secret)
- Custom Connections: `grant_type=client_credentials&scope=...` (space-separated scopes)

**Token response**: `access_token`, `expires_in`, `token_type: Bearer`, plus `id_token` (if OpenID scopes requested) and `refresh_token` (if `offline_access` requested; not issued for client_credentials).

**Token lifetimes** (documented values):

| Token | Lifetime |
|---|---|
| `id_token` | 5 minutes |
| `access_token` | 30 minutes |
| `refresh_token` | 60 days |

**Refresh and rotation rules**

- Refresh with `grant_type=refresh_token&refresh_token=...` (Basic auth header with client_id:client_secret; PKCE apps use client_id only).
- The response contains a **new access token and a new refresh token; you must persist both** (rotation). Each refresh resets the 60-day clock; a refresh token unused for 60 days expires and the user must re-authorise.
- **30-minute grace period**: if your app fails to receive or save the new tokens, the *previous* refresh token remains usable for 30 minutes, after which it expires permanently.
- The access token is a JWT; the decoded payload includes `xero_userid`, `scope`, and `authentication_event_id` (used to identify which tenants were connected in the current auth flow).
- **Revocation**: POST `token=<refresh_token>` to the revocation endpoint (Basic auth) to revoke the refresh token and remove all of that user's connections to your app. Success returns 200 with empty body.

**offline_access**: request this scope to receive a refresh token and maintain a long-lived connection without user interaction. Without it you only get the 30-minute access token.

### 1.3 Tenants and the /connections endpoint

Xero is multi-tenanted. A tenant can be an ORGANISATION, a Xero HQ practice, or a Practice Manager account (tenantType distinguishes them). Core APIs (Accounting, Payroll, Files) operate in the context of an Organisation tenant.

After completing an OAuth flow, call the connections endpoint to discover authorised tenants:

```
GET https://api.xero.com/connections
Authorization: Bearer {access_token}
```

Response (array):

```json
[{
  "id": "e1eede29-f875-4a5d-8470-17f6a29a88b1",
  "authEventId": "d99ecdfe-391d-43d2-b834-17636ba90e8d",
  "tenantId": "70784a63-d24b-46a9-a4db-0e70a274b056",
  "tenantType": "ORGANISATION",
  "tenantName": "Maple Florist",
  "createdDateUtc": "2019-07-09T23:40:30.1833130",
  "updatedDateUtc": "2020-05-15T01:35:13.8491980"
}]
```

- All previously connected tenants are accessible with the most recent access token, which is what makes one app serve many group entities.
- Filter to just-connected tenants with `?authEventId=<authentication_event_id from the decoded access token>`.
- `createdDateUtc` != `updatedDateUtc` means the tenant was disconnected and reconnected at some point.
- Remove a connection with `DELETE https://api.xero.com/connections/{connectionId}` (note: connection `id`, not `tenantId`). Connections can also be viewed/disconnected in the developer portal's Connections management page. If you no longer hold a user token, connections can be managed via the client_credentials grant with the non-tenanted `app.connections` scope.

**Every Accounting API call must carry the tenant header**:

```
GET https://api.xero.com/api.xro/2.0/Invoices
Authorization: Bearer {access_token}
Accept: application/json
xero-tenant-id: {tenantId}
```

(Custom Connections are bound to a single org, so they authenticate with just the Bearer token; the tenant is implicit.)

### 1.4 Scopes

Scopes are **space-separated** in the authorize request and **additive**: re-sending a user through the flow with new scopes adds them to previously consented scopes. Scopes cannot be removed from an existing consent; the only way to reduce them is to revoke the token and start over. Request the minimum needed.

**Identity / session scopes**

| Scope | Grants |
|---|---|
| `openid` | Use of the user's identity (required for Sign In with Xero) |
| `profile` | First name, last name, full name, Xero user id |
| `email` | Email address |
| `offline_access` | Issues a refresh token (long-lived offline connection) |

**Accounting API scopes**

Note on the granular-scopes migration: broad scopes are being replaced by granular scopes. Since March 2026 all new and existing Web/PKCE apps have been assigned granular scopes; since 29 April 2026 all custom connections have granular scopes; apps currently on broad scopes can keep using them **until September 2027**.

| Scope | Status | Unlocks (endpoints) |
|---|---|---|
| `accounting.transactions` | Deprecated (broad) | View and manage business transactions: BankTransactions, BankTransfers, BatchPayments, CreditNotes, ExpenseClaims, Invoices, LinkedTransactions, ManualJournals, Overpayments, Quotes, Payments, Prepayments, PurchaseOrders, Receipts, RepeatingInvoices |
| `accounting.transactions.read` | Deprecated (broad) | As above, GET only |
| `accounting.invoices` / `.read` | New (granular) | CreditNotes, Invoices, LinkedTransactions, Quotes, PurchaseOrders, RepeatingInvoices, Items |
| `accounting.payments` / `.read` | New (granular) | BatchPayments, Overpayments, Payments, Prepayments |
| `accounting.banktransactions` / `.read` | New (granular) | BankTransactions, BankTransfers |
| `accounting.manualjournals` / `.read` | New (granular) | ManualJournals |
| `accounting.journals.read` | | **View the general ledger (Journals endpoint)**. Not available to new Custom Connections created from 29 April 2026 |
| `accounting.reports.read` | Deprecated (broad) | AgedPayablesByContact, AgedReceivablesByContact, BalanceSheet, BankSummary, BASReport, BudgetSummary, ExecutiveSummary, GSTReport, ProfitAndLoss, TrialBalance |
| `accounting.reports.aged.read` | New | AgedPayablesByContact, AgedReceivablesByContact |
| `accounting.reports.balancesheet.read` | New | BalanceSheet |
| `accounting.reports.banksummary.read` | New | BankSummary |
| `accounting.reports.budgetsummary.read` | New | BudgetSummary |
| `accounting.reports.executivesummary.read` | New | ExecutiveSummary |
| `accounting.reports.profitandloss.read` | New | ProfitAndLoss |
| `accounting.reports.trialbalance.read` | New | TrialBalance |
| `accounting.reports.taxreports.read` | New | GSTReport, BASReport |
| `accounting.reports.tenninetynine.read` | | 1099 reports (US) |
| `accounting.settings` / `.read` | | Accounts (chart of accounts), BrandingThemes, Currencies, Items, InvoiceReminders, Organisation, TaxRates, TrackingCategories, Users (`.read` = GET only) |
| `accounting.contacts` / `.read` | | Contacts, ContactGroups (`.read` = GET only) |
| `accounting.attachments` / `.read` | | Attachments on Accounts, BankTransactions, BankTransfers, Contacts, CreditNotes, Invoices, LinkedTransactions, ManualJournals, PurchaseOrders, Receipts, RepeatingInvoices |
| `accounting.budgets.read` | | Budgets endpoint |

Broad-to-granular mapping: `accounting.transactions` maps to `accounting.invoices` + `accounting.payments` + `accounting.banktransactions` + `accounting.manualjournals` (same pattern for `.read`); `accounting.reports.read` maps to the eight granular `accounting.reports.*.read` scopes.

**Related scopes worth knowing for reconciliation work** (Finance API, separate from the Accounting API): `finance.cashvalidation.read` (bank statement and reconciliation data), `finance.statements.read` (financial statements), `finance.bankstatementsplus.read` (bank statements and reconciled transactions), `finance.accountingactivity.read` (usage activity).

**Non-tenanted scopes** (client_credentials grant only): `app.connections` (view/manage your app's connection data), `marketplace.billing` (Xero App Store endpoints).

### 1.5 API conventions

**Base URL**: `https://api.xero.com/api.xro/2.0/` (Accounting API). Resource style: `GET .../Contacts/{ContactID}` for one resource, `GET .../TaxRates` for a full list.

**Required headers**

| Header | Value |
|---|---|
| `Authorization` | `Bearer {access_token}` |
| `xero-tenant-id` | tenant GUID from /connections (not needed for Custom Connections) |
| `Accept` | `application/json` for JSON. **Default responses are XML if this header is omitted.** Individual Invoices, Quotes and Credit Notes can be returned as PDF with `Accept: application/pdf` |
| `Content-Type` | For POST/PUT: `application/json`, `application/xml`, or `application/x-www-form-urlencoded`, UTF-8 encoded |
| `Idempotency-Key` | Optional, POST/PUT/PATCH only (see below) |
| `If-Modified-Since` | Optional UTC timestamp filter (see below) |

**JSON dates**: JSON responses use the legacy .NET format, e.g. `"DateTimeUTC": "\/Date(1439434356790)\/"` (unix epoch in milliseconds, sometimes with a `+0000` offset). Some elements also include helper strings (DateString, DueDateString).

**Pagination**

- Append `?page=1`; page size via `pageSize`. **Default 100 items per page, minimum 1, maximum 1000**; out-of-range values are adjusted to the nearest supported size.
- Paged endpoints: Invoices, Contacts, CreditNotes, BankTransactions, ManualJournals, Payments, PurchaseOrders, Prepayments, Overpayments. Paged results include full detail (e.g. line items), often avoiding per-resource fetches. The Journals endpoint returns batches of 100 via its own offset mechanism.
- Responses include a `pagination` object: `{"page": 1, "pageSize": 10, "pageCount": 1, "itemCount": 1}`. Use it to iterate pages (supersedes the old "fetch until empty" approach).

**Filtering**

- **Optimised query parameters** (preferred, faster): e.g. `Invoices?IDs=...`, `Invoices?ContactIDs=...`, `Invoices?Statuses=AUTHORISED,PAID`, `Invoices?InvoiceNumbers=...`, `Contacts?IDs=...`, `Contacts?SearchTerm=Alice`. These accept comma-separated lists and make the response paged.
- **`where` parameter**: filters on most elements, must be percent-encoded, e.g. `Accounts?where=Type%3D%3D%22BANK%22` (`Type=="BANK"`), `AmountDue > 5000 and DueDate > DateTime(2015, 01, 01)`. Keep where clauses to simple `==` conditions; complex clauses can time out on large orgs.
- **Optimisation rules** (enforced by high-volume threshold limits since 4 September 2024): GET requests that would require processing more than 100k documents are rejected with HTTP 400, `"Type": "HighVolumeException"`, `ErrorNumber: 51` on Invoices, Payments, BankTransactions, ManualJournals, CreditNotes, Contacts, PurchaseOrders (Journals has a limit of 100 per fetch). To stay optimised: use `If-Modified-Since` instead of `UpdatedDateUTC` in where clauses; avoid `or` and `!=` (use list parameters like `Statuses=` instead); range operators (`>`, `>=`, `<`, `<=`) are only optimised on certain fields per endpoint (e.g. Date, DueDate, AmountDue on Invoices; Date on BankTransactions); avoid LINQ-like functions (`.ToLower()`, `.Contains()`, `.StartsWith()`), use `SearchTerm` instead.
- **`If-Modified-Since`** (HTTP header): returns only records created/modified since the given UTC timestamp (accurate to the second). Recommended for all large result sets and for incremental sync. Caveat: some changes don't touch UpdatedDateUTC (e.g. DueDate/SentToContact changes on partially paid transactions, and Contact fields like Balances, IsSupplier, IsCustomer), so such records may be missed by If-Modified-Since queries.
- **`summaryOnly=true`**: on GET Contacts and Invoices, returns a lightweight response excluding computation-heavy fields.

**Ordering**: `?order=EmailAddress` or `?order=EmailAddress%20DESC`. Secondary ordering by ID is applied by default on Contacts, Payments, Batch Payments, Credit Notes, Invoices and Bank Transactions; the default order is `UpdatedDateUTC ASC, [ID] ASC`, which keeps paging consistent.

**Writes (POST/PUT)**

- PUT creates new data; POST creates or updates. Multiple entities of the same type can be sent in one call (recommended practical batch ~50 elements; request body must stay under the size limits: FAQ guidance cites a 3.5MB practical request ceiling, and the stated maximum request size for all APIs is 10MB).
- **`summarizeErrors=false`** (Accounting API only): when posting multiple entities, ensures every entity comes back with its own status attribute (`StatusAttributeString`: `OK`, `WARNING`, or `ERROR`) and you get HTTP 200 even when some elements failed; per-element failures carry `ValidationErrors`. Without it, any validation error fails the whole request with HTTP 400.
- **`unitdp=4`**: opt in to four-decimal-place unit amounts (UnitAmount is rounded to 2dp by default) by adding this query parameter (see the "Rounding in Xero" guide).
- Always check the response: HTTP 200 plus a status of OK plus a returned identifier (e.g. InvoiceID). Log and surface validation WARNINGs, do not suppress them.

**Idempotency (`Idempotency-Key` header)**

- Supported on requests that mutate data (POST, PUT, PATCH); ignored on other methods. Header name is case-insensitive; max key length **128 characters**; any string is accepted (uniqueness is your responsibility; Xero recommends concatenating four UUIDs).
- Xero caches the response per key for **6 minutes** from first call; retries with the same key return the cached response instead of re-processing. Reusing a key with a *different* request (any change to URL, body or method) returns HTTP 400: `Idempotency Key: KEY_VALUE is used with a different request.` Re-use is scoped per app.
- Errors are cached too: if an idempotent request fails internally, replaying the same key returns the cached error; after repeated errors, GET the resource to check whether it exists, then retry with a new key if not.
- Idempotency is checked *after* rate limits, so duplicate requests still count against your limits.

### 1.6 Rate limits

Per **tenant** (organisation/account/practice):

| Limit | Value |
|---|---|
| Concurrent | 5 calls in progress at one time |
| Per minute | 60 calls |
| Per day | 5,000 calls (Core tier and above); 1,000 calls for Starter-tier apps |

Per **app** (across all tenants):

| Limit | Value |
|---|---|
| App minute limit | 10,000 calls per minute |

- Limits are per tenant, so two connected organisations each get their own 60/min and 5,000/day allowance.
- Every response includes `X-DayLimit-Remaining`, `X-MinLimit-Remaining` and `X-AppMinLimit-Remaining` headers.
- Exceeding a limit returns **HTTP 429** with an `X-Rate-Limit-Problem` header naming the limit hit. Minute/daily breaches also include a **`Retry-After`** header (seconds to wait). Limits use a fixed window that resets at different times per tenant, so always honour Retry-After rather than guessing.
- Best practices from the docs: pause requests to that tenant until Retry-After elapses; batch multiple entities per write call; use pagination + If-Modified-Since for bulk extraction; queue/schedule large extractions rather than serving them synchronously to users. (The Journals endpoint is noted as a premium feature for apps in the Advanced tier and above.)
- Request size: maximum 10MB for all APIs; batches of up to ~50 elements recommended for timely responses.

### 1.7 Error handling

| HTTP code | Meaning | Notes |
|---|---|---|
| 200 | OK | Also returned with per-element errors when `summarizeErrors=false` is used |
| 400 | Bad Request | Validation exception; body contains an ApiException summary. Also used for idempotency-key conflicts and HighVolumeException (ErrorNumber 51) |
| 401 | Unauthorized | Invalid/expired credentials. Users can disconnect your app inside Xero at any time, so handle 401 by prompting re-authorisation |
| 403 | Not Permitted | User lacks permission to the resource |
| 404 | Not Found | Resource doesn't exist |
| 412 | Precondition Failed | Invalid request-header conditions; also returned when using TLS 1.0 |
| 429 | Rate Limit Exceeded | See rate limits; honour Retry-After |
| 500 | Internal Error | Unhandled Xero error; contact Xero API team if persistent |
| 501 | Not Implemented | Method not implemented (e.g. POST Organisation) |
| 503 | Not Available | Scheduled outage/maintenance ("The Xero API is currently offline for maintenance"), retry soon |
| 503 | Organisation offline | "The Organisation is offline": a single org is temporarily unreachable, typically for several minutes; retry interval of ~5 minutes recommended |

**Validation error shape** (HTTP 400):

```json
{
  "ErrorNumber": 10,
  "Type": "ValidationException",
  "Message": "A validation exception occurred",
  "Elements": [{
    "ValidationErrors": [{ "Message": "Email address must be valid" }]
  }]
}
```

**Bulk responses** (`summarizeErrors=false`): each element carries `StatusAttributeString` of `OK`, `WARNING` (processed, with `Warnings: [{Message}]`) or `ERROR` (not saved, with `ValidationErrors: [{Message}]` / `Description`).

### 1.8 Official SDKs and OpenAPI specs

All SDKs are generated from Xero's OpenAPI definitions: **https://github.com/XeroAPI/Xero-OpenAPI**.

| Language | GitHub | SDK docs |
|---|---|---|
| C# (.NET Standard) | https://github.com/XeroAPI/Xero-NetStandard | xeroapi.github.io/Xero-NetStandard/accounting |
| Java | https://github.com/XeroAPI/Xero-Java | xeroapi.github.io/Xero-Java/v4/accounting |
| Node.js | https://github.com/XeroAPI/xero-node | xeroapi.github.io/xero-node/accounting |
| PHP | https://github.com/XeroAPI/xero-php-oauth2 | xeroapi.github.io/xero-php-oauth2/docs/v2/accounting |
| Ruby | https://github.com/XeroAPI/xero-ruby | xeroapi.github.io/xero-ruby/accounting |
| Python | https://github.com/XeroAPI/xero-python | xeroapi.github.io/xero-python/v1/accounting |

API status page: https://status.developer.xero.com/.

### 1.9 Practical implications for a multi-entity automation agent

- Use a **standard (code flow) app** with `offline_access` for group-wide access: one consent flow per entity, one token store, per-call `xero-tenant-id` switching, up to 25 orgs uncertified. Custom Connections only fit if every entity is in UK/AU/NZ/US, each pays the monthly fee, and (for new connections) you can live without `accounting.journals.read`.
- Recommended scope set for reconciliation/journals/posting/review (granular era): `offline_access accounting.journals.read accounting.settings accounting.contacts accounting.banktransactions accounting.payments accounting.invoices accounting.manualjournals accounting.attachments accounting.reports.aged.read accounting.reports.balancesheet.read accounting.reports.banksummary.read accounting.reports.profitandloss.read accounting.reports.trialbalance.read accounting.reports.executivesummary.read` (plus `openid profile email` if humans sign in).
- Build refresh-token rotation with durable storage and the 30-minute grace retry; treat 401 as "reconnect needed", 429 as "sleep Retry-After for that tenant", and 503 organisation-offline as "retry in ~5 minutes".
- Use `If-Modified-Since` + paging (pageSize up to 1000) + optimised filters for incremental sync per entity, and `Idempotency-Key` on every posting/correction write.

---
## 2. Banking & Reconciliation

Everything in this section (except the Bank Feeds API and Finance API) lives on the Accounting API:

- **Base URL:** `https://api.xero.com/api.xro/2.0/`
- **Required header:** `xero-tenant-id: <tenant guid>` on every call
- **Scopes:** `accounting.transactions` (write) / `accounting.transactions.read` (read) for bank transactions, payments, batch payments, bank transfers, prepayments and overpayments; `accounting.reports.read` for the reports in 2.8. These are the broad scopes, usable until September 2027; see 1.4 for the granular equivalents (`accounting.banktransactions`, `accounting.payments`, `accounting.reports.*.read`)
- **Idempotency:** all create/update operations accept an `Idempotency-Key` header (max 128 chars) so retries do not create duplicates. Use this for every write in an automation pipeline.
- **Writes convention:** across the Accounting API, `PUT` = create, `POST` = update (or update-or-create on some collections). Several "delete" operations are modelled as a `POST` that sets `Status: "DELETED"`.

Doc pages: `https://developer.xero.com/documentation/api/accounting/{banktransactions|payments|batchpayments|banktransfers|prepayments|overpayments|bankstatements|reports}` and `https://developer.xero.com/documentation/bank-feeds-api/overview`.

---

### 2.1 Bank Transactions, `/BankTransactions`

Spend money / receive money transactions on a bank account. This is the primary object your automation creates to represent a coded bank line on the Xero side.

**Operations**

| Verb & path | Purpose |
|---|---|
| `GET /BankTransactions` | Retrieve spend/receive money transactions. Filters: `where`, `order`, `page` (default 100 per page **with line item detail**), `pageSize` (max 1000, see 1.5), `unitdp=4`, `If-Modified-Since` header, `References` (comma-separated list filter) |
| `PUT /BankTransactions` | Create one or more transactions |
| `POST /BankTransactions` | Update **or create** one or more transactions ("UpdateOrCreate") |
| `GET /BankTransactions/{BankTransactionID}` | Retrieve one transaction |
| `POST /BankTransactions/{BankTransactionID}` | Update one transaction |
| `GET/PUT/POST /BankTransactions/{id}/Attachments/{FileName}` | Full attachments support (list, fetch by AttachmentID or filename, create with PUT, replace with POST) |
| `GET/PUT /BankTransactions/{id}/History` | Read history / add a history note |

**Type values** (enum in the official OpenAPI spec):

| Type | Meaning | Creatable via this endpoint? |
|---|---|---|
| `SPEND` | Spend money | Yes |
| `RECEIVE` | Receive money | Yes |
| `SPEND-OVERPAYMENT` | Overpayment to a supplier | Yes, this is how AP overpayments are created via API |
| `RECEIVE-OVERPAYMENT` | Overpayment from a customer | Yes, this is how AR overpayments are created via API |
| `SPEND-PREPAYMENT` | Prepayment to a supplier | Yes (verify, creation of prepayment types documented on the endpoint page; overpayment creation via this endpoint is well confirmed) |
| `RECEIVE-PREPAYMENT` | Prepayment from a customer | Yes (verify, as above) |
| `SPEND-TRANSFER` / `RECEIVE-TRANSFER` | Two sides of a bank transfer | No, returned read-only; create via `/BankTransfers` (2.4) |

When a transaction is an overpayment/prepayment type, the response carries a read-only `PrepaymentID` or `OverpaymentID` linking to the credit document (see 2.5).

**Fields**

| Field | Notes |
|---|---|
| `Type` | **Required.** See table above |
| `BankAccount` | **Required.** Account object, supply `AccountID` (or `Code`). Must be a bank-type account |
| `LineItems` | **Required.** Array of LineItem: `Description` (min 1 char), `Quantity`, `UnitAmount` (or just `LineAmount` and Xero computes the rest), `AccountCode`/`AccountID`, `TaxType`, `TaxAmount` (override allowed), `ItemCode`, `Tracking` (max 2 categories per line) |
| `Contact` | Contact object (`ContactID` or `Name`). Required for SPEND/RECEIVE per the endpoint doc page (verify, the OpenAPI `required` list is only Type/LineItems/BankAccount) |
| `Date` | `YYYY-MM-DD`; transaction date |
| `IsReconciled` | Boolean, writable. **See semantics below** |
| `Reference` | Only supported for SPEND and RECEIVE types |
| `CurrencyCode`, `CurrencyRate` | For non-base-currency bank accounts; if omitted, user-defined rate or XE.com day rate is used. Per spec: "Setting currency is only supported on overpayments" |
| `LineAmountTypes` | `Exclusive` / `Inclusive` / `NoTax` |
| `Url` | Deep link back to the source document in your app ("Go to App Name" in Xero UI), very useful for audit trails |
| `Status` | `AUTHORISED`, `DELETED`, `VOIDED`. There is no draft state. Delete by POSTing `Status: "DELETED"` (verify exact void vs delete rules per type) |
| `SubTotal`, `TotalTax`, `Total` | Read-back monetary totals |
| `BankTransactionID`, `PrepaymentID`, `OverpaymentID`, `UpdatedDateUTC`, `HasAttachments` | Read-only |

**`IsReconciled` semantics, read this carefully**

- On **GET**, `IsReconciled=true` means the transaction is reconciled (either genuinely matched to an imported statement line, or force-marked).
- On **PUT/POST**, you may set `IsReconciled: true`. This flags the transaction as reconciled, but it **does not match the transaction against any bank statement line**, there is no statement line involved at all. Xero has publicly confirmed (official product response on the Xero developer ideas forum, May 2026) that it will not add statement-line matching or expose unreconciled statement lines via the public API, for regulatory/data-sharing reasons (unreconciled lines are treated as raw banking data).
- Practical consequence: if a bank feed later delivers the real statement line for money you already force-marked reconciled, that statement line arrives with nothing to match and must be handled in the UI (or the transaction unmarked). In the Xero **UI**, "Mark as reconciled" creates a synthetic statement line to balance the account; whether the API flag does the same is not documented, treat statement-balance reports with care after using it (verify).
- Recommended use: only set `IsReconciled=true` for orgs that genuinely have **no feed and no statement import** for that account, i.e. Xero is the system of record for the bank account.

**Example, create a coded SPEND transaction (`PUT /BankTransactions`)**

```json
{
  "BankTransactions": [
    {
      "Type": "SPEND",
      "Date": "2026-08-14",
      "Reference": "POS-88231",
      "Url": "https://app.example.com/txn/88231",
      "Contact": { "ContactID": "6d42f03b-181f-43e3-93fb-2025c012de92" },
      "BankAccount": { "AccountID": "297c2dc5-cc47-4afd-8ec8-74990b8761e9" },
      "LineAmountTypes": "Inclusive",
      "IsReconciled": false,
      "LineItems": [
        {
          "Description": "Office supplies - Northwind Office Supplies",
          "Quantity": 1.0,
          "UnitAmount": 113.85,
          "AccountCode": "453",
          "TaxType": "INPUT"
        }
      ]
    }
  ]
}
```

Response returns the array with `BankTransactionID`, computed `SubTotal`/`TotalTax`/`Total` and `Status: "AUTHORISED"`. Use `?summarizeErrors=false` on batch PUTs to get per-element validation errors mixed with successes.

---

### 2.2 Payments, `/Payments`

Applies money against an **approved (AUTHORISED) invoice or credit note**, and records refunds of credit notes, prepayments and overpayments. A payment against an ACCREC/ACCPAY invoice is what makes an invoice show as paid and creates the bank-account-side transaction that reconciliation will match.

**Operations**

| Verb & path | Purpose |
|---|---|
| `GET /Payments` | Retrieve payments. Supports `where`, `order`, `page`, `pageSize`, `If-Modified-Since` |
| `PUT /Payments` | Create multiple payments |
| `POST /Payments` | Create a single payment |
| `GET /Payments/{PaymentID}` | Retrieve one payment |
| `POST /Payments/{PaymentID}` | **Delete (reverse) the payment**, body sets `Status: "DELETED"` |
| `GET/PUT /Payments/{PaymentID}/History` | History records |

**Fields**

| Field | Notes |
|---|---|
| `Invoice` | Target document: supply `{"InvoiceID": …}` (or `InvoiceNumber` at top level) |
| `CreditNote` / `Prepayment` / `Overpayment` | Alternative targets, used for **refund** payments against those credit documents |
| `Account` / `Code` | The bank (or applicable) account the payment is made from/to: `{"AccountID": …}` or account `Code`. Note: not all accounts have a code |
| `Date` | Payment date `YYYY-MM-DD` |
| `Amount` | Must be ≤ the outstanding amount on the invoice |
| `BankAmount` | Amount in the bank account's currency (multicurrency) |
| `CurrencyRate` | For non-base-currency invoices/credit notes |
| `Reference` | Optional description, e.g. "Direct Debit" |
| `IsReconciled` | Per the official spec: "A boolean indicating whether you would like the payment to be **created as reconciled** when using PUT, or whether a payment has been reconciled when using GET." Same caveats as 2.1, no statement line is matched |
| `Status` | `AUTHORISED` or `DELETED` |
| `PaymentType` | Read-only: `ACCRECPAYMENT`, `ACCPAYPAYMENT`, `ARCREDITPAYMENT`, `APCREDITPAYMENT`, `AROVERPAYMENTPAYMENT`, `ARPREPAYMENTPAYMENT`, `APPREPAYMENTPAYMENT`, `APOVERPAYMENTPAYMENT`, i.e. the endpoint also carries credit-note/prepayment/overpayment **refunds** |
| `PaymentID`, `BatchPaymentID`, `UpdatedDateUTC` | Read-only identifiers; `BatchPaymentID` present if created inside a batch |
| `BankAccountNumber`, `Particulars`, `Details` | Supplier bank details / remittance info (used with batch payments) |

**Example, apply a payment to an invoice (`PUT /Payments`)**

```json
{
  "Payments": [
    {
      "Invoice": { "InvoiceID": "cd7f9c9e-5e3a-4b64-9d3f-2f3c1c7e2ab1" },
      "Account": { "Code": "090" },
      "Date": "2026-08-14",
      "Amount": 460.00,
      "Reference": "INV-4003 EFT",
      "IsReconciled": false
    }
  ]
}
```

**Deleting / reversing a payment**, payments are not editable; to reverse, `POST /Payments/{PaymentID}` with:

```json
{ "Status": "DELETED" }
```

(The spec models this as the `PaymentDelete` object whose only required field is `Status`, default `DELETED`.) The payment then returns with `Status: "DELETED"` on GET; the invoice's outstanding amount is restored.

**Payment on account:** the Payments endpoint always needs a target document (invoice, credit note, prepayment or overpayment). A customer receipt with no invoice ("payment on account") is represented in Xero as an **overpayment**, create it as a `RECEIVE-OVERPAYMENT` bank transaction (2.1), then allocate it to invoices later (2.5). (Verify exact wording; behaviour is confirmed by the type enums and the allocation endpoints.)

---

### 2.3 Batch Payments, `/BatchPayments`

One bank-side amount covering payments of many invoices (or one payment split from many). Critical for reconciliation because the bank statement shows a **single line for the whole batch**, and Xero's Find & Match screen shows the batch as one amount.

**Operations**

| Verb & path | Purpose |
|---|---|
| `GET /BatchPayments` / `GET /BatchPayments/{BatchPaymentID}` | Retrieve batches |
| `PUT /BatchPayments` | Create one or many batch payments |
| `POST /BatchPayments` | Delete: body `{"BatchPaymentID": …, "Status": "DELETED"}` (spec `BatchPaymentDelete`, operation `deleteBatchPayment`) |
| `POST /BatchPayments/{BatchPaymentID}` | Delete by URL param: body `{"Status": "DELETED"}` |
| `GET/PUT /BatchPayments/{BatchPaymentID}/History` | History |

Note: the older field-level doc text says "It is not possible to delete batch payments via the API", but the current OpenAPI spec defines the delete operations above, the delete support is the newer state (verify against your org before relying on it).

**Fields**

| Field | Notes |
|---|---|
| `Account` | Bank account making/receiving the batch (`AccountID`) |
| `Date` | Payment date |
| `Payments` | Array of Payment objects, each with `Invoice.InvoiceID`, `Amount`, and optionally supplier bank details (`BankAccountNumber`, `Particulars`, `Details`, `Code`, `Reference` per payment) |
| `Reference`, `Particulars` (max 12), `Code` (max 12) | NZ only; shown in the Find & Match screen and possibly on the imported statement |
| `Details` | Non-NZ; max 18 chars; sent to the org's bank as batch reference, shown in Find & Match |
| `Narrative` | UK only; max 18; shows on the statement line in Xero |
| `Type` | Read-only: `PAYBATCH` (bills) or `RECBATCH` (sales invoices) |
| `Status` | Read-only: `AUTHORISED` / `DELETED` |
| `TotalAmount`, `BatchPaymentID`, `UpdatedDateUTC` | Read-only |
| `IsReconciled` | **Read-only** here, tells you whether the batch has been reconciled; you cannot force-mark a batch reconciled via this endpoint |

---

### 2.4 Bank Transfers, `/BankTransfers`

Moves money between two bank accounts in the org; Xero creates a `SPEND-TRANSFER` bank transaction in the source account and a `RECEIVE-TRANSFER` in the destination account.

**Operations**

| Verb & path | Purpose |
|---|---|
| `GET /BankTransfers` / `GET /BankTransfers/{BankTransferID}` | Retrieve (supports `where`, `order`, `If-Modified-Since`) |
| `PUT /BankTransfers` | Create a transfer |
| `POST /BankTransfers` | Delete one or more transfers |
| `POST /BankTransfers/{BankTransferID}` | Delete a specific transfer |
| Attachments (`GET/PUT/POST …/Attachments/{FileName}`) and `History` | Supported |

There is no update operation, transfers are create/delete only.

**Fields**

| Field | Notes |
|---|---|
| `FromBankAccount` | **Required.** `{"AccountID": …}` (or Code) |
| `ToBankAccount` | **Required.** |
| `Amount` | **Required.** |
| `Date` | `YYYY-MM-DD` |
| `Reference` | Reference for both generated transactions |
| `FromIsReconciled` / `ToIsReconciled` | Writable booleans, reconciled flag for each side (default false). Same non-matching caveat as `IsReconciled` in 2.1 |
| `FromBankTransactionID` / `ToBankTransactionID` | Read-only, the two generated bank transactions |
| `CurrencyRate`, `BankTransferID`, `CreatedDateUTC`, `HasAttachments` | Read-only |
| `Status` | Read-only: `AUTHORISED` (on create) or `DELETED` |
| `FromTracking` / `ToTracking` | Optional tracking categories (max 2 per account side) |

---

### 2.5 Prepayments & Overpayments, `/Prepayments`, `/Overpayments`

Both are **read + allocate only** on their own endpoints. You cannot PUT/POST the documents themselves here; they are created:

- via the **BankTransactions** endpoint using types `RECEIVE-OVERPAYMENT` / `SPEND-OVERPAYMENT` (and prepayment types, see 2.1), or
- by users during bank reconciliation in the UI.

Creation and allocation cannot be combined in a single call, always two steps (create the bank transaction, then allocate).

**Operations (identical shape for both)**

| Verb & path | Purpose |
|---|---|
| `GET /Prepayments`, `GET /Prepayments/{PrepaymentID}` | Retrieve (supports `where`, `order`, `page`, `unitdp`, `If-Modified-Since`) |
| `PUT /Prepayments/{PrepaymentID}/Allocations` | Allocate an amount of the prepayment to an invoice |
| `DELETE /Prepayments/{PrepaymentID}/Allocations/{AllocationID}` | Remove an allocation |
| `GET/PUT /Prepayments/{PrepaymentID}/History` | History |
| Same paths under `/Overpayments/{OverpaymentID}` | As above |

**Key fields**

| Field | Notes |
|---|---|
| `Type` | Prepayments: `RECEIVE-PREPAYMENT`, `SPEND-PREPAYMENT`, `ARPREPAYMENT`, `APPREPAYMENT`. Overpayments: `RECEIVE-OVERPAYMENT`, `SPEND-OVERPAYMENT`, `AROVERPAYMENT` |
| `Status` | `AUTHORISED` (credit remaining), `PAID` (fully allocated/refunded), `VOIDED` |
| `RemainingCredit` | Unallocated balance, drive your allocation logic off this |
| `Allocations` | Array of applied allocations |
| `Payments` | Refund payments made against the document (created via the Payments endpoint, 2.2) |
| `Contact`, `Date`, `LineItems`, `LineAmountTypes`, `SubTotal`, `TotalTax`, `Total`, `CurrencyCode`, `CurrencyRate`, `HasAttachments` | As usual. Overpayment line items are auto-coded to the AR/AP control account (verify, third-party sourced) |

**Allocation object** (`PUT …/Allocations`), required: `Invoice` (`InvoiceID`), `Amount`, `Date`:

```json
{ "Invoice": { "InvoiceID": "cd7f9c9e-…" }, "Amount": 150.00, "Date": "2026-08-14" }
```

---

### 2.6 Bank Feeds API, `bankfeeds.xro/1.0` (restricted)

- **Base URL:** `https://api.xero.com/bankfeeds.xro/1.0`
- **Scope:** `bankfeeds`; `xero-tenant-id` header required; `Idempotency-Key` supported on writes.
- Docs: `https://developer.xero.com/documentation/bank-feeds-api/overview`, `/feed-connections`, `/statements`.

**Who can use it, be realistic.** From the official spec: *"The Bank Feeds API is a closed API that is only available to financial institutions that have an established financial services partnership with Xero. If you're an existing financial services partner that wants access, contact your local Partner Manager. If you're a financial institution who wants to provide bank feeds to your business customers, contact us to become a financial services partner."* A partnership agreement plus a certification process is required before an app is enabled for these endpoints; calls from a non-enabled app fail with `403 invalid-application` ("The application has not been configured to use these API endpoints"). In July 2026 Xero announced an expansion of the Bank Feeds API for **non-bank statement data** providers, starting with a small set of vetted partners and a lighter certification process (Xero devblog; details not independently verified, verify current onboarding terms with Xero). **An ordinary accounting-automation app cannot simply request this API**, for most orgs the practical routes remain a bank/aggregator feed set up by the user, or manual CSV/OFX import in the UI.

**FeedConnections**, registers a feed against a Xero bank account.

| Verb & path | Purpose |
|---|---|
| `GET /FeedConnections` | List (paged; `page`, `pageSize`, default 10/page) |
| `POST /FeedConnections` | Create one or more connections (async, returns `202` with per-item `status: PENDING` or `REJECTED` + error) |
| `GET /FeedConnections/{id}` | Retrieve one |
| `POST /FeedConnections/DeleteRequests` | Delete connections (async, `202`) |

Fields: `accountToken` (FI-generated identifier, must be unique per financial institution, max 50), `accountNumber` (string(40) for BANK, last 4 digits only for CREDITCARD; required if `accountId` absent, used to match an existing Xero bank account, **a new Xero bank account is created if no match**), `accountName` (max 30, used when creating a new account), `accountId` (Xero bank account GUID, alternative to accountNumber), `accountType` (`BANK` | `CREDITCARD`), `currency`, `country` (only for multi-region apps). Status: `PENDING` / `REJECTED`. Notable errors: `feed-connected-in-different-organisation`, `feed-already-connected-in-current-organisation`.

**Statements**, delivers statement lines into a connected account. This is the **only API that creates bank statement lines** in Xero.

| Verb & path | Purpose |
|---|---|
| `POST /Statements` | Create one or more statements (`202`, items come back `PENDING`; delivery is asynchronous) |
| `GET /Statements` (paged) / `GET /Statements/{statementId}` | Check delivery status: `PENDING` → `DELIVERED` or `REJECTED` (with `errors`) |

Statement fields: `feedConnectionId`, `startDate` (**no older than one year**), `endDate`, `startBalance` / `endBalance` (`amount` + `creditDebitIndicator`, from the customer's perspective), `statementLines[]`. Statement line fields: `postedDate`, `description` (max 2000), `amount`, `creditDebitIndicator`, `transactionId` (FI's internal id, **factored into duplicate detection**), `payeeName` (255), `reference` (255), `chequeNumber` (20), `transactionType` (30).

Validation/errors worth coding for: `409 duplicate-statement`, `422 invalid-end-balance` ("End balance does not match start balance +/- statement line amounts"), `413` request > 3,000,000 bytes, `403 invalid-application` / `invalid-feed-connection`, `500 internal-error` (retryable).

---

### 2.7 What "reconciliation" actually means via the API

Xero reconciliation is the pairing of two independent record sets per bank account:

1. **Statement lines** (the bank side), imported only via a bank feed (Yodlee/direct/Bank Feeds API partners) or manual CSV/OFX/manual entry in the UI.
2. **Account transactions** (the Xero side), BankTransactions, Payments, BatchPayments, BankTransfers, expense claim payments.

A statement line is "reconciled" when a user matches it to (or creates from it) an account transaction in the Reconcile screen. The account transaction then reports `IsReconciled=true`.

**What the public Accounting API can do:**

- Create/update/delete the entire Xero side: coded bank transactions, payments, batch payments, transfers, allocations.
- Read `IsReconciled` on those objects (and filter on it, e.g. `GET /BankTransactions?where=IsReconciled==false`).
- Set `IsReconciled=true` when creating/updating bank transactions and payments (flag only).

**What it cannot do, confirmed Xero policy, not just a gap:**

- No endpoint to create statement lines (except the restricted Bank Feeds API in 2.6).
- No endpoint to read **unreconciled** statement lines.
- No endpoint to match/"reconcile" a statement line to a transaction, and no bank-rules or suggested-match API. Xero's official position (developer ideas forum, admin response, May 2026): reconciliation will not be added to the public API because unreconciled statement lines are raw banking data subject to banking/consumer-data regulation; Xero invests in in-product automation (bank rules, suggested matches, cash coding) instead.

**Automation patterns (general Xero guidance; this agent applies only the bank-transaction parts and never creates Payments, BatchPayments or allocations):**

| Org situation | Pattern |
|---|---|
| Feed or statement import available | Create fully coded `BankTransactions` / `Payments` / `BatchPayments` **before or as** the statement lines arrive, with exact amount, date and contact. Xero's matching engine then shows them as green suggested matches and the user just clicks **OK** in the Reconcile screen. Never set `IsReconciled` yourself, let the match set it. Use `Idempotency-Key` + your own `Reference`/`Url` fields to prevent and detect duplicates |
| Feed blocked, user willing to import CSV/OFX manually | Same as above; the manual import produces statement lines and the pre-created transactions match. (CSV/OFX import is UI-only, there is no API for it) |
| No feed and no imports at all (Xero is the register) | Create transactions with `IsReconciled: true` (or POST the flag after creation). The org shows as reconciled, but understand there are no statement lines behind it; if a feed is connected later, historical lines will arrive unmatched (see 2.1 caveats) |
| Unallocated customer receipts | `RECEIVE-OVERPAYMENT` bank transaction now, `PUT /Overpayments/{id}/Allocations` when the invoice is known |

**Duplicate safety:** before creating, query existing transactions with `where` on `Date`, `Total`/`Amount`, `BankAccount.AccountID` and your `Reference`, plus `If-Modified-Since` for incremental syncs.

---

### 2.8 Verifying reconciliation state, reports & Finance API

- **`GET /Reports/BankSummary?fromDate=…&toDate=…`**, per-bank-account opening balance, cash received/spent, closing balance for the period. Good coarse check that your created transactions land where expected. (No per-line reconciliation status.)
- **Bank Statement report**, the Accounting API documents a Bank Statements page (`…/api/accounting/bankstatements`) exposing imported statement line data as a report for a given bank account and date range (historically `GET /Reports/BankStatement?bankAccountID=…&fromDate=…&toDate=…`). It is absent from the current OpenAPI spec and some SDKs, and the page could not be fetched during research, **(verify: exact path, parameters and per-line reconciled status column before building on it; see also 5.2, which records the report as restricted in April 2024 and since removed from the docs)**.
- **Finance API, Bank Statements Plus:** `GET https://api.xero.com/finance.xro/1.0/BankStatementsPlus/statements?BankAccountID=…&FromDate=…&ToDate=…&SummaryOnly=true`, scope `finance.bankstatementsplus.read`. Returns imported statements (with `importSource`, start/end balances) and statement lines including **`isReconciled`, `isDuplicate`, `isDeleted`**, plus, for reconciled lines, the matched `bankTransactions` and `payments` (with invoice/credit-note/prepayment/overpayment detail unless `SummaryOnly`). Max query range 12 months; `ToDate` cannot be in the future. This is the closest thing to a read-only view of statement-line reconciliation state. Caveat: the Finance API is positioned for lending/financial-services use cases; confirm your app can be granted the scope **(verify access conditions)**.
- **Unreconciled Xero-side transactions:** `GET /BankTransactions?where=IsReconciled==false` (add `BankAccount.AccountID` and date filters) enumerates account transactions still awaiting a match; `Payment.IsReconciled` (writable, see 2.2) and the read-only `BatchPayment.IsReconciled` (see 2.3) provide the same signal for payments.
*(Section source: https://developer.xero.com/documentation/api/accounting/*, verified 2026-08-18)*

---

## 3. Sales & Purchases

All endpoints below live under `https://api.xero.com/api.xro/2.0/` and follow the Accounting API's shared conventions: PUT creates only; POST creates **or** updates (specify the resource ID in the URL or body to update); responses default to XML unless `Accept: application/json` is sent; append `?SummarizeErrors=false` on bulk writes so each entity comes back with its own `StatusAttributeString` (`OK` / `WARNING` / `ERROR`) and per-entity `ValidationErrors`. Paging (where supported) defaults to `pageSize=100`, maximum 1000 (out-of-range values are clamped); a `pagination` metadata object is returned. Amounts on every document are in the document's own currency.

### 3.1 Invoices (`/Invoices`)

The single endpoint covers both **ACCREC** (sales invoice / accounts receivable) and **ACCPAY** (bill / supplier invoice / accounts payable). Methods: GET, POST (create or update), PUT (create only). Also supports: delete drafts, void approved invoices, online invoice URL (ACCREC), emailing (ACCREC), attachments, history & notes, PDF retrieval of an individual invoice, and webhooks (create/update events).

Key type differences:

| | ACCREC | ACCPAY |
|---|---|---|
| Meaning | Sales invoice to a customer | Bill from a supplier |
| `InvoiceNumber` | Unique; auto-generated from org invoice settings if omitted (max 255) | Non-unique; displays as "Reference" in the Xero UI (max 255) |
| Line discounts (`DiscountRate`/`DiscountAmount`) | Supported | **Not supported** |
| Online invoice URL / Email endpoint | Yes | No |
| `ExpectedPaymentDate` / `PlannedPaymentDate` | ExpectedPaymentDate | PlannedPaymentDate |

#### Status lifecycle

| Status | Meaning | Journals posted? | Valid transitions from here |
|---|---|---|---|
| `DRAFT` | Default on create; may contain incomplete lines (e.g. missing account codes); not in reports | No | DRAFT, SUBMITTED, AUTHORISED, DELETED |
| `SUBMITTED` | Awaiting approval | No | SUBMITTED, AUTHORISED, DRAFT, DELETED |
| `AUTHORISED` | Approved; appears in reports; payments can now be applied | Yes | AUTHORISED, VOIDED |
| `PAID` | Fully paid, set **by the system** when payments/credits cover the total (never set directly) | Yes | None (remove payments/allocations to revert) |
| `VOIDED` | Voided approved invoice (only possible when **no payments** are applied) | Reversed | Terminal |
| `DELETED` | Deleted DRAFT/SUBMITTED invoice | Never existed | Terminal |

- New invoices may be created directly as `DRAFT` (default), `SUBMITTED`, or `AUTHORISED`.
- **Creating a PAID invoice is a two-step process**: POST an `AUTHORISED` invoice, then apply a Payment via the Payments endpoint; Xero moves it to `PAID` when fully paid.
- **Delete vs void**: POST `{"Status": "DELETED"}` for DRAFT/SUBMITTED; POST `{"Status": "VOIDED"}` for AUTHORISED. An AUTHORISED invoice cannot be DELETED, and a DRAFT cannot be VOIDED.

#### Required fields (create)

Minimum for a draft: `Type`, `Contact` (supply **only** `ContactID`), and `LineItems` (at least one line to be a complete invoice). Everything else is optional: `Date` (defaults to today in org timezone), `DueDate`, `LineAmountTypes` (defaults to `Exclusive`), `InvoiceNumber`, `Reference`, `BrandingThemeID`, `Url`, `CurrencyCode` (defaults to org base currency), `CurrencyRate`, `Status`, `SentToContact` (only settable on approved invoices), `ExpectedPaymentDate`/`PlannedPaymentDate`.

`LineAmountTypes`: `Exclusive` (line amounts exclude tax, default), `Inclusive` (include tax), `NoTax`.

Line item fields: `Description` (≥1 char, max 4000; a description-only line is allowed), `Quantity` (max length 13), `UnitAmount` (2 dp by default; pass `?unitdp=4` for 4 dp), `ItemCode`, `AccountCode`, `LineItemID`, `TaxType` (override of the account's default tax code), `TaxAmount` (auto-calculated from the tax rate but can be overridden), `LineAmount` (= Quantity × UnitAmount × ((100 − DiscountRate)/100); cap 9,999,999,999.99), `DiscountRate`/`DiscountAmount` (ACCREC + quotes only), `Tracking` (max 2 tracking categories per line, `Name` + `Option` required).

Example, create an approved ACCPAY invoice (bill):

```http
POST https://api.xero.com/api.xro/2.0/Invoices
```

```json
{
  "Type": "ACCPAY",
  "Contact": { "ContactID": "eaa28f49-6028-4b6e-bb12-d8f6278073fc" },
  "Date": "2026-08-01",
  "DueDate": "2026-08-20",
  "InvoiceNumber": "SUPP-INV-8871",
  "LineAmountTypes": "Exclusive",
  "Status": "AUTHORISED",
  "LineItems": [
    {
      "Description": "Monthly electricity",
      "Quantity": 1.0,
      "UnitAmount": 295.00,
      "AccountCode": "445",
      "TaxType": "INPUT",
      "Tracking": [ { "Name": "Region", "Option": "North" } ]
    }
  ]
}
```

(Shape assembled from the documented required/optional fields; the doc's own examples use ACCREC but the schema is identical apart from the type-specific rules above.)

#### POST vs PUT and line-item update semantics

- `PUT /Invoices`, create only. `POST /Invoices`, create or update; to update, include `InvoiceID`/`InvoiceNumber` in the body or address the resource directly (`POST /Invoices/{InvoiceID or InvoiceNumber}`).
- Updates **replace the line set**: a line with its `LineItemID` is updated; a line without `LineItemID` is created; **any existing line omitted from the request is deleted**. Always send the full line set with `LineItemID`s on update.
- `?allowBackorders=true` allows creating/updating an invoice with backordered tracked items; otherwise such a request 400s with "Insufficient stock. Items are eligible for backorder."
- The invoice address comes from the contact's POBOX address (or a custom branding template); it cannot be overridden per invoice.
- US orgs: invoices with auto sales tax calculation (`SalesTaxCalculationTypeCode` = TAXCALC/AUTO) are **read-only**.

#### Which fields are editable at each status

- **DRAFT / SUBMITTED**: everything editable (lines may even be incomplete in DRAFT).
- **AUTHORISED, unpaid**: still updatable (both types), lines, contact, dates, etc., but only the AUTHORISED→AUTHORISED and AUTHORISED→VOIDED transitions are valid.
- **AUTHORISED/PAID with any payment applied** (part or full): only these fields may change:
  - Both types: `Reference`, `DueDate`, `InvoiceNumber`, `BrandingThemeID`, `Url`, `Contact` (not if paid via a credit note; ACCPAY also not if lines contain a CIS labour expense account), and per line: `Description`, `AccountCode` (except CIS account codes), `Tracking`.
  - ACCPAY additionally: `PlannedPaymentDate`.
- **In a locked period**: no updates at all (either type).

#### Reading efficiently

- Single invoice by `InvoiceID` or `InvoiceNumber`; add `Accept: application/pdf` to fetch the PDF. Multi-invoice GETs return contact summaries and **no line items** unless you page.
- **Paging**: `?page=1[&pageSize=250]` (default 100, max 1000) returns full line detail. Pagination is enforced when filtering by `Statuses` or using `summaryOnly`.
- **High-volume threshold**: requests that would return >100k invoices, or that filter/order on unoptimised fields hitting >100k, are rejected with 400.
- **Optimised list filters** (comma-separated): `?Statuses=`, `?IDs=`, `?InvoiceNumbers=`, `?ContactIDs=`.
- **Optimised `where` fields**: `Status`, `Contact.ContactID`, `Contact.Name`, `Contact.ContactNumber`, `Reference`, `InvoiceNumber`, `InvoiceId`, `Date`, `Type`, `AmountDue`, `AmountPaid`, `DueDate` (Date/AmountDue/DueDate support ranges `>,>=,<,<=`). `OR` is optimised only for `InvoiceId`.
- **Optimised ordering**: `InvoiceId`, `UpdatedDateUTC`, `Date`; default order `UpdatedDateUTC ASC, InvoiceId ASC`.
- `ModifiedAfter` = `If-Modified-Since` HTTP header (UTC timestamp) for delta sync.
- `summaryOnly=true`: lightweight response excluding `Payments`, `HasAttachments`, `LineItems`, `CISDeduction`; can't be combined with filters on those fields; paging enforced.
- `createdByMyApp=true`: only invoices your app created. `SearchTerm`: case-insensitive search over `InvoiceNumber` and `Reference`.

#### Emailing and online invoice URL

- `POST /Invoices/{InvoiceID}/Email` (empty body), ACCREC only, status must be SUBMITTED, AUTHORISED or PAID. Sends to the contact's primary email plus contact persons with `IncludeInEmails=true`, using the org's default email template; sender is the authorising user. Success = **204**. Daily org-wide limits: 1,000 (paying), 20 (trial), 0 (demo); suspicious-activity restrictions surface as 400.
- `GET /Invoices/{InvoiceID}/OnlineInvoice`, returns `OnlineInvoiceUrl` for ACCREC invoices (not available for DRAFT).
- Mark as sent: POST `{"SentToContact": true}` (approved invoices only).

#### Attachments, history, notes

- `POST/PUT /Invoices/{InvoiceID}/Attachments/{FileName}` with a raw byte body. The invoices page says up to 10 attachments of up to 25 MB each; the central Attachments page says 10 MB each (Xero docs conflict, verify; see 5.3). `?IncludeOnline=true` makes an attachment visible on the online invoice (ACCREC invoices and ACCREC credit notes).
- `GET /Invoices/{Guid}/History` and `PUT /Invoices/{Guid}/History` (`HistoryRecords` with `Details`) for audit history and notes.

### 3.2 Credit Notes (`/CreditNotes`)

Methods: GET, POST (create/update draft, create approved), PUT (create only; also allocations), DELETE (allocations only). Types: **ACCRECCREDIT** (customer credit note; unique `CreditNoteNumber`, `Reference` supported) and **ACCPAYCREDIT** (supplier credit note; non-unique number shown as Reference). Statuses are the **same set as invoices** (DRAFT, SUBMITTED, AUTHORISED, PAID, VOIDED, DELETED): delete drafts by setting `Status: "DELETED"`, void approved ones with `Status: "VOIDED"`, the types doc states status/line-amount behaviour is identical to invoices.

- Create with `Type` + `Contact` minimum (a draft with no lines is allowed); same LineItems schema as invoices; `Status: "AUTHORISED"` to create approved. Discounts are not supported on ACCPAY-side documents.
- **Allocations**: `PUT /CreditNotes/{CreditNoteID}/Allocations` with `{"Amount": 60.50, "Invoice": {"InvoiceID": "..."}}`. The credit note must be `AUTHORISED`; you **cannot create and allocate in one call** (two separate requests). Allocation `Date` is read-only (the later of invoice date and credit note date). Remove with `DELETE /CreditNotes/{CreditNoteID}/Allocations/{AllocationID}` (response includes `"IsDeleted": true`).
- **Refunds** of credit balances are made via the Payments endpoint, not here.
- Useful read fields: `RemainingCredit`, `Allocations[]`, `FullyPaidOnDate`. GET supports paging (default 100), `If-Modified-Since`, optimised `where` on `Status`, `Date` (range), `Reference`, `Contact.*`, `Type`; optimised ordering `CreditNoteID`/`UpdatedDateUTC`/`Date`; 100k high-volume threshold. Attachments: same pattern as invoices.

### 3.3 Quotes (`/Quotes`)

Methods: GET, POST (create or update), PUT (create only). ACCREC-side only. Required to create a draft: `Contact`, `Date`, `LineItems` (a description-only line suffices). `QuoteID` required for updates. Optional: `LineAmountTypes`, `Status`, `ExpiryDate`, `CurrencyCode`/`CurrencyRate`, `QuoteNumber` (unique, max 255), `Reference`, `BrandingThemeID`, `Title` (100), `Summary` (3000), `Terms` (4000). Quirks: line `TaxType` does **not** auto-populate from the account code default if omitted; line tracking accepts `TrackingOptionID` only.

Statuses: `DRAFT` (default), `SENT`, `ACCEPTED`, `DECLINED`, `INVOICED`, `DELETED`.

Valid transitions: DRAFT→SENT/DELETED; SENT→ACCEPTED/DECLINED/DELETED; DECLINED→SENT/DELETED; ACCEPTED→INVOICED/SENT/DELETED; INVOICED→SENT/DELETED.

Editable fields by status: DRAFT and SENT, all fields; DECLINED / ACCEPTED / INVOICED, contact details and notes only.

GET filters: `QuoteNumber` (**partial match**), `Status`, `DateFrom/DateTo`, `ExpiryDateFrom/ExpiryDateTo`, `ContactID`, `If-Modified-Since`, `order`, `page`/`pageSize` (up to 1000). Individual quotes retrievable as PDF. History and notes supported.

### 3.4 Purchase Orders (`/PurchaseOrders`)

Methods: GET, POST (create/update), PUT (create only). Required: `Contact` (must be an existing contact, the endpoint does not create contacts) and at least one `LineItem`. Optional: `Date` (defaults today), `DeliveryDate`, `LineAmountTypes`, `PurchaseOrderNumber` (unique; auto-generated if omitted), `Reference`, `BrandingThemeID`, `CurrencyCode`/`CurrencyRate`, `Status`, `SentToContact` (only on approved/billed POs), delivery block (`DeliveryAddress`, `AttentionTo`, `Telephone`, `DeliveryInstructions` max 500, `ExpectedArrivalDate`).

Statuses: `DRAFT`, `SUBMITTED`, `AUTHORISED`, `BILLED`, `DELETED`. Delete via POST `{"Status": "DELETED"}`. The docs don't publish a PO transition matrix; `BILLED` corresponds to the PO having been copied to a bill. **There is no API call to convert a PO into an ACCPAY invoice**, create the bill via `/Invoices` and set the PO's status yourself (verify, inferred from the absence of any conversion endpoint in the official docs).

Line items follow the invoice schema (plus `ItemID`/`AccountID` variants; `DiscountRate` supported); the same replace-semantics apply on update (omit a `LineItemID` ⇒ that line is deleted and recreated). GET: paging enforced by default (100/page, `pageSize` up to 1000), filters `Status`, `DateFrom/DateTo`, `If-Modified-Since`; 100k threshold; PDF retrieval; history/notes; `HasAttachments` returned.

### 3.5 Repeating Invoices (`/RepeatingInvoices`)

Templates that generate invoices on a schedule. Supported via API: **GET (read templates), POST (create templates and delete templates), PUT (create only), history/notes**. There is **no documented update of an existing template's contents**, POST is described as "create or delete"; delete by POSTing the template with `Status: "DELETED"`.

- Reading returns both ACCREC and ACCPAY templates; **creation via API is ACCREC only** ("Type needs to be ACCREC" is listed as required).
- Required on create: `Type` (ACCREC), `Contact`, `Schedule`, `LineItems`, `LineAmountTypes`, `CurrencyCode`, `Status` (`DRAFT` or `AUTHORISED`, the only template statuses).
- `Schedule`: `Period` + `Unit` (`WEEKLY`/`MONTHLY`), `DueDate` + `DueDateType` (payment-terms enum, e.g. `OFCURRENTMONTH`, `DAYSAFTERBILLDATE`), `StartDate`, optional `EndDate`; read-only `NextScheduledDate`.
- Email automation flags: `ApprovedForSending`, `SendCopy`, `MarkAsSent`, `IncludePDF`. Setting `ApprovedForSending: true` with status AUTHORISED makes Xero email each generated invoice (not for invoices generated with past dates when `StartDate` is in the past).
- Generated invoices carry `RepeatingInvoiceID` back on the Invoices endpoint. GET supports `where`/`order` but no paging parameters are documented.

### 3.6 Items (`/Items`)

Methods: GET, POST (create/update, max 500 per request; 50-100 recommended), PUT (create only), DELETE.

- **Untracked items**: goods/services with default sales/purchase details; Xero does not track quantity or value. Minimum create: `Code` (max 30). Optional: `Name` (50), `IsSold`/`IsPurchased` (default true; setting false nulls the corresponding description/details), `Description`/`PurchaseDescription` (4000), `SalesDetails`/`PurchaseDetails` (`UnitPrice`, 2 dp default, `unitdp=4` opt-in, `AccountCode`, `TaxType`).
- **Tracked inventory items** (`IsTrackedAsInventory: true`): additionally require `InventoryAssetAccountCode` (an account of type `INVENTORY`) **and** `COGSAccountCode` inside `PurchaseDetails` (which then takes no `AccountCode`). Read-only stock fields: `QuantityOnHand`, `QuantityOnBackOrder`, `QuantityAvailable`, `TotalCostPool` (average cost), these **cannot be set via the API**; quantity/value change only through transactions (ACCPAY invoices / SPEND bank transactions increase; ACCREC invoices / RECEIVE bank transactions decrease).
- On invoice lines, specifying `ItemCode` pulls the item's default price/account; explicit `UnitAmount`/`AccountCode` override them.
- DELETE `/Items/{ItemID}`: irreversible. Untracked items delete any time (history keeps them). Tracked items can't be deleted once used on an approved invoice/bill, spend/receive money, opening balance or adjustment; items on repeating invoices/bills must be removed from the template first; otherwise 400.

### 3.7 Expense Claims & Receipts (legacy) (`/ExpenseClaims`, `/Receipts`)

**Deprecated/legacy.** Both pages carry this notice: classic expense claims are only available to organisations that used them in the 6 months before 10 July 2018; new integrations should create **ACCPAY invoices (bills)** instead. The newer **Xero Expenses** product is a separate offering, not served by these endpoints (the Accounting API has no Xero Expenses endpoints, verify against the Xero Expenses/Projects API docs if you need programmatic access to it).

- **Receipts** = draft expense-claim receipts. GET/PUT/POST; statuses `DRAFT` (default), `SUBMITTED` (part of a claim), `AUTHORISED`, `DECLINED`. Lines need `AccountCode`s with `ShowInExpenseClaims=true` (bank accounts not allowed); tracking must use `Name`/`Option` (not IDs); no discounts. Attachments (receipt images) supported.
- **ExpenseClaims** wrap a `User` + one or more existing `Receipts` (by `ReceiptID`). Statuses: `SUBMITTED` (default), `AUTHORISED`, `PAID`; the docs also show voiding via POST `{"Status": "VOIDED"}`. **Payment cannot be made via the API**, expense claims are paid in the Xero app.

### 3.8 Linked Transactions, billable expenses (`/LinkedTransactions`)

Links a **source** purchase line (ACCPAY invoice line or SPEND bank-transaction line) to a customer and optionally a **target** ACCREC invoice line, exposing Xero's billable-expenses feature. Methods: GET, POST (create/update), PUT (create only), DELETE (`DELETE /LinkedTransactions/{LinkedTransactionID}`).

- Create requires `SourceTransactionID` + `SourceLineItemID`; optional `ContactID` (customer to on-bill), `TargetTransactionID` + `TargetLineItemID` (the ACCREC invoice line; several linked transactions can share one target line). `Type` is always `BILLABLEEXPENSE`.
- `Status` is **derived, never set**: `DRAFT` (source draft, unallocated), `APPROVED` (source authorised, unallocated), `ONDRAFT` (allocated to a draft target), `BILLED` (allocated to an authorised target), `VOIDED` (source voided).
- GET filters: `SourceTransactionID`, `ContactID`, `ContactID`+`Status`, `TargetTransactionID`; paging enforced, fixed 100/page.

### 3.9 Correction patterns for coded transactions

All patterns below follow directly from the documented rules above and the Payments endpoint.

**Editing an AUTHORISED invoice with no payments.** Just POST the changes (full line set, keeping `LineItemID`s, omitted lines are deleted). Amounts, tax, contact, dates, account codes are all fair game, provided the invoice isn't in a locked period and isn't a US auto-sales-tax invoice.

**Editing an AUTHORISED/PAID invoice with payments applied.** Only re-coding metadata is possible: `Reference`, `DueDate`, `InvoiceNumber`, `BrandingThemeID`, `Url`, usually `Contact`, and per-line `Description`, `AccountCode` (non-CIS) and `Tracking`. That last trio means **pure recoding (wrong account/tracking) does not require touching payments**, update the line's `AccountCode`/`Tracking` in place, keeping the amounts identical. Amount, quantity, tax and currency changes are blocked.

**Correcting amounts/tax on a paid invoice, remove payment, edit, re-apply:**
1. `POST /Payments/{PaymentID}` with `{"Status": "DELETED"}` (payments can only be created and deleted, never modified; payments made via batch payments or receipts can't be deleted this way, handle those via the BatchPayments endpoint / UI). Deleting the payment reverts the invoice from PAID to AUTHORISED.
2. If a credit note is allocated, `DELETE /CreditNotes/{id}/Allocations/{AllocationID}`.
3. Either edit the now-unpaid AUTHORISED invoice directly, or, cleaner for audit trails, void it and reissue.
4. Re-apply the payment(s) via `PUT /Payments`.

**Void and reissue.** For an AUTHORISED invoice (payments removed first, voiding requires no payments applied): POST `{"Status": "VOIDED"}`, then PUT a corrected replacement invoice. For DRAFT/SUBMITTED, use `{"Status": "DELETED"}` instead. VOIDED/DELETED are terminal; a voided invoice cannot be un-voided.

**Adjusting without touching the original.** Issue an `AUTHORISED` credit note (ACCRECCREDIT against a sales invoice, ACCPAYCREDIT against a bill) and allocate it to the invoice via `PUT /CreditNotes/{id}/Allocations`, the documented pattern for reducing an already-sent invoice. Note that once a payment "is made with a Credit Note", the invoice's `Contact` becomes non-editable.

**Locked periods.** No invoice update of any kind succeeds in a locked period; corrections there require changing the org's lock date or posting adjusting documents (e.g. credit notes dated in an open period).

**Audit trail.** Every document type here supports `PUT .../{Guid}/History` notes, write a note explaining each automated correction; history also reveals prior user/system actions before you mutate a document.
*(Section source: developer.xero.com Accounting API docs and the official XeroAPI/Xero-OpenAPI spec, xero_accounting.yaml v17.0.0)*

---

## 4. Ledger & Organisation Setup

All endpoints below live under `https://api.xero.com/api.xro/2.0/` and require the `Authorization: Bearer {access_token}` and `xero-tenant-id: {tenantId}` headers. Write endpoints accept an optional `Idempotency-Key` header (max 128 characters) so retries cannot double-post. Bulk create/update endpoints accept `summarizeErrors=false` to get a 200 response containing a mix of successes and per-object `ValidationErrors` instead of an all-or-nothing 400.

| Endpoint | Verbs | OAuth 2.0 scope(s) |
|---|---|---|
| `/ManualJournals` | GET, PUT, POST | `accounting.transactions` (read: `accounting.transactions.read`) |
| `/Journals` | GET only | `accounting.journals.read` |
| `/Accounts` | GET, PUT, POST, DELETE | `accounting.settings` (read: `accounting.settings.read`) |
| `/TaxRates` | GET, PUT, POST | `accounting.settings` (read: `accounting.settings.read`) |
| `/TrackingCategories` | GET, PUT, POST, DELETE | `accounting.settings` (read: `accounting.settings.read`) |
| `/Currencies` | GET, PUT | `accounting.settings` (read: `accounting.settings.read`) |
| `/Organisation`, `/Organisation/Actions` | GET only | `accounting.settings` or `accounting.settings.read` |
| `/Users` | GET only | `accounting.settings` or `accounting.settings.read` |
| `/Budgets` | GET only | `accounting.budgets.read` |

(`accounting.transactions` / `.read` are the broad scopes, usable until September 2027; the granular equivalent for `/ManualJournals` is `accounting.manualjournals` / `.read`, see 1.4.)

### 4.1 Manual Journals (`/ManualJournals`)

The only write path into the general ledger. `PUT` creates one or more new manual journals (create-only); `POST` creates or updates (single journal via `/ManualJournals/{ManualJournalID}`, or an array at the collection URL).

**Fields**

| Field | Notes |
|---|---|
| `Narration` | **Mandatory.** Description of the journal being posted. |
| `JournalLines` | **Mandatory.** Must contain **at least two** `JournalLine` elements. |
| `Date` | Recommended. `YYYY-MM-DD`; defaults to the current date if omitted. |
| `LineAmountTypes` | Optional: `Exclusive`, `Inclusive`, `NoTax`. For manual journals the default is `NoTax` if not specified. |
| `Status` | Optional. `DRAFT` (default), `POSTED`, `DELETED`, `VOIDED` (see below). |
| `Url` | Optional link to a source document, shown as "Go to [appName]" in Xero. |
| `ShowOnCashBasisReports` | Optional boolean; **defaults to true**. Controls whether the journal appears on cash-basis reports. |
| `ManualJournalID` | Xero-generated UUID (returned; used for update/attachment URLs). |
| `HasAttachments`, `UpdatedDateUTC` | Read-only. |

**Journal line fields**

| Field | Notes |
|---|---|
| `LineAmount` | **Mandatory.** Debits are positive, credits are negative. |
| `AccountCode` | **Mandatory** (the spec also allows `AccountID`). |
| `Description` | Optional per-line description. |
| `TaxType` | Optional override when the selected account's default tax code is not correct. `TaxAmount` is then calculated by Xero from `TaxType` + `LineAmount` (interpretation depends on `LineAmountTypes`). |
| `Tracking` | Optional array of `{ "Name": ..., "Option": ... }`; **maximum 2 tracking categories per line**. |

**Balancing.** The journal must balance: total debits (positive `LineAmount`s) must equal total credits (negative `LineAmount`s), i.e. line amounts must net to zero, otherwise the API returns a 400 validation error. With `Exclusive`/`Inclusive` tax the balance is evaluated on the tax-adjusted amounts, so mixed tax types on either side can also trigger an imbalance error (verify exact evaluation rule against a test organisation).

**Restricted accounts.** You cannot journal to system accounts (accounts receivable, accounts payable, retained earnings) or to bank accounts; you get a 400 validation error. Xero's documented workaround is to set up one or more clearing accounts.

**Statuses, voiding vs deleting**

| Status | Meaning |
|---|---|
| `DRAFT` | Default on create. |
| `POSTED` | Posted to the ledger (appears in the Journals feed as `SourceType: MANJOURNAL`). |
| `DELETED` | A deleted **draft** manual journal. |
| `VOIDED` | A voided **posted** manual journal. |

So: to remove a draft, `POST` it with `"Status": "DELETED"`; to reverse a posted journal, `POST` it with `"Status": "VOIDED"`. Posted journals cannot be deleted, only voided. (The OpenAPI enum also lists `ARCHIVED`, which does not appear in the documented status-code table; treat as unused (verify).)

**Retrieval.** `GET /ManualJournals` supports `where`, `order`, `If-Modified-Since`, and paging via `page` (line items are only included when paging or fetching a single journal). Up to 100 manual journals per page by default; a `pageSize` parameter is supported (Xero raised the page-size ceiling to 1000 per its developer blog; confirm current maximum) (verify).

**Attachments and history.** Up to **10 attachments, each up to 3 MB**, per manual journal (the 3 MB figure is from the manual journals page; other pages state 10 MB or 25 MB, Xero docs conflict, verify; see 5.3), uploaded after creation as a raw byte stream: `PUT/POST /ManualJournals/{ManualJournalID}/Attachments/{FileName}` with the appropriate `Content-Type`. `GET .../Attachments` lists them. `GET/PUT /ManualJournals/{ManualJournalID}/History` reads and adds history notes.

**Example: create a balanced draft accrual journal (PUT /ManualJournals)**

```json
{
  "ManualJournals": [
    {
      "Narration": "Monthly accrual: contractor fees for July 2026",
      "Date": "2026-07-31",
      "LineAmountTypes": "NoTax",
      "Status": "DRAFT",
      "ShowOnCashBasisReports": false,
      "Url": "https://example.com/workpapers/2026-07/accrual-113",
      "JournalLines": [
        {
          "LineAmount": 4500.00,
          "AccountCode": "412",
          "Description": "Contractor fees incurred, not yet billed",
          "Tracking": [
            { "Name": "Region", "Option": "Eastside" }
          ]
        },
        {
          "LineAmount": -4500.00,
          "AccountCode": "801",
          "Description": "Accrued expenses"
        }
      ]
    }
  ]
}
```

To post it later: `POST /ManualJournals/{ManualJournalID}` with `{ "ManualJournalID": "...", "Narration": "...", "Status": "POSTED" }` (include the existing lines/fields you want preserved; POST is an upsert of the journal).

### 4.2 Journals: the read-only general ledger feed (`/Journals`)

`GET` only. Every transaction **posted** to the ledger (invoices, payments, bank transactions, manual journals, payroll, etc.) surfaces here as one or more journals; this is the canonical feed for building a local GL mirror and running audit checks. If you need to create journals, use Manual Journals.

**Paging model (offset, not page numbers).** A maximum of **100 journals** is returned per response, ordered **oldest to newest** by `JournalNumber`. Use the `offset` parameter: journals with a `JournalNumber` **greater than** the offset are returned. The sync loop is:

1. `GET /Journals` (first call, no offset).
2. Take the `JournalNumber` of the last journal returned; call `GET /Journals?offset={lastJournalNumber}`.
3. Repeat until a response returns fewer than 100 journals (store the high-water mark for incremental syncs).

`If-Modified-Since` is also supported for incremental pulls. The `where`, `order` and `page` parameters are **not** available on this endpoint.

**Cash vs accrual.** Journals are returned on an **accrual basis by default**; pass `paymentsOnly=true` to retrieve journals on a **cash basis**.

**Journal fields:** `JournalID`, `JournalDate`, `JournalNumber` (Xero-generated, sequential), `CreatedDateUTC`, `Reference`, `SourceID` (the identifier of the source transaction, e.g. an InvoiceID), `SourceType`, `JournalLines`.

Note: `SourceType`/`SourceID` are **not returned** when retrieving an individual journal via `GET /Journals/{JournalID}` or `GET /Journals/{JournalNumber}` (the docs state this explicitly for `SourceType`; the same behaviour for `SourceID` (verify)).

**Journal line fields:** `JournalLineID`, `AccountID`, `AccountCode`, `AccountType`, `AccountName`, `Description` (from the source line item, only if populated), `NetAmount` (positive = debit, negative = credit), `GrossAmount` (= NetAmount + TaxAmount), `TaxAmount`, `TaxType`, `TaxName`, `TrackingCategories`.

**SourceType values** (what created the journal):

| SourceType | Source | SourceType | Source |
|---|---|---|---|
| `ACCREC` | Sales invoice | `AROVERPAYMENT` | AR overpayment |
| `ACCPAY` | Bill (purchase) | `APOVERPAYMENT` | AP overpayment |
| `ACCRECCREDIT` | Sales credit note | `EXPCLAIM` | Expense claim |
| `ACCPAYCREDIT` | Purchase credit note | `EXPPAYMENT` | Expense claim payment |
| `ACCRECPAYMENT` | Invoice payment | `MANJOURNAL` | Manual journal |
| `ACCPAYPAYMENT` | Bill payment | `PAYSLIP` | Payslip |
| `ARCREDITPAYMENT` | AR credit note payment | `WAGEPAYABLE` | Payroll payable |
| `APCREDITPAYMENT` | AP credit note payment | `INTEGRATEDPAYROLLPE` | Integrated payroll PE |
| `CASHREC` | Receive money bank transaction | `INTEGRATEDPAYROLLPT` | Integrated payroll PT |
| `CASHPAID` | Spend money bank transaction | `INTEGRATEDPAYROLLPTPAYMENT` | Integrated payroll PT payment |
| `TRANSFER` | Bank transfer | `INTEGRATEDPAYROLLCN` | Integrated payroll CN |
| `ARPREPAYMENT` | AR prepayment | `EXTERNALSPENDMONEY` | External spend money |
| `APPREPAYMENT` | AP prepayment | | |

(Enum list from the official OpenAPI spec; per-value descriptions abbreviated.)

**Audit/mirror notes.**
- Journal lines carry `AccountType`, tax detail and tracking, so per-account and per-tracking-option balances can be recomputed locally and reconciled against the Trial Balance report.
- The feed has no filter to exclude journals whose **source transaction** was later voided or deleted; voiding generates reversing journal entries rather than removing history. To flag reversals, group by `SourceID`/`SourceType` and check the source transaction's current status via its own endpoint (practical guidance; not stated on the Journals doc page itself).
- Journal numbers are the natural cursor: persist the max `JournalNumber` per tenant and resume from it.

### 4.3 Accounts / Chart of Accounts (`/Accounts`)

`GET /Accounts` returns the full chart of accounts (supports `where`, e.g. `Status=="ACTIVE" AND Type=="BANK"`, `order`, `If-Modified-Since`). `GET /Accounts/{AccountID}` returns one account.

**Fields**

| Field | Notes |
|---|---|
| `Code` | Customer-defined alphanumeric code, e.g. `200` or `SALES` (max length 10). **Mandatory on create** for non-bank accounts. |
| `Name` | **Mandatory on create** (max length 150). |
| `Type` | **Mandatory on create.** See the account type table below. |
| `BankAccountNumber` | **Mandatory for `Type: BANK`**, and only valid for bank accounts. |
| `BankAccountType` | Bank accounts only: `BANK`, `CREDITCARD`, `PAYPAL`, `NONE`. |
| `CurrencyCode` | Bank accounts only. |
| `Status` | `ACTIVE` or `ARCHIVED` (schema also includes `DELETED`). Accounts with status `ACTIVE` can be updated to `ARCHIVED`. |
| `Description` | Max length 4000; valid for all account types **except bank accounts**. |
| `TaxType` | Default tax rate for the account (a `TaxType` code from TaxRates). |
| `EnablePaymentsToAccount` | Boolean: whether payments can be applied to this account (needed if you want to pay invoices/bills directly to it). |
| `ShowInExpenseClaims` | Boolean: whether the code is available for expense claims. |
| `AddToWatchlist` | Boolean: show in the dashboard watchlist widget. Works with POST (update) only, not PUT. |
| `Class` | **Read-only.** `ASSET`, `EQUITY`, `EXPENSE`, `LIABILITY`, `REVENUE` (derived from `Type`). |
| `SystemAccount` | **Read-only.** Returned only for system accounts, e.g. `DEBTORS`, `CREDITORS`, `RETAINEDEARNINGS`, `GST`, `GSTONIMPORTS`, `BANKCURRENCYGAIN`, `REALISEDCURRENCYGAIN`, `UNREALISEDCURRENCYGAIN`, `HISTORICAL`, `ROUNDING`, `TRACKINGTRANSFERS`, `UNPAIDEXPCLM`, `WAGEPAYABLES`, plus UK CIS accounts (`CISASSET`, `CISLABOUR`, `CISLIABILITY`, `CISMATERIALS`, ...). Non-system accounts return this empty or null. |
| `ReportingCode`, `ReportingCodeName` | Shown if set (report codes; partner-oriented). |
| `AccountID`, `HasAttachments`, `UpdatedDateUTC` | Read-only identifiers/metadata. |

**Account Types and their Classes** (types from the docs Types page and OpenAPI spec; Class column per Xero's Class enum, which the API derives from Type):

| Type | Description | Class |
|---|---|---|
| `BANK` | Bank account | ASSET |
| `CURRENT` | Current asset | ASSET |
| `FIXED` | Fixed asset | ASSET |
| `INVENTORY` | Inventory asset | ASSET |
| `NONCURRENT` | Non-current asset | ASSET |
| `PREPAYMENT` | Prepayment | ASSET |
| `EQUITY` | Equity | EQUITY |
| `DEPRECIATN` | Depreciation | EXPENSE |
| `DIRECTCOSTS` | Direct costs | EXPENSE |
| `EXPENSE` | Expense | EXPENSE |
| `OVERHEADS` | Overhead | EXPENSE |
| `CURRLIAB` | Current liability | LIABILITY |
| `LIABILITY` | Liability | LIABILITY |
| `TERMLIAB` | Non-current liability | LIABILITY |
| `OTHERINCOME` | Other income | REVENUE |
| `REVENUE` | Revenue | REVENUE |
| `SALES` | Sales | REVENUE |

Payroll-specific types also exist (`PAYG`/`PAYGLIABILITY`, `SUPERANNUATIONEXPENSE`, `SUPERANNUATIONLIABILITY`, `WAGESEXPENSE`); the Type-to-Class mapping above is the standard accounting classification; exact Class values for the payroll types are unconfirmed (verify).

**Create.** `PUT /Accounts` with e.g. `{ "Code": "200", "Name": "Sales", "Type": "SALES", "TaxType": "OUTPUT" }`. For a bank account: `{ "Name": "Business Cheque", "Type": "BANK", "BankAccountNumber": "..." }`.

**Update (`POST /Accounts/{AccountID}`), documented limitations:**
- Accounts of type `BANK` **cannot be updated** via the API.
- Only **one account per call** can be updated.
- You **cannot combine archiving with other field changes**: updating `Status` to `ARCHIVED` must be its own request.

**Archive vs delete.** Archive with `POST` setting `"Status": "ARCHIVED"`. `DELETE /Accounts/{AccountID}` works only for **non-system accounts that have not been used on any transaction**; if an account cannot be deleted, archive it instead. System accounts cannot be deleted, and cannot be used in manual journals (see 4.1).

**Attachments.** Up to 10 attachments of up to 3 MB each per account via `/Accounts/{AccountID}/Attachments/{FileName}` (the 3 MB figure is from the accounts page; other pages state 10 MB or 25 MB, Xero docs conflict, verify; see 5.3).

### 4.4 Tax Rates (`/TaxRates`)

`GET /TaxRates` (supports `where`, `order`, and filtering to a single rate via `GET /TaxRates/{TaxType}`). `PUT` creates, `POST` creates or updates; **only one tax rate can be created or updated per request**.

**TaxRate fields:** `Name`, `TaxType` (the code used on transactions/accounts; usable only on update calls, since Xero generates the code for new rates), `TaxComponents`, `Status` (`ACTIVE`, `DELETED`, `ARCHIVED`, `PENDING`), `ReportTaxType`, plus read-only `CanApplyToAssets/Equity/Expenses/Liabilities/Revenue`, `DisplayTaxRate` (nominal, to 4 dp) and `EffectiveRate` (compound-adjusted effective rate, to 4 dp).

**TaxComponents** (a rate is the sum of its components, e.g. Canadian GST + PST):

| Field | Notes |
|---|---|
| `Name` | Component name. |
| `Rate` | Percentage, up to 4 dp. |
| `IsCompound` | Compound tax is calculated on top of the other components. |
| `IsNonRecoverable` | Defaults to false; **only applicable to Canadian organisations**. |

**Creating a custom rate.** `PUT /TaxRates` with `Name`, optional `ReportTaxType`, and one or more `TaxComponents`. **`ReportTaxType` is required when creating tax rates for AU, NZ and UK organisations** (it maps the rate onto the tax return, e.g. `INPUT`/`OUTPUT`; the valid set differs by region and invalid values are rejected). Xero assigns the new rate's `TaxType` code (e.g. `TAX001`).

**Updating.** System-defined tax rates **cannot be updated**. When updating a custom rate you must supply **all existing tax components**; components **cannot be renamed or removed**, only added.

**Deleting/archiving.** There is no DELETE verb: `POST` the rate with `"Status": "DELETED"` (or `"ARCHIVED"`).

**Constraints for automation.** Certain rates are only valid with certain account classes (the `CanApplyTo*` flags). Using an incompatible pair on a line yields: `The TaxType code 'xxx' cannot be used with account code 'xxx'.` Default rate sets differ per region (AU, NZ, UK, US, Canada, Singapore, South Africa, Global) and users can rename/add rates, so always `GET /TaxRates` for the connected organisation rather than hard-coding TaxType codes.

### 4.5 Tracking Categories (`/TrackingCategories`)

Tracking categories are Xero's dimension mechanism (e.g. Department, Region), each with tracking options (e.g. East, West).

**Limits:** an organisation can have a maximum of **2 ACTIVE tracking categories**, and 4 in total including ARCHIVED (the "4 total" figure is from the archived docs page; current docs wording (verify)). Any journal/transaction line can reference at most 2 (one option per category). Category and option names: max length 100.

**CRUD:**
- `GET /TrackingCategories` (with `where`, `order`, `includeArchived=true`); `GET /TrackingCategories/{TrackingCategoryID}`. Options are returned nested with `TrackingOptionID`, `Name`, `Status`.
- `PUT /TrackingCategories` creates a category (optionally with options); `PUT /TrackingCategories/{TrackingCategoryID}/Options` adds options to a category.
- `POST /TrackingCategories/{TrackingCategoryID}` renames a category or updates its status; `POST .../Options/{TrackingOptionID}` renames an option or updates its status.
- `DELETE /TrackingCategories/{TrackingCategoryID}` and `DELETE .../Options/{TrackingOptionID}` delete **unused** categories/options; archive (status `ARCHIVED`) those in use.

On transaction/journal lines you supply tracking as `{ "Name": "<category>", "Option": "<option>" }` (or by IDs); both must already exist, the API does not auto-create options.

### 4.6 Currencies (`/Currencies`) and multi-currency

- `GET /Currencies` returns the currencies in use: just `Code` (ISO 4217 3-letter code) and `Description`. Supports `where`/`order`.
- `PUT /Currencies` adds a currency to the organisation (body: `{ "Code": "USD" }`). **Currencies cannot be removed once added.**
- Multi-currency is a plan feature: check `GET /Organisation/Actions` for `UseMulticurrency` = `ALLOWED` before attempting foreign-currency writes.
- Exchange rates are not exposed by this endpoint. Rates live on transactions: documents such as invoices carry a `CurrencyCode` and a `CurrencyRate`; if no rate is specified on creation, **the XE.com day rate is used** (rate max format 18.6). Retrieving a multi-currency transaction returns the exact rate applied. There is no API to query Xero's rate table directly.
- Realised/unrealised FX gains post to the system accounts `REALISEDCURRENCYGAIN` / `UNREALISEDCURRENCYGAIN` / `BANKCURRENCYGAIN` (visible via the Accounts endpoint and in Journals output).

### 4.7 Organisation (`/Organisation` and `/Organisation/Actions`)

`GET /Organisation` (read-only; note the response array key is `Organisations`). Fields most useful for a multi-entity automation setup:

| Field | Why it matters |
|---|---|
| `OrganisationID` | Xero's unique identifier for the organisation. This is distinct in concept from the **tenantId** you send in the `xero-tenant-id` header, which comes from `GET https://api.xero.com/connections`. Resolve each connection's tenantId to its OrganisationID/Name/ShortCode by calling this endpoint once per tenant; do not assume the two values are interchangeable (for organisation tenants they are reported to match, but Xero's guidance is to key API calls off the connections tenantId) (verify). |
| `ShortCode` | Unique org identifier used for deep linking into the Xero UI (e.g. linking a journal for human review). |
| `Name` / `LegalName` | Display name vs name shown on reports. |
| `BaseCurrency` | Reporting currency; all Journals amounts are in base currency terms (verify: Journals amounts are documented as amounts of the journal lines; base-currency representation of foreign transactions). |
| `CountryCode`, `Version` | Region/edition (`AU`, `NZ`, `GLOBAL`, `UK`, `US`, plus ONRAMP variants); drives tax behaviour (see 4.4). |
| `FinancialYearEndDay`, `FinancialYearEndMonth` | Needed to compute financial-year period boundaries for accruals and reviews. |
| `SalesTaxBasis`, `SalesTaxPeriod` | Tax-return accounting basis (`PAYMENTS`, `INVOICE`, `NONE`, `CASH`, `ACCRUAL`, plus UK flat-rate variants) and filing frequency. |
| `DefaultSalesTax`, `DefaultPurchasesTax` | Organisation defaults for `LineAmountTypes`. |
| `PeriodLockDate`, `EndOfYearLockDate` | Shown if set. Check before posting manual journals: writes dated on or before a lock date are rejected. |
| `Timezone` | Organisation timezone enum (e.g. `NEWZEALANDSTANDARDTIME`). |
| `Edition` | `BUSINESS` or `PARTNER`; partner-edition orgs are sold through accounting partners and have restricted functionality. |
| `Class` | The subscription plan: `DEMO`, `TRIAL`, `STARTER`, `STANDARD`, `PREMIUM` (+ `PREMIUM_20/50/100`), `LEDGER`, `GST_CASHBOOK`, `NON_GST_CASHBOOK`, `LITE`, `ULTIMATE` (+ tiers), `IGNITE`, `GROW`, `COMPREHENSIVE`, `SIMPLE`, `ULTRA`, ... Plan determines feature availability (e.g. multi-currency). |
| `PaysTax`, `TaxNumber`, `RegistrationNumber`, `EmployerIdentificationNumber` | Tax registration details (labels vary by region: TFN (AU), GST Number (NZ), VAT Number (UK), Tax ID (US/Global)). |
| `OrganisationStatus`, `IsDemoCompany`, `CreatedDateUTC`, `OrganisationEntityType`, `LineOfBusiness`, `Addresses`, `Phones`, `ExternalLinks`, `PaymentTerms`, `APIKey` | Additional metadata (APIKey is the Xero-to-Xero network key). |

**Organisation Actions endpoint: confirmed, it exists.** `GET /Organisation/Actions` returns the key actions your app can perform in the connected organisation, as `{ "Name": ..., "Status": "ALLOWED" | "NOT-ALLOWED" }`. An action is ALLOWED when the feature exists in the org's plan **and** the connecting user has permission, e.g. `CreateApprovedInvoice`, `UseMulticurrency`. It is explicitly not a comprehensive permission list; it exposes the actions that most frequently differ between plans. Use it at connection time to pre-flight what your automation may do per entity.

UK organisations additionally expose `GET /Organisation/{OrganisationID}/CISSettings` (Construction Industry Scheme settings).

### 4.8 Users (`/Users`) - read-only

`GET /Users` and `GET /Users/{UserID}` only. Supports `where` (e.g. `IsSubscriber==true`), `order`, and `If-Modified-Since`.

Fields: `UserID`, `EmailAddress`, `FirstName`, `LastName`, `UpdatedDateUTC`, `IsSubscriber` (is this user the subscription holder), `OrganisationRole`: one of `READONLY`, `INVOICEONLY`, `STANDARD`, `FINANCIALADVISER`, `MANAGEDCLIENT`, `CASHBOOKCLIENT`, `UNKNOWN`, `REMOVED`.

Useful for attribution in review workflows (History endpoints report which user performed actions) and for checking the role behind a connection. Users cannot be created or modified via the API.

### 4.9 Budgets (`/Budgets`) - read-only

`GET /Budgets` lists budgets; **budget lines are only returned when requesting a single budget** via `GET /Budgets/{BudgetID}`. Query parameters: `IDs` (filter list by BudgetID), `DateFrom`, `DateTo` (`YYYY-MM-DD`). When no period range is requested the response defaults to 3 months (previous, current and next month); a maximum of **24 months** can be retrieved in a single request.

Structure:
- `Budget`: `BudgetID`, `Type` (`OVERALL` = whole-org budget, `TRACKING` = budget for a tracking option combination), `Description` (max 255), `UpdatedDateUTC`, `Tracking` (the tracking category/option a TRACKING budget applies to), `BudgetLines`.
- `BudgetLine`: `AccountID`, `AccountCode`, and `BudgetBalances`.
- `BudgetBalance`: `Period` (e.g. `"2019-08"`), `Amount`, `UnitAmount` (budgeted amount), `Notes` (max 255).

For accounts-review automation: pull the overall budget's per-account monthly balances and compare against actuals recomputed from the Journals feed (4.2) or against the Budget Summary / P&L reports for variance checks. Budgets cannot be created or updated via the public Accounting API.

### 4.10 Batch Payments

Out of scope for this section; `/BatchPayments` (create/retrieve batches of payments against invoices/bills) is covered in section 2 (Banking & Reconciliation), see 2.3.
*(Section source: developer.xero.com official documentation, retrieved 2026-08-18)*

---

## 5. Contacts, Reports & Platform Features

### 5.1 Contacts

`GET/POST/PUT https://api.xero.com/api.xro/2.0/Contacts`
Scopes: `accounting.contacts` (read/write) or `accounting.contacts.read`. Contacts also support attachments, history and notes, and webhooks.

> Xero warns that Contact Name may stop being a unique field in future. Always key on `ContactID`, never on `Name`.

#### Key fields

Returned on every GET:

| Field | Notes |
|---|---|
| `ContactID` | Xero GUID, unique within an organisation |
| `ContactNumber` | External-system identifier; read-only in the Xero UI (shown as "Contact Code"), writable via API only. Max 50 chars |
| `AccountNumber` | User-defined account number, max 50 chars |
| `ContactStatus` | `ACTIVE` / `ARCHIVED` (see Types) |
| `Name` | Full name, max 255 chars; required on create; no angle brackets, no leading/trailing/repeated spaces |
| `FirstName`, `LastName`, `EmailAddress` | Max 255 chars each; email does not support umlauts |
| `CompanyNumber` | Company registration number, max 50 chars |
| `TaxNumber`, `TaxNumberType` | ABN (AU) / GST number (NZ) / VAT number (UK) / Tax ID (US), max 50 chars |
| `AccountsReceivableTaxType`, `AccountsPayableTaxType` | Default tax types for AR/AP invoices |
| `Addresses`, `Phones` | Arrays of typed entries (POBOX/STREET; DEFAULT/FAX/MOBILE/DDI) |
| `IsSupplier`, `IsCustomer` | Read-only booleans, set automatically when AP/AR invoices exist. Cannot be set via POST/PUT |
| `DefaultCurrency` | Default invoicing currency |
| `BankAccountDetails` | Requires the caller's Xero user to have BankAccountAdmin permission to add/edit |
| `UpdatedDateUTC` | Last-modified timestamp (`/Date(...)/` .NET format in JSON) |

Returned **only** when fetching a single contact or when using pagination (unpaged list GETs return just the lightweight subset above):

`ContactPersons` (max 5), `XeroNetworkKey`, `MergedToContactID`, `SalesDefaultAccountCode`, `PurchasesDefaultAccountCode`, `SalesTrackingCategories`, `PurchasesTrackingCategories`, `SalesDefaultLineAmountType` / `PurchasesDefaultLineAmountType` (INCLUSIVE/EXCLUSIVE/NONE), `PaymentTerms`, `ContactGroups`, `Website`, `BrandingTheme`, `BatchPayments`, `Discount`, **`Balances`** (raw AccountsReceivable and AccountsPayable outstanding and overdue amounts, converted to base currency; useful for review criteria), `HasAttachments`.

#### GET parameters, optimised filters and pagination

| Parameter | Behaviour |
|---|---|
| `/{ContactID}` or `/{ContactNumber}` | Fetch a single record by appending the identifier to the URL |
| `If-Modified-Since` header | Only contacts created/modified after the UTC timestamp. Changes to `Balances`, `IsCustomer`, `IsSupplier` do NOT trigger inclusion |
| `IDs` | Comma-separated list of ContactIDs, e.g. `?IDs=guid1,guid2` (optimised bulk fetch) |
| `where` | Filter expression; only `Name`, `EmailAddress`, `AccountNumber` with `=` are optimised (case- and accent-insensitive; do not append `ToLower()`/`ToUpper()`). Avoid `Contains()`/`StartsWith()`/`EndsWith()` on large orgs |
| `searchTerm` | Case-insensitive text search across `Name`, `FirstName`, `LastName`, `ContactNumber`, `CompanyNumber`, `EmailAddress`. This is the optimised replacement for `Contains`-style where clauses |
| `order` | Optimised for `ContactID`, `UpdatedDateUTC`, `Name`. Default order is `UpdatedDateUTC ASC, ContactID ASC` (ContactID secondary ordering keeps pages consistent) |
| `page`, `pageSize` | `page=1` returns 100 by default; `pageSize` up to 1000 (values outside 1 to 1000 are clamped). Paged responses include the full field set plus a `pagination` object (`page`, `pageSize`, `pageCount`, `itemCount`) |
| `includeArchived=true` | Include `ARCHIVED` contacts |
| `summaryOnly=true` | Lightweight response excluding computation-heavy fields (`Addresses`, `Balances`, `ContactGroups`, `ContactPersons`, `IsCustomer`, `IsSupplier`, sales/purchases default account codes and tracking categories). Works with other filters but not when filtering on the excluded fields; enforces pagination |

**High-volume threshold**: requests that would return more than 100k contacts, or that use unoptimised filter/order fields resulting in more than 100k contacts, are rejected with HTTP 400. Use paging plus the optimised filters above.

#### Creating, updating, archiving

- `POST /Contacts` creates or updates (partial update: omitted elements are preserved). Only `Name` is required to create.
- `PUT /Contacts` creates only; errors if an existing contact matches the Name or ContactNumber.
- **Archive** by POSTing `{"ContactID": "...", "ContactStatus": "ARCHIVED"}`.

#### Merging (limitation)

Contacts **cannot be merged via the API**; merging is a Xero UI action only. After a UI merge, the losing contact remains retrievable by ID and exposes `MergedToContactID` pointing at the surviving contact (field returned on single-contact GETs and paged GETs). Multi-entity sync logic should follow `MergedToContactID` to re-point references. (The archived-status of the merged source is expected behaviour but not explicitly documented; verify against a live org.)

#### Contact groups

`GET/PUT/POST/DELETE https://api.xero.com/api.xro/2.0/ContactGroups` (same `accounting.contacts` scopes).

- Elements: `ContactGroupID`, `Name`, `Status` (only `ACTIVE` groups are returned on GET), `Contacts` (ContactID + Name; only returned when fetching a single group by ID).
- Max **100 contact groups** per organisation; creating beyond that returns a validation error.
- `POST /ContactGroups` creates or renames; delete by posting `"Status": "DELETED"`.
- `PUT /ContactGroups/{ContactGroupID}/Contacts` adds contacts to a group (body: `{"Contacts": [{"ContactID": "..."}]}`).
- `DELETE .../Contacts/{ContactID}` removes one contact; `DELETE .../Contacts` removes all.

#### CIS settings (UK)

`GET /Contacts/{ContactID}/CISSettings` returns `CISEnabled` and `Rate` for UK CIS subcontractors; 404 if the contact was never CIS-enabled.

### 5.2 Reports

All reports are read-only: `GET https://api.xero.com/api.xro/2.0/Reports/{ReportName}`, JSON or XML (XML default). Parameters are plain query strings. A Xero user with the Standard role but without the "reports" permission cannot access Reports, Journals, or ManualJournals (HTTP 403).

Scopes: the broad `accounting.reports.read` covers all except 1099, but is flagged in the scopes doc as being superseded by granular scopes: `accounting.reports.aged.read`, `accounting.reports.balancesheet.read`, `accounting.reports.banksummary.read`, `accounting.reports.budgetsummary.read`, `accounting.reports.executivesummary.read`, `accounting.reports.profitandloss.read`, `accounting.reports.trialbalance.read`, `accounting.reports.taxreports.read` (GST/BAS). The 1099 report requires its own `accounting.reports.tenninetynine.read`.

#### Available reports and key parameters

| Report | Endpoint | Required | Optional parameters |
|---|---|---|---|
| Trial Balance | `Reports/TrialBalance` | none | `date` (as-at; current month by default, YTD columns included), `paymentsOnly=true` (cash transactions only) |
| Profit and Loss | `Reports/ProfitAndLoss` | none | `fromDate`, `toDate` (default: current month), `periods` (1 to 11), `timeframe` (`MONTH`/`QUARTER`/`YEAR`), `trackingCategoryID`, `trackingOptionID`, `trackingCategoryID2`, `trackingOptionID2`, `standardLayout=true` (ignore custom report layouts), `paymentsOnly=true` |
| Balance Sheet | `Reports/BalanceSheet` | none | `date` (end of that month, plus same month prior year), `periods` (1 to 11), `timeframe` (`MONTH`/`QUARTER`/`YEAR`), `trackingOptionID1`, `trackingOptionID2` (options, not categories), `standardLayout=true`, `paymentsOnly=true` |
| Aged Receivables By Contact | `Reports/AgedReceivablesByContact` | `contactID` | `date` (payments up to; default end of current month), `fromDate`, `toDate` (invoice range) |
| Aged Payables By Contact | `Reports/AgedPayablesByContact` | `contactID` | `date`, `fromDate`, `toDate` (as above) |
| Bank Summary | `Reports/BankSummary` | none | `fromDate`, `toDate` (balances and cash movements per bank account) |
| Budget Summary | `Reports/BudgetSummary` | none | `date`, `periods` (1 to 12), `timeframe` (numeric here: `1`=month, `3`=quarter, `12`=year) |
| Executive Summary | `Reports/ExecutiveSummary` | none | `date` (monthly totals and common business ratios) |
| 1099 Report (US only) | `Reports/TenNinetyNine` | none | `reportYear` (2012 onward; 2020+ returns both 1099-NEC and 1099-MISC). Requires the advisor user role and the `accounting.reports.tenninetynine.read` scope. Response is NOT row/cell shaped: each report carries a `Contacts` array with `Box1`..`BoxN`, `TaxID`, `FederalTaxIDType`, `FederalTaxClassification` (SOLE_PROPRIETOR, PARTNERSHIP, TRUST_OR_ESTATE, NONPROFIT, C_CORP, S_CORP, OTHER), name/address fields |
| BAS Report (AU only) | `Reports` and `Reports/{ReportID}` | none | Lists published BAS reports; fetch one by appending its `ReportID`. Response uses `Fields` (FieldID/Description/Value) rather than rows |
| GST Report (NZ only) | `Reports` and `Reports/{ReportID}` | none | Same published-report pattern as BAS |

**BankStatement**: there is **no** `Reports/BankStatement` endpoint in the current Accounting API documentation, and the docs state bank statement lines and reconciliation are not exposed via the Accounting API. The old BankStatement report was restricted in April 2024 and has since been removed from the docs (verify exact retirement status for legacy integrations). Bank statement data now lives in the **Finance API** (`BankStatementsPlus`, scope `finance.bankstatementsplus.read`; see 2.8), which is access-gated.

**P&L notes**:
- With `periods` + `fromDate`/`toDate`, the same day-range is applied to each prior period (a 30-day range gives only 30 days of each earlier month).
- Tracking columns are hard-capped at **1000** (options in category 1 times options in category 2, archived options included); exceeding it returns HTTP 400 `ValidationException`.
- In most regions the standard layout groups multi-currency system accounts into one line whose cell attribute value is `FXGROUPID` instead of an AccountID (not US orgs or AU demo companies).

#### The row/cell response structure and how to parse it

Every row/cell-shaped report returns:

```jsonc
{
  "Reports": [{
    "ReportID": "ProfitAndLoss",          // or a GUID for published BAS/GST reports
    "ReportName": "Profit and Loss",
    "ReportType": "ProfitAndLoss",
    "ReportTitles": ["Profit & Loss", "Demo Company (AU)", "1 February 2018 to 28 February 2018"],
    "ReportDate": "25 February 2018",
    "UpdatedDateUTC": "/Date(1519593468971)/",
    "Rows": [
      { "RowType": "Header", "Cells": [{ "Value": "" }, { "Value": "28 Feb 18" }] },
      { "RowType": "Section", "Title": "Income", "Rows": [
        { "RowType": "Row", "Cells": [
          { "Value": "Sales",   "Attributes": [{ "Id": "account", "Value": "e2bacdc6-..." }] },
          { "Value": "9220.05", "Attributes": [{ "Id": "account", "Value": "e2bacdc6-..." }] }
        ]},
        { "RowType": "SummaryRow", "Cells": [{ "Value": "Total Income" }, { "Value": "9220.05" }] }
      ]}
    ]
  }]
}
```

Rules of thumb:

- The top-level `Rows` mixes exactly one `Header` row (column labels) with `Section` rows. Sections may have a `Title` (sometimes empty, e.g. the NET PROFIT section) and contain nested `Rows` of type `Row` (data) and `SummaryRow` (totals). Sections can also be title-only with no nested rows (Balance Sheet's top-level "Assets" section).
- Every cell is `{ "Value": ..., "Attributes": [...] }`. `Attributes` is optional; when present it carries machine-readable IDs, e.g. `{"Id": "account", "Value": "<AccountID>"}` on Trial Balance / P&L / Balance Sheet lines, or `{"Id": "invoiceID", ...}` on aged receivables/payables rows. Use attribute IDs, not display text, to join report lines back to Accounts or Invoices.
- Column position is meaningful: cell N of a data row lines up with cell N of the `Header` row. With `periods`/`timeframe` or tracking categories you simply get more columns.
- Values are strings; empty cells may omit `Value` entirely (Trial Balance emits attribute-only cells for blank debit/credit columns). Dates in `UpdatedDateUTC` use the .NET `/Date(ms)/` format.

Worked example: flatten a report into `(section, label, header, value, accountID)` tuples:

```python
def parse_report(payload):
    report = payload["Reports"][0]
    rows = report["Rows"]
    headers = next(
        [c.get("Value", "") for c in r.get("Cells", [])]
        for r in rows if r["RowType"] == "Header"
    )
    out = []
    for section in rows:
        if section["RowType"] != "Section":
            continue
        for row in section.get("Rows", []):
            if row["RowType"] not in ("Row", "SummaryRow"):
                continue
            cells = row["Cells"]
            label = cells[0].get("Value", "")
            attrs = {a["Id"]: a["Value"] for a in cells[0].get("Attributes", [])}
            for i, cell in enumerate(cells[1:], start=1):
                out.append({
                    "section": section.get("Title", ""),
                    "label": label,                      # e.g. "Sales" or "Total Income"
                    "is_summary": row["RowType"] == "SummaryRow",
                    "column": headers[i] if i < len(headers) else "",
                    "value": cell.get("Value"),          # string; may be None for blank cells
                    "account_id": attrs.get("account"),  # None on summary rows / FXGROUPID lines
                })
    return out
```

For group-entity review runs: call the same report per tenant (`xero-tenant-id` header), parse with the routine above, and compare `account_id`-keyed values against your criteria; fall back to `label` matching only for summary rows, which never carry attributes.

### 5.3 Attachments

`https://api.xero.com/api.xro/2.0/{Endpoint}/{Guid}/Attachments` with GET, PUT, POST.
Scopes: `accounting.attachments` or `accounting.attachments.read`.

**Endpoints supporting attachments**: Invoices, Receipts, Credit Notes, Repeating Invoices, Bank Transactions, Bank Transfers, Contacts, Accounts, Manual Journals, Purchase Orders, Quotes.

- **Upload**: `PUT` or `POST` to `.../{Endpoint}/{Guid}/Attachments/{Filename}` with the **raw file bytes as the body** (no JSON/XML wrapper) and the file's MIME type as `Content-Type`. The parent document must already exist. POST and PUT behave identically; re-sending to an existing filename **replaces** that attachment.
- **List**: `GET .../{Guid}/Attachments/` returns `AttachmentID`, `FileName`, `Url`, `MimeType`, `ContentLength` per attachment.
- **Download**: `GET .../{Guid}/Attachments/{Filename}` returns the raw content (no JSON envelope).
- **Limits**: **10 attachments per document**. Per-file size: the Attachments page says **10 MB** each, the Contacts and Invoices pages say up to **25 MB** each, and the Manual Journals and Accounts pages say **3 MB** each (Xero docs conflict, verify); treat the lowest figure quoted for the endpoint as the safe ceiling.
- **Filename rules**: names containing `< > : " / \ | ? * \0 +` are rejected with 400. Special characters should not be percent-encoded, except brackets which must be encoded.
- **`IncludeOnline=true`** (query parameter on upload): makes the attachment visible to the customer on the online invoice. Available for **accounts receivable invoices and AR credit notes** only; the response then echoes `"IncludeOnline": true`.

### 5.4 History and notes

`https://api.xero.com/api.xro/2.0/{Endpoint}/{Guid}/history` with GET, PUT, POST.

- **GET history** returns `HistoryRecords`: `Changes` (event type, e.g. Created/Approved/Edited), `DateUTC` (+ `DateUTCString`), `User` ("System Generated" for API-driven changes), `Details`.
- **PUT history** adds a note: body `{"HistoryRecords": [{"Details": "..."}]}`, max **2500 characters** per note. Notes are the only manually creatable change type; they show as "System Generated". POST is identical to PUT. History records and notes **cannot be updated or deleted**.
- **Supported document types** (same list for GET history and PUT notes): BankTransactions, BatchPayments, BankTransfers (via the BankTransactions endpoint), Contacts, CreditNotes, Invoices, Items, ManualJournals, Overpayments, Payments, Prepayments, PurchaseOrders, RepeatingInvoices, Quotes.

Useful pattern for an automation agent: after an automated review flags a document, `PUT .../history` a note recording the criteria result, so the audit trail is visible inside Xero.

### 5.5 Webhooks

Configured per app in My Apps on developer.xero.com (choose event categories + HTTPS delivery URL); events arrive for **every organisation connected to the app**. Use the `offline_access` scope so connections outlive 30 minutes. At least one org must be connected before payloads flow.

**Event categories** (current list, each with `CREATE` and `UPDATE` event types):

| eventCategory | Notes |
|---|---|
| `CONTACT` | UPDATE fires for archiving too |
| `INVOICE` | UPDATE fires for archiving too |
| `CREDITNOTE` | Payload includes a `data` object: `Type` (e.g. ACCPAYCREDIT), `Status` (e.g. DRAFT, SUBMITTED) |
| `PREPAYMENT` | Also includes a `data` object |
| `SUBSCRIPTION` | Xero App Store subscription created/upgraded/downgraded/cancelled/renewed |

**Payload shape** (JSON POST):

```json
{
  "events": [{
    "resourceUrl": "https://api.xero.com/api.xro/2.0/Invoices/717f2bfc-...",
    "resourceId": "717f2bfc-c6d4-41fd-b238-3f2f0c0cf777",
    "eventDateUtc": "2025-10-21T01:15:39.902",
    "eventType": "Update",
    "eventCategory": "INVOICE",
    "tenantId": "c2cc9b6e-9458-4c7d-93cc-f02b81b0594f",
    "tenantType": "ORGANISATION"
  }],
  "firstEventSequence": 1,
  "lastEventSequence": 1,
  "entropy": "S0m3r4Nd0mt3xt"
}
```

`tenantId` identifies the organisation the event belongs to (it matches the tenantId from `GET /connections`; see 4.7 on tenantId vs OrganisationID), which is how a multi-entity app routes an event to the right group entity. `entropy` is a random string added for cryptographic strength. A full contract lives in the `xero-webhooks.yaml` OpenAPI spec on GitHub (XeroAPI/Xero-OpenAPI).

**Signature verification**: every request carries an `x-xero-signature` header = Base64(HMAC-SHA256(raw request body, webhook signing key)). Compute over the exact raw bytes; respond **401 Unauthorized** for signatures that do not match, 2xx for ones that do.

**Endpoint requirements**: HTTPS on port 443, respond within **5 seconds** with a 2xx, no cookies in response headers, 401 on invalid signature.

**Intent to receive (ITR)**: on create, re-enable, or URL change, Xero sends a series of POSTs with an empty `events` array (sequences 0), some correctly signed and some deliberately mis-signed. Pass by returning 2xx to correctly signed and 401 to incorrectly signed payloads; validation can take up to 30 seconds. Status moves "Intent to receive required" -> "in progress" -> "OK".

**Retries**: on a failed delivery Xero retries immediately, then every **15 minutes**, with decreasing frequency for up to **24 hours**; after that the subscription is **Disabled** and collaborators are emailed. Events occurring while in Retry/Disabled are stored for up to **31 days** and replayed in order once healthy. Xero requires consumers to implement **idempotency** and tolerate replays.

### 5.6 Adjacent APIs (awareness only)

**Assets API**: a separate API for fixed assets at base URL `https://api.xero.com/assets.xro/1.0/` (endpoints: Assets, AssetTypes, Settings; GET/POST), JSON only, own scopes `assets` / `assets.read`, versioned independently of the Accounting API. Relevant if account reviews later need to reconcile fixed-asset registers against balance sheet accounts.

**Files API**: a separate API at `https://api.xero.com/files.xro/1.0/` (endpoints: Files, Folders, Associations, Inbox) with scopes `files` / `files.read`. It manages the org's file library: upload/download files, manage folders, and associate files with documents such as invoices and contacts. This is distinct from the Accounting API's per-document Attachments endpoint; use it if the project needs a shared document store rather than direct attachments.

**Payroll APIs**: three separate region-specific APIs (AU at `https://api.xero.com/payroll.xro/1.0/`, plus distinct UK and NZ APIs) with their own scope family (`payroll.employees`, `payroll.payruns`, `payroll.payslip`, `payroll.timesheets`, `payroll.settings`, each with `.read` variants). Authorisation additionally requires the connecting Xero user to be a payroll administrator, and all payroll endpoints are paginated at 100 records per page. Only needed if review criteria ever extend to payroll balances.
