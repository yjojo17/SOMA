# Instagram Feed Audit

Sock-puppet audit of Instagram's recommendation feed, built for a master's thesis
at the Hasso-Plattner-Institut. The system runs a fleet of controlled sock-puppets,
that scroll the Instagram timeline like a human would, captures the underlying API
responses from inside the browser, classifies each post against a set of content
buckets with CLIP, and writes everything to TimescaleDB for statistical analysis.

> **This repository is research tooling, not a product.** Read
> [Legal and ethical scope](#legal-and-ethical-scope) before running anything.

---

## How it works

An orchestrator schedules sessions across matched groups of accounts. Each session
launches a real Firefox profile, opens the timeline, and scrolls under a behaviour
model that varies dwell time, scroll distance, mouse movement and back-scrolls.

Response capture happens **inside the browser**, via a WebExtension using
`StreamFilter`. This is deliberate: routing through a MITM proxy such as
selenium-wire terminates TLS locally, so the handshake Instagram observes is the
proxy's rather than Firefox's — a fingerprint mismatch that fires before any page
script runs. The extension relays captured bodies through a DOM node that the
collector drains synchronously.

Captured posts are scored inline by CLIP (ViT-B/32) against the bucket definitions
in the config. Classification is by softmax over the full candidate set. Captions
are fused into the query at a configurable weight, since topical buckets like News
and Business carry their signal in text rather than image.

### Components

| File | Role |
| --- | --- |
| `orchestrator.py` | Daily scheduler. Groups accounts by matched follow-set, assigns per-day session counts and shared off-days, persists state so restarts reproduce the schedule. |
| `configurable_collector.py` | Session driver. Browser lifecycle, scroll loop, interaction decisions, persistence. |
| `human_behavior.py` | Timing and movement distributions for scrolls, pauses, dwell. |
| `extension_interceptor.py` | Drains the in-browser capture relay. |
| `seleniumwire_interceptor.py` | Response parsing, shared by both capture paths. Legacy proxy ingestion. |
| `capture_addon.py` | Builds and temporarily installs the capture WebExtension. |
| `clip_classifier.py` | Zero-shot post classification against bucket definitions. |
| `action_logger.py` | Structured event log for every scroll, pause, like, follow, view. |
| `database_manager.py` | Synchronous psycopg writer. One connection per session. |
| `raw_archive.py` | Per-session gzipped JSONL archive of captured responses. |
| `schema.sql` | TimescaleDB schema. |
| `docker-compose.yml` | TimescaleDB, Prometheus, Pushgateway, Grafana. |

---

## Requirements

- Linux with Firefox and geckodriver
- Python 3.11+
- Docker and Docker Compose
- `undetected_geckodriver`, `selenium`, `seleniumwire`, `psycopg[binary]`, `pyyaml`,
  `pyvirtualdisplay`, `torch`, `transformers`, `pillow`, `requests`

## Setup

**1. Infrastructure**

```bash
export POSTGRES_PASSWORD=...
export GRAFANA_ADMIN_PASSWORD=...
docker compose up -d
```

TimescaleDB, Prometheus and Grafana bind to loopback only. Data lives outside the
containers under `/home/audit/data/` — change the volume paths in
`docker-compose.yml` for your host.

**2. Schema**

```bash
psql "postgresql://audit:$POSTGRES_PASSWORD@127.0.0.1:5432/audit" -f schema.sql
```

The schema is not idempotent. Changes to a populated database need a migration, not
a drop-and-recreate.

**3. Firefox profiles**

Create one Firefox profile per account and log in manually once. The collector never
handles credentials; it attaches to a profile that already holds a session cookie.

**4. Configuration**

`research_Warmup_config.yaml` holds the bucket definitions and the account roster.
Each account entry:

```yaml
- id: U_MI_W1_BandF_Fit
  email: ...
  firefox_profile: /home/.mozilla/firefox/UserMI1.BandF_Fit
  screen: [1920, 1080]
  window: [1920, 1080]
  role: study              # study | probe
  gender: M                # study accounts only
  condition: interaction   # interaction | no_interaction
  follow_suggested: true
  assigned_interests: [Business and Finance, Fitness, Entertainment]
  bucket: BandF_Fit
```

Top-level keys:

| Key | Meaning |
| --- | --- |
| `bucket_definitions` | CLIP prompt text per content category. |
| `save_feed_data` | Whether raw API responses are archived to disk. See below. |

## Running

```bash
python orchestrator.py
```

Sessions run inside a 09:00–23:00 window, twice per weekday and three times per
weekend day, with roughly a 10% chance that a whole matched group takes the day off
together. State persists in `orchestrator_state.json`.

Single session, for debugging:

```bash
python configurable_collector.py
```

Grafana is at `http://127.0.0.1:3000`; session metrics arrive via Pushgateway.

---

## Data

Three tables carry the results. `sessions` is one row per browser launch. `posts` and
`interactions` are TimescaleDB hypertables, chunked weekly and compressed after seven
days.

`posts` holds one row per *observation*, not per post — if the same media appears in
two sessions, that is two rows. CLIP fields (`clip_score`, `clip_aligned`,
`clip_top_bucket`, `vlm_scores`) may be populated retroactively during calibration.
`post_data` carries the full parsed media object as JSONB.

`interactions` holds every logged event: scroll, pause, like, follow, mouse_move,
post_view, back_scroll, session_start, session_end, error. It is also the single
source of truth for interaction budgets — the collector counts against it directly
rather than keeping a separate tally.

### `save_feed_data`

When enabled, each session writes a gzipped JSONL archive of the raw captured API
responses to `data/raw/{account_id}/{session_id}.jsonl.gz`, and the path is recorded
in `sessions.raw_archive_path`.

The archive exists so that a later change to the parser can be replayed against what
Instagram actually sent, rather than requiring re-collection of a feed that no longer
exists. It also holds substantially more third-party personal data than the parsed
rows do. Leaving it disabled is the data-minimising default; enable it only for a
purpose you can name in your data protection documentation, and set a retention
limit. `sessions.final_stats` records which setting each session ran under, since a
NULL `raw_archive_path` alone cannot distinguish "disabled" from "captured nothing".

---

## Legal and ethical scope

This code operates automated accounts against a platform whose terms of service
prohibit automated access, and it collects personal data belonging to people who
have not consented and are not the research subjects.

Before running it against a live platform you need, at minimum:

- A documented legal basis under GDPR Art. 6, and Art. 9 / §27 BDSG where the
  content buckets touch special categories such as political opinion
- A minimisation policy covering what is stored and in what form

Publishing collected personal data is separately restricted under §27(4) BDSG.
Publish aggregates and classification results, not the underlying corpus.

---

## License

GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).
