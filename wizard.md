---
wizard: 0.2
id: wizard-polar
name: Polar
type: external
category: system
version: 0.1.2
summary: Connect your Polar. Health and your other apps get read-only Nightly Recharge, sleep, activity, Cardio Load and training sessions.
tags: [data]
license: GPL-3.0-only
icon: icon.png
source: https://github.com/oscarhenrycollins/wizard-polar
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 8832
  path: /api
  spec: openapi.json
mcp:
  path: /mcp
  port: 8832
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 8832
  source:
    path: web
    build: python3 app/build_view.py
    dist: web
provides: [health-data, polar-health-data]
system: true
uses: []
needs: []
health: /healthz
switches:
  contract: wizard.app-switches/1
  path: /api/switches
---

# Polar

Connects one Polar Flow account to your Wizard. You need your own free Polar AccessLink API client (your own developer credentials): the app walks you through creating it on its own screen. You create the client on Polar's AccessLink admin page, paste its Client ID and Client Secret here, then sign in to Polar Flow once in your own browser and paste the address it lands on back into the app. The app then registers your account with your client, as Polar requires. The keys and sign-in stay inside this app and are never shown again.

After sign-in the app downloads what Polar shares (about the last 28 days of sleep and Nightly Recharge, up to a year of daily activity and Cardio Load, the last 30 days of training sessions), then checks for new data every 20 minutes. It keeps the copy here and offers it, read-only, to other apps on this box through the `health-data` spot (contract `wizard.health-data/1`, see `docs/health-data-contract.md`). Nightly Recharge becomes a 0-100 readiness score and Cardio Load a 0-21 activity value with documented formulas, so Health can colour them like any other source.

Disconnect unlinks your account from your client and deletes the local copy.
