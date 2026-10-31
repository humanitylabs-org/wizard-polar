# Polar (Wizard connector)

A Wizard OS connector app for one Polar Flow account. It holds the Polar sign-in, keeps a private synced copy of Nightly Recharge, sleep, daily activity, Cardio Load and training sessions, and offers that copy read-only to other apps on the same box through the `health-data` spot, contract [`wizard.health-data/1`](docs/health-data-contract.md). The [Health](https://github.com/oscarhenrycollins/wizard-health) app is the screen for the data; this app is only the connection.

**Needs the owner's own developer credentials**: a free Polar AccessLink API client created with the owner's Polar Flow account. Nothing is shared between boxes.

## Setup (all on the app's own screen)

1. A Polar Flow account (free; no membership). Nightly Recharge and sleep stages need a Polar watch that measures them.
2. Sign in at https://admin.polaraccesslink.com with Polar Flow and tap Create client.
3. Fill in the client with the values the screen shows (application name, description, Authorization redirect URL; tick the three data types). Each has a Copy button. Polar shows the Client ID and Client Secret once.
4. Paste Client ID and Client Secret (stored server-side, never echoed). The app tests them by exchanging a made-up code at Polar's token endpoint with Basic auth (`invalid_grant` = keys valid, `unauthorized_client`/`invalid_client` = wrong).
5. Connect with Polar: Polar Flow's consent page opens in a new tab of the owner's own browser. After Accept, the browser lands on `http://localhost:3000/callback?code=...` (which doesn't load); the owner pastes that address back within 10 minutes and the app finishes the sign-in, then registers the user with AccessLink (`POST /v3/users`, once). If Polar answers 403 the owner accepts Polar's consents at account.polar.com and connects again.

The Redirect URL defaults to `http://localhost:3000/callback` because it works from any browser. Over https the screen also offers the box's own address (`https://<box>/wizard-polar/callback`), where the app finishes the sign-in by itself.

Polar user tokens do not expire and AccessLink issues no refresh tokens; a 401 means the owner or Polar ended the access, and the app asks to connect again.

## Data and sync

- Backfill after sign-in, then every 20 minutes with a 4-day overlap. Polar shares about the last 28 days of nights (`/v3/users/sleep`) and Nightly Recharge, up to 365 days of daily activity (`/v3/users/activities`) and Cardio Load (`/v3/users/cardio-load/date`, both read in 28-day windows), and the last 30 days of exercises (`/v3/exercises?zones=true`).
- 429 answers are retried after `RateLimit-Reset`; a long reset pauses syncing for 15 minutes.
- Readiness and load mappings (documented formulas `polar-nightly-recharge/1` and `polar-trimp/1`) are in `docs/health-data-contract.md` under "Provider mappings".

## Interfaces

- Screen: `/wizard-polar/` (status, connected account, Sync now, Disconnect, guided setup).
- Contract (box-only: refused when it arrives through the private-access proxy): `GET /api/status`, `GET /api/days?from&to&limit&page&changed_since`.
- Setup API used by the screen: `GET /api/setup`, `POST /api/keys`, `/api/keys/test`, `/api/keys/forget`, `/api/oauth/start`, `/api/oauth/finish`, `/api/sync`, `/api/disconnect`; `GET /callback` for the https Redirect URL.
- MCP (`POST /mcp`, box-only): `health_data_status`, `health_data_sync`, `health_data_days`.

## Development

`python3 -m unittest discover -s tests` runs the tests against a fake AccessLink built from Polar's published v3 documentation (sample payloads copied from its example responses). No live Polar account was used to build this app. `python3 tools/package.py` refreshes the inline Runtipi payload; `python3 tools/package.py --check` verifies it.
