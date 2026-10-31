# Health data contract: `wizard.health-data/1`

A **health-data connector** is an app that holds the owner's sign-in to one wearable or health service (WHOOP,
Oura, Garmin, Apple Health...) and offers that data, read-only and normalized, to other apps on the same box.
A **health front end** (the Health app) reads any number of connectors through this contract and never talks to a
provider itself. This mirrors the money stack: Money reads `wizard.bank-connection/1` from SimpleFIN, Monarch or
Teller; Health reads `wizard.health-data/1` from WHOOP and any later connector.

The same file lives in both repos (`wizard-whoop/docs/` and `wizard-health/docs/`). Change both together.

## Discovery

- A connector declares `provides: [health-data, <provider>-health-data]` in `wizard.md`.
- A front end declares `uses: [health-data]`. The App Manager reads that as the `health-data` spot, links the
  front end's network to **every** installed app that provides it, and writes the endpoint it granted into the
  front end's `/wizard/grants.json` (`provider_endpoints`, one per spot; newer App Managers also list every
  provider in `provider_endpoints_all`).
- The front end probes each candidate's `GET /api/status`. A reply with `"contract": "wizard.health-data/1"` is a
  source; the reply names itself (`source.id`, `source.name`, `source.app_path`), so a new connector needs no code
  change in the front end. Candidates come from, in order: `grants.json` `provider_endpoints_all` (every provider
  of the spot: `{"slot:health-data": {"<app id>": "http://<host>:<port>"}}`), `grants.json` `provider_endpoints`
  (one endpoint per spot, what older App Managers write), the `HEALTH_SOURCES` env (`id=http://host:port,...`),
  and the built-in list of known connectors with their ports (`wizard-whoop` 8824, `wizard-oura` 8828,
  `wizard-fitbit` 8830, `wizard-polar` 8832, `wizard-withings` 8834), as Money keeps for bank connections.
  An unreachable candidate is simply "not installed".
- Older App Managers write one endpoint per spot grant. When two connectors fill the spot, the front end still
  reaches both (both networks are linked) through the built-in list or `HEALTH_SOURCES`; App Managers with
  `provider_endpoints_all` need neither.

## Several sources (front end behaviour)

- The owner picks one **main source** when two or more are connected (saved by the front end, server-side). Until
  then the main source is the first connected one in this order: WHOOP, Oura, Fitbit, Polar, Withings, then any
  other.
- Per day the front end uses the main source's record. A metric the main source lacks that day (no `sleep`, no
  `readiness.score`, a `null` number) is taken whole from the first other source that has it, and the front end
  shows where it came from. A readiness the main source marks `pending` is not filled from elsewhere.
- A connector without a readiness score at all says so in its status: `scales.readiness` left out, `null`, or
  `{"label": ..., "available": false}` (a missing `available` means `true`), optionally also
  `"provides": {"readiness": false}`. Its days carry `"readiness": null`. The front end then says it has no score
  from that source (grey), may still show `hrv_ms` and `resting_hr_bpm`, and never derives a score from them.

## Transport and safety

- Plain HTTP on the connector's own port, JSON only, `GET` only for reads.
- Reads are **box-only**: the connector refuses contract reads that arrive through the private-access proxy (any
  `X-Forwarded-For`, `Forwarded`, `X-Real-IP`, `Tailscale-User-Login` or `CF-Connecting-IP` header). The front end
  calls the connector from its own container (`http://<connector-id>:<port>`), scripts call `127.0.0.1:<port>`.
- No reply ever contains keys, tokens, passwords or sign-in codes.
- Errors: `{"error": "<plain sentence>"}` with 400 (bad query), 403 (not box-only), 409 (not connected).

## `GET /api/status`

```json
{
  "contract": "wizard.health-data/1",
  "source": {"id": "wizard-whoop", "provider": "whoop", "name": "WHOOP", "app_path": "/wizard-whoop/"},
  "status": "connected",
  "account": {"name": "Sam"},
  "dataset": "4f0c9a1e2b7d...",
  "last_sync": "2026-10-31T01:02:09.000Z",
  "next_sync": "2026-10-31T01:22:09.000Z",
  "sync_every_minutes": 20,
  "history_from": "2026-03-26",
  "latest_date": "2026-10-31",
  "counts": {"days": 217, "sleeps": 254, "workouts": 122},
  "error": null,
  "scales": {
    "readiness": {"label": "Recovery", "min": 0, "max": 100},
    "sleep_score": {"label": "Sleep performance", "min": 0, "max": 100},
    "load": {"label": "Strain", "min": 0, "max": 21, "scale_max": 21}
  }
}
```

