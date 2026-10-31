---
name: wizard-polar
description: Use when the owner asks about his Polar connection, Polar setup or sync. Health questions go to Health.
version: 0.1.1
author: Humanity Labs
license: GPL-3.0-only
metadata:
  hermes:
    tags: [polar, health-data, connector]
    related_skills: [wizard-health]
---
# Polar (connector)

The box's Polar connection (`health-data` spot, contract `wizard.health-data/1`). It holds the Polar sign-in and a synced copy; the **Health** app (`/wizard-health/`) shows the data and answers health questions. Tools: `mcp__wizard__wizard_polar__<tool>`.

- Questions about readiness, Nightly Recharge, sleep, HRV, activity, Cardio Load, workouts or trends: use the Health app's tools, not these.
- "Is my Polar connected / when did it last sync": `health_data_status`. `connected`, `syncing` or `rate_limited` = data is flowing; `not_configured` or `needs_login` = the owner must finish or redo setup (its `error` says why, e.g. Polar consents to accept at account.polar.com).
- Setup, reconnecting or replacing keys: send the owner the app link `[Polar](/wizard-polar/)` and say its screen walks him through it (five steps: Polar Flow account, AccessLink admin, create a client with the copy buttons, paste Client ID and Secret, Connect). It needs his own free AccessLink API client. Never walk him through Polar's admin page in chat, and never ask for or accept a Client ID, Client Secret, password, sign-in code or the localhost callback address in chat; those go only into the app's own screen.
- `health_data_sync` asks for fresh Polar data now (the app also syncs every 20 minutes); it returns at once.
- `health_data_days` is the raw contract read. Readiness is Nightly Recharge mapped to 0-100 (status bands kept); load is Cardio Load on a 0-21 equivalent with the TRIMP in `load.native`. Use it only to check what the connector holds.
- Scripts on the box can read the same contract at `http://127.0.0.1:8832/api/status` and `/api/days`.
