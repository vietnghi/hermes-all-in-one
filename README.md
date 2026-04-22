# Hermes Railway Control Plane

This repository is the next evolution of the all-in-one Hermes Railway template: one shared Hermes runtime, Hermes WebUI at `/`, and a dedicated admin/control plane at `/admin`.

It keeps the vendor boundary clean:
- `vendor/hermes-agent`
- `vendor/hermes-webui`
- root-level orchestration and UX in `control_plane/`

The goal is boring, stable hosting:
- one persistent volume at `/data`
- one canonical Hermes home at `/data/.hermes`
- one shared agent identity across Telegram + WebUI
- pinned vendored upstreams instead of floating latest at runtime
- API-key-first onboarding
- honest advanced/manual handling for fragile OAuth-style provider flows

## Runtime shape

Public origin:
- `/` → Hermes WebUI
- `/admin` → control-plane wrapper
- `/health` → wrapper health endpoint used by Railway

Internal process tree:
- public Starlette wrapper (PID 1)
- internal Hermes WebUI process bound to loopback
- optional Hermes gateway process for Telegram / messaging surfaces

This avoids exposing multiple unrelated public services while keeping WebUI and admin clearly separated.

## Shared state contract

Persistent volume root:
- `/data`

Shared Hermes runtime:
- `HERMES_HOME=/data/.hermes`
- `HERMES_CONFIG_PATH=/data/.hermes/config.yaml`

WebUI state:
- `HERMES_WEBUI_STATE_DIR=/data/webui`

Workspace:
- `HERMES_WORKSPACE_DIR=/data/workspace`

Operational rule: back up the entire `/data` volume, not just `/data/.hermes`.

## Auth model

The first pass intentionally keeps auth separate per surface:
- WebUI uses `HERMES_WEBUI_PASSWORD`
- `/admin` uses `HERMES_ADMIN_PASSWORD`
- if `HERMES_ADMIN_PASSWORD` is unset, the wrapper falls back to `HERMES_WEBUI_PASSWORD`

This is deliberate. Shared SSO sounds nice, but it adds fragility without helping the core deployment goal.

## Provider onboarding

Happy path:
- OpenRouter API key
- Anthropic API key
- OpenAI API key
- custom OpenAI-compatible endpoint with API key + base URL

Advanced/manual path:
- OpenAI Codex / ChatGPT-style hosted login
- other OAuth-first or terminal-first provider flows

Those advanced flows are not treated as reliable in-browser setup on Railway. The UI should say so clearly instead of pretending otherwise.

## Channel model

Telegram and WebUI are meant to be the same Hermes identity.

That means:
- same `HERMES_HOME`
- same config and `.env`
- same memory, sessions, skills, and runtime state
- different frontends over one shared backend identity

The wrapper will autostart the gateway only when both of these are true:
- a provider is configured
- at least one supported messaging channel has credentials configured

Manual start/stop/restart controls remain available at `/admin`.

## Deploy on Railway

1. Create a Railway service from this repo.
2. Attach a persistent volume mounted at `/data`.
3. Set at least these secrets before exposing the app publicly:
   - `HERMES_WEBUI_PASSWORD`
   - `HERMES_ADMIN_PASSWORD` (recommended)
4. Deploy.
5. Confirm Railway uses `/health` for the health check.
6. Open:
   - WebUI: `/`
   - admin: `/admin`

## Environment contract

See `.env.example` for the exact values.

Key variables:
- `HERMES_HOME=/data/.hermes`
- `HERMES_CONFIG_PATH=/data/.hermes/config.yaml`
- `HERMES_WEBUI_STATE_DIR=/data/webui`
- `HERMES_WEBUI_AGENT_DIR=/app/vendor/hermes-agent`
- `HERMES_WORKSPACE_DIR=/data/workspace`
- `HERMES_WEBUI_PASSWORD`
- `HERMES_ADMIN_PASSWORD`
- `HERMES_GATEWAY_AUTOSTART=auto`
- `CONTROL_PLANE_INTERNAL_WEBUI_HOST=127.0.0.1`
- `CONTROL_PLANE_INTERNAL_WEBUI_PORT=8788`

## Update strategy

Both upstream projects stay vendored with `git subtree`.

Normal maintainer flow:

```bash
./scripts/sync-upstreams.sh
./scripts/smoke.sh
```

If a vendored edit ever becomes unavoidable, log it in `docs/vendor-patches.md`.

## Boundary rules

- Prefer root-level wrappers over edits inside `vendor/`
- Treat unexpected vendored edits as bugs
- Keep the gateway an internal/backend concern
- Keep `/` as WebUI and `/admin` as the control-plane surface
- Do not turn this repo into a permanent fork of Hermes Agent or Hermes WebUI