- `status`: `not_configured` (no keys/sign-in yet), `needs_login` (provider ended the sign-in), `syncing`,
  `connected`, `rate_limited` (provider asked to slow down; data still readable). Only `connected`, `syncing`
  and `rate_limited` mean data is flowing.
- `dataset`: an opaque id of the connector's local copy. It changes when the copy is wiped (Disconnect, a new
  account). A front end that sees a different `dataset` drops what it holds from that source and re-reads.
- `scales` tells the front end what to call things and how to draw them. A front end shows `load.label`
  (WHOOP "Strain", Garmin "Training load", Oura "Activity") and never assumes a provider's scale.
- `scales.load` (and every `load` in the days) may carry, additively:
  - `scale_max`: the top of the load scale the front end judges effort on (WHOOP `21`). Without it the front end
    uses `max`. For an open-ended scale (`max: null`, e.g. Active Zone Minutes) it is the top of the drawing scale.
    A front end that sets activity targets from the recovery band takes WHOOP's guidance as fractions of the scale
    (Ready 14/21-18/21, So-so 10/21-14/21, Run down under 10/21 of `scale_max`, "a bit much" up to 3/21 above), so
    WHOOP's own numbers stay 14-18, 10-14 and under 10.
  - `targets`: the provider's own target per readiness band, `{"high": [lo, hi], "medium": [lo, hi], "low": [0, hi]}`
    on the load's own scale. When present the front end uses these instead of the fractions.
  - `over_margin`: how far above a target's top is still "a bit much" (beyond it: "too much"), on the same scale.
    Without it: 3/21 of `scale_max`.
  - `unit`: the short unit of the value (`"min"`). A load with a numeric `max` reads "out of `max`"; an open-ended
    one reads as its `label` ("34 active zone minutes").
  A day's `load` may leave these out when `scales.load` has them; the front end takes them from `scales.load`.
  A load with no `scale_max`, no `max` and no `targets` gets no activity target (no colour), never a guessed one.
- A connector may add its own fields (the WHOOP app adds its setup state); front ends ignore unknown fields.

## `GET /api/days`

Normalized daily records, oldest first, with paging.

Query (all optional):

| name | meaning |
|---|---|
| `from`, `to` | `YYYY-MM-DD`, inclusive, on the record's `date` |
| `changed_since` | a `cursor` from an earlier reply; only days whose data changed after it. Empty string = everything |
| `limit` | days per page, default 100, max 500 |
| `page` | the `next_page` from the previous reply (keep the other query values the same) |

Reply:

```json
{
  "contract": "wizard.health-data/1",
  "source": "wizard-whoop",
  "dataset": "4f0c9a1e2b7d...",
  "days": [ /* day records */ ],
  "next_page": "eyJk...",
  "cursor": "w1.2840"
}
```

- `next_page` is `null` on the last page.
- `cursor` is the connector's change mark at the time of the **first** page of this read (every page repeats
  it). Store it only after the last page, then pass it as `changed_since` next time. Cursors are opaque strings.
- A day is returned again whenever anything in it changes (late scoring, a re-scored sleep, a new workout).
  The day record is always complete, so the front end replaces its copy of that `(source, date)`.

### Day record

```json
{
  "date": "2026-10-30",
  "updated_at": "2026-10-30T23:10:41.000Z",
  "readiness": {"score": 72, "band": "high", "state": "scored", "calibrating": false},
  "hrv_ms": 64.2,
  "resting_hr_bpm": 52,
  "spo2_pct": 96.1,
  "skin_temp_c": 33.6,
  "skin_temp_delta_c": null,
  "respiratory_rate": 15.4,
  "sleep": {
    "start": "2026-10-29T16:12:03.000Z", "end": "2026-10-30T00:01:10.000Z",
    "asleep_h": 7.02, "in_bed_h": 7.82, "awake_h": 0.8,
    "light_h": 3.6, "deep_h": 1.7, "rem_h": 1.72,
    "score": 81, "efficiency_pct": 90, "respiratory_rate": 15.4, "need_h": 8.4,
    "naps": [ {"start": "...", "end": "...", "asleep_h": 0.4} ]
  },
  "load": {"value": 12.4, "max": 21, "scale_max": 21, "label": "Strain"},
  "calories_kcal": 2410,
  "energy_kj": 10083,
  "avg_hr_bpm": 68,
  "max_hr_bpm": 171,
  "steps": null,
  "workouts": [
    {"id": "b2f...", "sport": "Running", "start": "...", "end": "...", "minutes": 42,
     "load": {"value": 9.8, "max": 21, "scale_max": 21, "label": "Strain"},
     "avg_hr_bpm": 148, "max_hr_bpm": 176, "calories_kcal": 512, "energy_kj": 2142, "distance_m": 7210,
     "zones_min": [0.5, 3.2, 10.1, 18.4, 9.1, 0.7]}
  ],
  "extras": {"whoop": {"cycle_id": "1234", "sleep_consistency_pct": 77, "disturbances": 9}}
}
```

