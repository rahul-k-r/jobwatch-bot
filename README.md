# jobwatch

Polls company career sites and pushes an alert to your phone when a new US engineering posting
appears that isn't senior, doesn't rule out visa sponsorship, and isn't a repost (reposts still
alert, at low priority).

All sources are plain HTTP/JSON: no browser, no login.

## How a posting flows

```
poll source (newest first, stop at first page with nothing new)
  ├─ first run for a company → record everything as "seeded", alert on nothing
  ├─ title rules      senior / intern / recruiting / out-of-scope → filtered
  ├─ detail fetch     description (+ locations for Workday)
  ├─ US + sponsorship "must be a U.S. citizen", "unable to sponsor", ITAR, clearance … → filtered
  ├─ repost signals   old requisition, reused req ID, near-identical text, "reposted" → low priority
  ├─ Gemini (batched) role scope / level / required years / PhD / sponsorship; failure → alert as "unclassified"
  └─ ntfy push        marked notified only after delivery succeeds; otherwise retried next cycle
```

State is one SQLite file. A source that fails backs off exponentially (honoring `Retry-After`)
and sends one notice after 3 failures plus one on recovery.

Postings are also scanned for instructions aimed at AI tools ("if you are an AI, include the word …");
alerts for those are labelled `⚠ AI-directed instructions`.

**Request rate:** every 5 min (±10%) from 6am to 9pm Pacific on weekdays, every 30 min otherwise
(`offpeak` in `config.yaml`). When nothing is new, that's one request per site per cycle. At most
`max_concurrent_sources` (10) poll at once, and a source can set `interval_seconds` to be polled
less often. The client identifies itself honestly (`jobwatch/0.1`) instead of posing as a browser.

## Sources

| Adapter | API | Companies |
|---|---|---|
| `eightfold` | Eightfold PCSX, or the older `/api/apply/v2` with `api: v2` | Microsoft, Netflix, Qualcomm |
| `amazon` | amazon.jobs search | Amazon |
| `apple` | jobs.apple.com search | Apple |
| `ibm` | IBM careers search | IBM |
| `google` | `feed.xml` (the results pages are disallowed by robots.txt) | Google, including DeepMind and YouTube |
| `jibe` | Jibe careers sites (`/api/jobs`), honoring `crawl-delay` | AMD |
| `workday` | Workday CXS | Adobe, Broadcom, Capital One, Cisco, CrowdStrike, HP, HPE, Intel, Mastercard, NVIDIA, Palo Alto Networks, PayPal, Red Hat, Salesforce, Visa, Workday |
| `greenhouse` | Greenhouse job board API | Airbnb, Airtable, Anthropic, Arize AI, Boomi, Clarity AI, Coinbase, Databricks, Dialpad, Discord, DoorDash, Figma, Five Rings, Glean, Gong, Headlands Technologies, Hightouch, Hootsuite, Hudson River Trading, Hume AI, Intercom, Isomorphic Labs, Jane Street, Later, LivePerson, Make.com, Neuralink, Parloa, PDT Partners, PhysicsX, PlanetScale, PolyAI, Safari AI, Scandit, Speechmatics, Stability AI, Stripe, Tenstorrent, Twilio, Vercel, Waymo, Wayve, Weights & Biases (CoreWeave), xAI |
| `ashby` | Ashby posting API | Attio, Bland AI, Causaly, Cerebras, Clay Labs, Clerk, Cohere, Cradle, Decagon, Deepgram, ElevenLabs, Etched, Faculty, Glacis AI, Inngest, Klue, Kraken, LangChain, Linear, Midjourney, Monad (Category Labs), n8n, Notion, OpenAI, Paradigm, Perk, Perplexity, Photoroom, Pinecone, Resend, RunPod, Runway, Sierra, Snowflake, Supabase, Synthesia, Temporal, The Browser Company, Vapi, WorkOS, Zapier, Zed Industries |
| `lever` | Lever postings API | Palantir, Pigment, Tinybird, Zoox |
| `workable` | Workable widget API | Hugging Face, Semios, Trail of Bits |

### Adding a source

- **Greenhouse / Ashby / Lever / Workable:** `board` is the slug from the public board URL,
  e.g. `{company: "Stripe", type: "greenhouse", board: "stripe"}`.
- **Workday:** take `host/tenant/site` from the `*.myworkdayjobs.com` URL. Facet keys and IDs differ
  per tenant; read them from the `facets` array of a list response, and check that `total` actually
  drops when you apply one (some tenants silently ignore unknown facets).
- **Company-specific seniority:** `exclude_title` on a source adds patterns for that company only
  (e.g. Microsoft `IC4+`, Amazon `III`, Netflix `L5+`).
- Disable any source with `enabled: false`.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # .venv/bin/python on Linux
cp .env.example .env                               # fill in what you use; everything is optional
.venv/Scripts/python -m pytest
.venv/Scripts/python -m jobwatch --once            # one cycle; the first run seeds every company
.venv/Scripts/python -m jobwatch                   # run forever
```

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Classification. Without it, alerts go out unclassified. |
| `NTFY_TOPIC`, `NTFY_SERVER`, `NTFY_TOKEN` | Push delivery. Without a topic, alerts print to the console. |
| `HEARTBEAT_URL` | Optional dead-man's switch (e.g. healthchecks.io), hit after every cycle. |
| `HANDOFF_DIR` | Optional. Classified high-priority alerts are written to `inbox/` as JSON for another process; results it drops in `outbox/` are pushed as notifications. |
| `JOBWATCH_DB` | SQLite path (default `jobwatch.db`). |

**Alerts on Android:** install ntfy, subscribe to your topic, and turn on instant delivery. On the
public server, use a long random topic name (it's effectively the password). Each alert opens the
posting on tap and also carries the link as text and an "Open posting" button.

**Tuning:** filters, thresholds and polling live in `config.yaml`. `repost.created_gap_days` is a
heuristic; check the `reason` column of the `jobs` table after a week and adjust.

## Classification

Gemini (Flash-Lite, falling back to Gemma 4 26B on errors) reads each posting that passes the rules
and returns whether it's hands-on engineering at all, the level, required years (using the
Master's-degree path when several are listed), whether a PhD is strictly required, sponsorship
restrictions and repost mentions. Out-of-scope roles, postings needing 4+ years, a PhD with no
Master's alternative, or ruling out sponsorship are dropped. Usage is capped per day below the free
tier.

## Docker

```bash
docker build -t jobwatch .
docker run -d --name jobwatch --restart unless-stopped --env-file .env -v jobwatch-data:/data jobwatch
```

For a private push channel, self-host ntfy with `auth-default-access: deny-all`, create a user and
token, and set `NTFY_SERVER` / `NTFY_TOKEN`.

## License

[MIT](LICENSE)
