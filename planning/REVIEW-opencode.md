# Review of PLAN.md

## Overall

The plan is thorough, well-structured, and makes sensible engineering trade-offs for a single-container, single-user capstone project. The architecture choices (SSE, SQLite, static Next.js export, uv) are well-reasoned and clearly documented. Below are actionable observations.

---

## Clarifications Needed

### 1. LLM Architecture (LiteLLM + OpenCode + Cerebras)
Section 9 says: "use cerebras skill to use LiteLLM via OpenCode to the `opencode/deepseek-v4-flash-free` model with Cerebras as the inference provider."

This conflates three layers. Which path is correct?
- `LiteLLM → Cerebras API directly`
- `LiteLLM → OpenCode Go (as gateway) → Cerebras API`
- `OpenCode Go → Cerebras API` (with LiteLLM only used as a client library)

The cerebras skill description says "calling an LLM using LiteLLM and OpenCode Go with the Cerebras inference provider." Clarify the exact call chain so agents can implement consistently.
R: LiteLLM is a client library that calls OpenCode Go, through Cerebras.

Also, `opencode/deepseek-v4-flash-free` looks like an OpenCode-hosted model identifier — is this a model name that OpenCode Go routes to Cerebras, or a model hosted by OpenCode itself?
R: it's a model hosted by OpenCode. Cerebras is used to call opencode.

### 2. SSE Push Cadence When Using Massive API
Section 6 lists SSE push cadence at **500ms** regardless of data source. But the Massive free tier polls every **15s**. If the SSE stream pushes every 500ms but prices only change every 15s, the frontend receives 30 identical price events per update — wasteful. Either:
- Push only when the cached price changes, or
- Clarify that the 500ms cadence is simulator-only, and the SSE adapts to the source refresh rate.
R: let's clarify that the 500ms cadence is simulator-only, and the SSE adapts to the source refresh rate.

### 3. Massive API — What Is This?
Section 5's `.env.example` comment says `# Massive (Polygon.io) API key`. Is Massive a custom name for a Polygon.io wrapper, a separate service, or a typo? If it's a custom wrapper, document the expectation. If it's Polygon.io, say that explicitly everywhere.
R: it's a custom wrapper for Polygon.io. Let's clarify that in the plan.
---

## Missing Details

### 4. No Consistent API Response Envelope
The plan lists endpoints and their methods but never defines a standard response format. Every API should return a consistent envelope:
```json
{"success": true, "data": { ... }}
{"success": false, "error": "message", "code": "ERROR_CODE"}
```
Without this, frontend error handling will be ad-hoc and inconsistent.
R: suggest a consistent response envelope in the plan.

### 5. Partial Trade Execution
Section 9 says trades auto-execute and "go through the same validation as manual trades." If the LLM requests 3 trades and only 2 succeed, what happens? Execute the valid ones and report the failure, or reject the whole batch? Specify the semantics.
R: let's reject the batch.

### 6. No Rate Limiting / Abuse Protection
The trade and chat endpoints have no mention of rate limiting. For a course project this may be fine, but it should be a deliberate decision noted in the plan so an agent doesn't add it unnecessarily later.
R: it's a MVP, so no rate limiting is fine, but note that in the plan.

### 7. No SSE Reconnection Test
Section 12 lists E2E scenarios but doesn't include "SSE disconnects and reconnects" as a test case, despite it being called out in Section 2 as a connection status indicator feature.
R: include a test for SSE reconnection in the E2E plan.

### 8. No Input Validation for Watchlist Add
`POST /api/watchlist` accepts any ticker string. Should it validate against known symbols? The plan doesn't specify whether the simulator/Massive can handle arbitrary tickers or only known ones.
R: let's validate against known symbols and return a 400 error for unknown tickers, or clarify that the simulator can handle arbitrary strings.

---

## Minor Issues

### 9. `portfolio_snapshots` 30s interval + trade execution writes
The plan says snapshots are recorded "every 30 seconds" and "immediately after each trade." If trades happen rapidly, the immediate snapshot + the 30s tick will create very uneven time spacing. Ensure the P&L chart handles irregular intervals gracefully.
R: not sure about it, please clarify

### 10. No Fallback for Slow LLM Inference
Section 9 relies on Cerebras being "fast enough" to skip streaming. If inference takes >5s on a complex query, the user sees a spinner with no progress. Consider adding a note that if latency becomes an issue, a token-streaming fallback can be added later.
R: since it's a demo, let's note that a streaming fallback can be added later if latency becomes an issue. A message like "LLM is thinking..." can be shown in the meantime.

### 11. Volume Mount Path
Line 413 says `db/` → `/app/db`, but the backend config (Section 7) says the database lives at `db/finally.db`. Ensure the Dockerfile and backend config agree on this path, and document it in one place (currently in Section 4 and Section 11, but could be consolidated).
R: let's ensure the Dockerfile and backend config agree on the path, and document it in one place.

### 12. No SQL Injection Mention
SQLite queries should use parameterized statements. The plan doesn't mention this, but it's worth a note given the "production-quality" goal.
R: let's add a note about using parameterized statements to prevent SQL injection.
---

## Strengths

- **Clear single-container architecture**: One port, no CORS, no docker-compose dependency — excellent for students.
- **Multi-user-ready schema**: All tables carry `user_id`; hardcoded to "default" for now but no migration needed later.
- **Simulator quality**: Correlated GBM with configurable beta, random events, realistic seed prices — impressive for a demo.
- **Auto-execution rationale**: Well-explained (fake money, demo experience, agentic demo).
- **Testing strategy**: Separate docker-compose for E2E, mock LLM mode, fresh ephemeral volumes per test run — all solid practices.
- **Directory structure**: Clean boundaries between frontend, backend, planning, tests, scripts.
