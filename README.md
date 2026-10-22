# jobwatch

Polls company career sites and pushes an alert to your phone when a new US engineering posting
appears that isn't senior, doesn't rule out visa sponsorship, and isn't a repost (reposts still
alert, at low priority).

All sources are plain HTTP/JSON: no browser, no login.

## Configuration

| File | Holds | Who edits it |
|---|---|---|
| `catalog/*.yaml` | The companies and how to poll them, one file per adapter (`custom.yaml` for the single-company ones). Company-specific seniority patterns, poll intervals, and `enabled: false` for dead boards. | Shared: add companies, fix boards |
| `config.yaml` | Your `profile` (who the classifier judges postings for), title filters, years/sponsorship rules, `companies:` (`disable`, `enable`, `snooze` by catalog name), polling and classifier settings. Gitignored: start from `config.example.yaml`. | You |
| `.env` | Secrets and deployment settings (see `.env.example`). | You |

## How a posting flows

```
poll source (newest first, stop at first page with nothing new)
  ├─ first run for a company → record everything as "seeded", alert on nothing
  ├─ title rules      senior / intern / recruiting / out-of-scope → filtered
  ├─ detail fetch     description (+ locations for Workday, application questions for Greenhouse/Ashby)
  ├─ US + sponsorship "must be a U.S. citizen", "unable to sponsor", ITAR, clearance … → filtered
  ├─ repost signals   old requisition, reused req ID, near-identical text, "reposted" → low priority
  ├─ LLM (batched)    role scope / level / required years / PhD / sponsorship; failure → held and retried, then alert as "unclassified"
  └─ ntfy push        marked notified only after delivery succeeds; otherwise retried next cycle
```

State is one SQLite file. A source that fails backs off exponentially (honoring `Retry-After`)
and sends one notice after 3 failures plus one on recovery.

Postings are also scanned for instructions aimed at AI tools ("if you are an AI, include the word …");
alerts for those are labelled `⚠ AI-directed instructions`.

**Request rate:** every 5 min (±10%) from 6am to 9pm Pacific on weekdays, every 30 min otherwise
(`offpeak` in `config.yaml`). When nothing is new, that's one request per site per cycle. At most
`max_concurrent_sources` (10) poll at once, and a catalog entry can set `interval_seconds` to be
polled less often. A company in `companies.snooze` (`Name: YYYY-MM-DD`) still goes through every
filter until that date but alerts at low priority tagged `snoozed`, with no resume build. The client
identifies itself honestly (`jobwatch/0.1`) instead of posing as a browser.

**Hot reload:** `config.yaml` and the catalog are re-read within ~2 s of a save (no restart). Added or changed sources are polled
immediately; everything else (filters, snoozes, intervals, classifier limits) applies from the next use. A file that
fails to parse is logged and ignored. `.env` values and turning the classifier on or off still need a restart.

## Sources

The catalog has 226 companies (199 polled with `config.example.yaml`). Read `catalog/` for the list.

| Adapter | API | Catalog file |
|---|---|---|
| `greenhouse` | Greenhouse job board API | `greenhouse.yaml` (92) |
| `ashby` | Ashby posting API | `ashby.yaml` (82) |
| `workday` | Workday CXS | `workday.yaml` (23) |
| `lever` | Lever postings API | `lever.yaml` (13) |
| `oracle` | Oracle Recruiting Cloud (`recruitingCEJobRequisitions`), US via the tenant's country facet | `oracle.yaml` (4) |
| `workable` | Workable widget API | `workable.yaml` (4) |
| `eightfold` | Eightfold PCSX, or the older `/api/apply/v2` with `api: v2` | `eightfold.yaml` (3) |
| `amazon`, `apple`, `ibm` | Each company's own search API | `custom.yaml` |
| `google` | `feed.xml` (the results pages are disallowed by robots.txt); includes DeepMind and YouTube | `custom.yaml` |
| `jibe` | Jibe careers sites (`/api/jobs`), honoring `crawl-delay` (AMD) | `custom.yaml` |

### Adding a source