Rules:

- `date` is the owner's local calendar day the record belongs to (the day he woke up / lived through), as the
  provider defines it.
- Any value the provider did not give is `null`; a section with nothing at all is `null` (`readiness`, `sleep`,
  `load`). Never invent a value.
- `readiness.score` is 0-100. `readiness.band`: `high` (67-100), `medium` (34-66), `low` (0-33) unless the
  provider has its own bands, in which case the provider's band maps onto these three. `readiness.state`:
  `scored`, `pending` (not scored yet), `unscorable`.
- `sleep` is the main sleep that ended on `date`; `sleep.naps` lists shorter sleeps. Hours are decimal hours;
  `asleep_h` excludes awake time. `sleep.score` is the provider's sleep score or performance, 0-100.
- `load` is the day's effort on the provider's own scale; `load.max`, `load.label` and the optional `scale_max`,
  `targets` and `unit` repeat `scales.load`.
- `calories_kcal` is the day's total energy; `energy_kj` is the same in kilojoules when the provider reports kJ.
- `workouts[].zones_min` is minutes in heart-rate zones 0-5 (or `null`).
- `extras` holds provider-specific fields under the provider's own key (`extras.whoop`, `extras.oura`...). Front
  ends may show them but never depend on them.

## Provider mappings

Additive notes: each connector documents here how it maps its provider onto this contract.

### Polar (`wizard-polar`, AccessLink v3)

- `date`: Polar's own result date (the morning the night ended) for sleep and Nightly Recharge; the device's local
  calendar day for daily activity, Cardio Load and exercises.
- `readiness` from Nightly Recharge, formula `polar-nightly-recharge/1`. Polar's six statuses keep their meaning
  and line up with the contract bands: very poor 0-16 and poor 17-33 (`low`), compromised 34-50 and OK 51-66
  (`medium`), good 67-83 and very good 84-100 (`high`). ANS charge (-10 to +10, 0 = the owner's usual) places the
  night inside its status's slice: `score = lo + (hi - lo) * (clamp(ans_charge, -10, 10) + 10) / 20`, the slice's
  middle when ANS charge is missing. So Health's 67/34 bands always agree with Polar's status.
- `hrv_ms` = Nightly Recharge `heart_rate_variability_avg` (RMSSD, first ~4 h of sleep); `resting_hr_bpm` =
  `heart_rate_avg` over the same window; `respiratory_rate` = `breathing_rate_avg`.
- `sleep`: stages from Polar seconds; `asleep_h` = light + deep + REM + unrecognised; `awake_h` =
  `total_interruption_duration`; `in_bed_h` = end - start; `score` = `sleep_score` (1-100); `need_h` = the owner's
  Polar sleep goal (`sleep_goal`), so Health's percent-of-need works.
- `load` from Cardio Load (TRIMP), formula `polar-trimp/1`: `value = round(21 * (1 - e^(-TRIMP / 100)), 1)`,
  `max = scale_max = 21`, `native` = the TRIMP. Anchors: 50 -> 8.3, 100 -> 13.3 (a typical hard hour; Polar puts a
  60-minute session at 70-130), 150 -> 16.3, 200 -> 18.2, 300 -> 20.0. Days Polar could not calculate
  (`LOAD_STATUS_NOT_AVAILABLE`, no load) are `null`; without a Cardio Load row the day's exercises' cardio loads
  are added up. Workouts use the same formula on each session's Training Load Pro cardio load.
- `calories_kcal`, `steps` from daily activity; `workouts[].zones_min` = `[null, z1..z5]` (Polar has no zone 0).
- `extras.polar`: Nightly Recharge status and word, ANS charge, sleep charge, continuity, Cardio Load status,
  7-day strain, 28-day tolerance, ratio and level thresholds, active minutes, daily activity %.

## MCP (connector side)

Connectors expose the same narrow tools so the agent can check them without the front end:
`health_data_status` (the status above), `health_data_sync` (ask for fresh data now) and `health_data_days`
(the `/api/days` read with the same arguments). Owner-facing questions ("how did I sleep?") go to the front end's
tools, not the connector's.

## Versioning

Additive fields keep `wizard.health-data/1`. Renaming or removing a field, or changing a meaning, is
`wizard.health-data/2`; a front end that sees a contract it does not know marks the source "needs an update".
