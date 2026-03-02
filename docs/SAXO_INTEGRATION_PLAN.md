# Saxo Bank OpenAPI — Integration Plan

Reference: [developer.saxo/openapi/referencedocs](https://www.developer.saxo/openapi/referencedocs)

Service groups in **business dependency order**: you need onboarded clients before they have accounts to trade or view portfolio — so **Client Management (onboarding) comes first**, then Trading + Portfolio (per user).

---

## Phase 0 — Foundation

**Goal:** Auth + config + one successful REST call (SIM).

| Task | Details |
|------|--------|
| Config | Add `saxo_*` in `nexow/config.py`: base URL (SIM/LIVE), app key, app secret, auth base URL. |
| Auth module | Implement token flow (e.g. Authorization Code or PKCE). Store access + refresh; refresh before expiry. Never expose app secret to frontend. |
| Root / session | Call Root Services (e.g. `/port/v1/users/me` or session) with Bearer token to validate setup. |
| Env | `.env.sample` + docs for `SAXO_APP_KEY`, `SAXO_APP_SECRET`, `SAXO_BASE_URL` (sim vs live). |

**Deliverable:** `nexow/broker/saxo.py` (or `nexow/broker/saxo/auth.py` + thin client) that can get a token and call one port/session endpoint.

---

## Phase 1 — Client onboarding (IB)

**Goal:** Onboard new clients onto Saxo first — trading and portfolio are **per user**, so clients must exist before they have accounts to trade or view.

| Service group | Use | Priority |
|---------------|-----|----------|
| **Client Management (CM)** | Signups, KYC docs, status, verification, onboarding PDF. | P0 |

**Endpoints to implement:**

- `POST /cm/v1/signups` — create signup (get ClientId, ClientKey, SignupId).
- `POST /cm/v1/signups/attachments/{SignUpId}` — upload documents (ID, residency, source of funds, etc.).
- `PUT /cm/v1/signups/completeapplication/{SignUpId}` — submit application.
- `GET /cm/v1/signups/status/{ClientKey}` — onboarding status.
- `POST /cm/v1/signups/verification/initiate/{ClientKey}` — start verification (e.g. redirect to vendor).
- `GET /cm/v1/signups/onboardingpdf/{ClientKey}` — generate onboarding PDF.
- `GET /cm/v1/signups/options` — dropdowns/options for forms.

**Tasks:**

- Backend: CM client in `nexow/broker/saxo.py` (or `saxo/cm.py`) calling these endpoints with the same token/session as Phase 0.
- API routes: e.g. `POST /onboarding/signup`, `POST /onboarding/signup/:id/documents`, `GET /onboarding/status/:clientKey`, etc.
- Frontend: onboarding wizard (identity, residency, documents, submit) and status page.

**Deliverable:** End-to-end onboarding flow: create signup → upload docs → complete → check status → optional verification + PDF.

---

## Phase 2 — Core trading (read + trade)

**Goal:** Once clients are onboarded, they (or you with their session) use instruments, portfolio, and orders. All of this is **per user** (per onboarded client).

| Service group | Use | Priority |
|---------------|-----|----------|
| **Reference Data** | Instruments, exchanges, search — drive symbol picker and order forms. | P0 |
| **Portfolio** | Balances, positions, margin — per client/account; dashboard and risk. | P0 |
| **Trading** | Prices (REST + optional streaming), place/cancel/amend orders, positions — per client. | P0 |

**Tasks:**

- Reference Data: list instruments (and exchanges if needed), optional search by text.
- Portfolio: balances and positions per client/account; map to your existing portfolio concepts.
- Trading: get quote/price for an instrument; place order (market/limit, etc.); list open orders; optional streaming prices (WebSocket) for live UI. Use the **client’s** token/session for Portfolio and Trading.

**Deliverable:** Saxo client methods used by API routes (e.g. `/markets`, `/portfolio`, `/orders`) so the app can show Saxo instruments and, for each onboarded user, their balances, positions, and orders.

---

## Phase 3 — History and reporting

**Goal:** Client-facing history and reports.

| Service group | Use | Priority |
|---------------|-----|----------|
| **Account History** | Historical and performance data for clients/accounts. | P1 |
| **Client Reporting** | Reports in .pdf/.xls. | P1 |
| **Client Services** | Reports, subscriptions, Mifid, fund transfers. | P1 |

**Tasks:**

- Account History: endpoints for history/performance; feed into your analytics or dashboards.
- Client Reporting: call report endpoints; link or download PDF/Excel for clients.
- Client Services: only what you need (e.g. fund transfer, Mifid, subscriptions).

**Deliverable:** Users can see history and download official reports from Saxo.

---

## Phase 4 — Optional / later

| Service group | Use |
|---------------|-----|
| **Chart** | Chart data for instruments (if you replace or complement existing chart provider). |
| **Streaming** | WebSocket subscriptions for prices, orders, positions (lower latency, less polling). |
| **ENS** | Event Notification Service — subscriptions for client activities (optional). |
| **Market Overview** | Market movers (winners/losers) for discovery. |
| **Regulatory Services** | Regulatory info per client. |
| **Disclaimer Management** | Legal text / disclaimers. |
| **Partner Integration** | If Saxo offers partner-specific endpoints. |
| **Value Add** | Extra value-added endpoints. |

**Defer:** Asset Transfers (BETA, select partners), Corporate Actions (special licensing).

---

## Implementation notes

- **Environments:** SIM: `https://gateway.saxobank.com/sim/openapi`, auth `https://sim.logonvalidation.net`. LIVE: `https://gateway.saxobank.com/openapi`, auth `https://live.logonvalidation.net`. Different app key/secret per env.
- **Auth:** Keep app secret on server; use Authorization Code (web) or PKCE (native). Support refresh token for long-lived sessions.
- **Structure:** Mirror existing brokers: `nexow/broker/saxo.py` (or `saxo/` package), `nexow/config.py` saxo section, and dedicated API routes that call the Saxo client.
- **Reference:** [Open API Learn](https://www.developer.saxo/openapi/learn), [Security](https://www.developer.saxo/openapi/learn/security), [Environments](https://www.developer.saxo/openapi/learn/environments).

---

## Getting stock and non-FX data on SIM

On **SIM**, Saxo only exposes **FxSpot** (forex) prices by default. Chart, infoprices, and instrument details for **stocks** (and other non-FX instruments) return **403 Forbidden** until you link your SIM account to a **Live** account.

To get stock (and other) data on SIM:

1. Go to [developer.saxo → Open API → Apps](https://www.developer.saxo/openapi/appmanagement#/).
2. Click **Live apps**.
3. Sign in with the **Live** account you want to link (it must be **funded**; otherwise login fails).
4. After linking, your SIM app gets **delayed** market data for all products that Live account has (including stocks).

Once linked, the same SIM token can be used for charts, quotes, and instrument details for stocks; no code changes needed.

- [Saxo help: Connect Live to SIM](https://openapi.help.saxo/hc/en-us/articles/4416934146449-How-do-I-connect-a-Live-account-to-a-SIM-Demo-account)

---

## Testing the callback on localhost

Saxo redirects the user to the **Redirect URL** you registered for your app. For local testing you have two options.

### Option A: Second redirect URL (if Saxo allows multiple)

If the Saxo Developer Portal lets you add more than one Redirect URL for your app:

1. Add **`http://localhost:8000/saxo/auth/callback`** as a redirect URL (keep production URL too if needed).
2. In **nexow-server** `.env`:
   ```bash
   SAXO_REDIRECT_URI=http://localhost:8000/saxo/auth/callback
   FRONTEND_URL=http://localhost:3000
   ```
3. Run backend on port 8000, frontend (nexow-app) on port 3000.
4. In the app, open **Saxo Connect** → **Connect to Saxo**. You go to Saxo, sign in, and Saxo redirects to `http://localhost:8000/saxo/auth/callback?code=...&state=...`. The backend exchanges the code and redirects you to `http://localhost:3000/saxo?state=...&saxo=ok`.

### Option B: Tunnel (when only one redirect URL is allowed)

If your Saxo app has a single Redirect URL (e.g. production), expose your local backend with a tunnel:

1. Install [ngrok](https://ngrok.com/) and run: `ngrok http 8000`. Note the HTTPS URL (e.g. `https://abc123.ngrok.io`).
2. In the Saxo app, set Redirect URL to **`https://abc123.ngrok.io/saxo/auth/callback`** (or add it if multiple are allowed). For local-only testing you can temporarily use only this URL.
3. In **nexow-server** `.env`:
   ```bash
   SAXO_REDIRECT_URI=https://abc123.ngrok.io/saxo/auth/callback
   FRONTEND_URL=http://localhost:3000
   ```
4. Run backend on 8000, frontend on 3000. Click **Connect to Saxo** in the app. Saxo redirects to the ngrok URL → your local backend → redirect to `http://localhost:3000/saxo?state=...&saxo=ok`.

**Important:** The `redirect_uri` sent in the token exchange must match the Redirect URL configured in Saxo exactly (including scheme and path). Use the same value in `SAXO_REDIRECT_URI`.

---

## Checklist summary

- [ ] Phase 0: Config, auth, token, one Root/Port call.
- [ ] Phase 1: Client Management (signup, docs, complete, status, verification, PDF, options) — onboard clients first.
- [ ] Phase 2: Reference Data + Portfolio + Trading (instruments, balances, positions, orders) — per onboarded user.
- [ ] Phase 3: Account History + Client Reporting + Client Services (as needed).
- [ ] Phase 4: Chart, streaming, ENS, others as needed.