Append an entry to the catalog file for its adapter. `company` must be unique across the catalog
(it's part of every job key).

- **Greenhouse / Ashby / Lever / Workable:** `board` is the slug from the public board URL,
  e.g. `{company: "Stripe", type: "greenhouse", board: "stripe"}`.
- **Workday:** take `host/tenant/site` from the `*.myworkdayjobs.com` URL. Facet keys and IDs differ
  per tenant; read them from the `facets` array of a list response, and check that `total` actually
  drops when you apply one (some tenants silently ignore unknown facets).
- **Oracle Recruiting Cloud:** `host` and `site` come from the `…/hcmUI/CandidateExperience/en/sites/<site>`
  URL the careers page lands on (the host can be the company's own domain, e.g. Dell). The US facet is
  looked up by name; set `country` for another country.
- **Company-specific seniority:** `exclude_title` on a source adds patterns for that company only
  (e.g. Microsoft `IC4+`, Amazon `III`, Netflix `L5+`).
- `enabled: false` in the catalog is for boards that are dead. To skip a working company for
  yourself, list it under `companies.disable` in `config.yaml`.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # .venv/bin/python on Linux
cp .env.example .env                               # fill in what you use; everything is optional
cp config.example.yaml config.yaml                 # then set your profile, filters and companies
.venv/Scripts/python -m pytest
.venv/Scripts/python -m jobwatch --once            # one cycle; the first run seeds every company
.venv/Scripts/python -m jobwatch                   # run forever
```

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Classification. Without it (or the key below), alerts go out unclassified. |
| `JOBWATCH_ANTHROPIC_API_KEY` | Optional. Claude (`claude_model`, default Haiku 5.5) classifies when both Gemini models fail; paid, so last in the chain and capped by `claude_daily_limit` (100 calls). |
| `NTFY_TOPIC`, `NTFY_SERVER`, `NTFY_TOKEN` | Push delivery. Without a topic, alerts print to the console. |
| `NTFY_TOPIC_RESUMES`, `NTFY_TOPIC_LOW` | Optional separate topics for handoff results ("Resume ready") and for low-priority alerts such as reposts. Each falls back to `NTFY_TOPIC`. |
| `HEARTBEAT_URL` | Optional dead-man's switch (e.g. healthchecks.io), hit after every cycle. |
| `HANDOFF_DIR` | Optional. Classified high-priority alerts are written to `inbox/` as JSON for another process; results it drops in `outbox/` are pushed as notifications. A `ready` result's `pdf` is attached to the push if it is a real PDF under 15 MB. Greenhouse and Ashby postings also carry their free-text application questions; a result's `answers` become one "Copy answers" button (ntfy app; trailing answers are left out past ~6 KB). |
| `JOBWATCH_DB` | SQLite path (default `jobwatch.db`). |

**Alerts on Android:** install ntfy, subscribe to your topic, and turn on instant delivery. On the
public server, use a long random topic name (it's effectively the password). Each alert opens the
posting on tap and also carries the link as text, plus a one-line summary of the role's stack and
conditions.

**Tuning:** your profile, filters, thresholds and polling live in `config.yaml`. `repost.created_gap_days` is a
heuristic; check the `reason` column of the `jobs` table after a week and adjust.

## Classification

An LLM reads each posting that passes the rules and returns whether it's hands-on engineering at
all, the level, required years (using the Master's-degree path when several are listed), whether a
PhD is strictly required, sponsorship restrictions, repost mentions and a one-line highlight for the
push. Out-of-scope roles, internships, postings needing 4+ years, a PhD with no Master's
alternative, or ruling out sponsorship are dropped.

Models are tried in order: Gemini 3.1 Flash-Lite (pinned), then `gemini-flash-lite-latest` (takes
over when 3.1 is retired), then Claude Haiku if `JOBWATCH_ANTHROPIC_API_KEY` is set. Postings go in
batches of 10, up to `max_parallel_batches` (3) at once; each model's starts are spaced by its
`requests_per_minute` and capped by a daily limit kept below the free tier.

If no model can be reached, the posting stays pending and is retried each cycle for
`classify_grace_seconds` (default 900); one "Classifier unreachable" push goes out per outage. After
that it alerts as "unclassified", so an outage can delay a posting but never lose it.

## Docker

```bash
docker build -t jobwatch .
docker run -d --name jobwatch --restart unless-stopped --env-file .env -v jobwatch-data:/data jobwatch
```

For a private push channel, self-host ntfy with `auth-default-access: deny-all`, create a user and
token, and set `NTFY_SERVER` / `NTFY_TOKEN`.

## License

[MIT](LICENSE)
