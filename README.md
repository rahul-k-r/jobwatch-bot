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
  ├─ Gemini (batched) role scope / level / required years / PhD / sponsorship; failure → held and retried, then alert as "unclassified"
  └─ ntfy push        marked notified only after delivery succeeds; otherwise retried next cycle
```

State is one SQLite file. A source that fails backs off exponentially (honoring `Retry-After`)
and sends one notice after 3 failures plus one on recovery.

Postings are also scanned for instructions aimed at AI tools ("if you are an AI, include the word …");
alerts for those are labelled `⚠ AI-directed instructions`.

**Request rate:** every 5 min (±10%) from 6am to 9pm Pacific on weekdays, every 30 min otherwise
(`offpeak` in `config.yaml`). When nothing is new, that's one request per site per cycle. At most
`max_concurrent_sources` (10) poll at once, and a source can set `interval_seconds` to be polled
less often. A source can also set `snooze_until: YYYY-MM-DD`: until that date its new postings still go through every filter but alert at low priority tagged `snoozed`, with no resume build. The client identifies itself honestly (`jobwatch/0.1`) instead of posing as a browser.

## Sources

| Adapter | API | Companies |
|---|---|---|
| `eightfold` | Eightfold PCSX, or the older `/api/apply/v2` with `api: v2` | Microsoft, Netflix, Qualcomm |
| `amazon` | amazon.jobs search | Amazon |
| `apple` | jobs.apple.com search | Apple |
| `ibm` | IBM careers search | IBM |
| `google` | `feed.xml` (the results pages are disallowed by robots.txt) | Google, including DeepMind and YouTube |
| `jibe` | Jibe careers sites (`/api/jobs`), honoring `crawl-delay` | AMD |
| `workday` | Workday CXS | Adobe, Broadcom, Capital One, Cisco, CrowdStrike, eBay, Expedia, HP, HPE, Intel, Marvell, Mastercard, Micron, NVIDIA, Palo Alto Networks, PayPal, Red Hat, Salesforce, Snap, Visa, Workday, Zillow, Zoom |
| `oracle` | Oracle Recruiting Cloud (`recruitingCEJobRequisitions`), US via the tenant's country facet | Dell, JPMorgan Chase, Oracle, Texas Instruments |
| `greenhouse` | Greenhouse job board API | Affirm, Airbnb, Airtable, Akuna Capital, Anthropic, Arize AI, Asana, Block, Boomi, Box, Brex, Chime, Clarity AI, Cloudflare, Coinbase, Databricks, Datadog, Dialpad, Discord, DoorDash, Dropbox, Duolingo, Elastic, Figma, Figure AI, Five Rings, GitLab, Glean, Gong, Gusto, Headlands Technologies, Hightouch, Hootsuite, Hudson River Trading, Hume AI, IMC Trading, Instacart, Intercom, Isomorphic Labs, Jane Street, Jump Trading, Later, LivePerson, Lucid Motors, Lyft, Make.com, MongoDB, Neuralink, Okta, Parloa, PDT Partners, PhysicsX, Pinterest, PlanetScale, PolyAI, Reddit, Robinhood, Roblox, Safari AI, SambaNova, Samsara, Samsung Semiconductor, Scale AI, Scandit, SoFi, Speechmatics, Stability AI, Stripe, Tenstorrent, Toast, Together AI, Twilio, Vercel, Waymo, Wayve, Weights & Biases (CoreWeave), xAI |
| `ashby` | Ashby posting API | Abridge, Anyscale, Attio, Baseten, Bland AI, Causaly, Cerebras, Character AI, Clay Labs, Clerk, Cognition, Cohere, Cradle, Crusoe, Cursor, Decagon, Deepgram, ElevenLabs, Etched, Faculty, Fireworks AI, Glacis AI, Harvey, Hebbia, Inngest, Klue, Kraken, Lambda, LangChain, Linear, Mercor, Midjourney, Modal, Monad (Category Labs), n8n, Notion, OpenAI, Paradigm, Perk, Perplexity, Photoroom, Physical Intelligence, Pinecone, Plaid, Ramp, Replit, Resend, RunPod, Runway, Safe Superintelligence, Sierra, Snowflake, Supabase, Synthesia, Temporal, The Browser Company, Thinking Machines Lab, Vapi, WorkOS, Writer, Zapier, Zed Industries |
| `lever` | Lever postings API | Lyra Health, Palantir, Pigment, Tinybird, Zoox |
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
| `GEMINI_API_KEY` | Classification. Without it (or the key below), alerts go out unclassified. |
| `JOBWATCH_ANTHROPIC_API_KEY` | Optional. Claude (`claude_model`, default Haiku 5.5) classifies when both Gemini models fail; paid, so last in the chain and capped by `claude_daily_limit` (100 calls). |
| `NTFY_TOPIC`, `NTFY_SERVER`, `NTFY_TOKEN` | Push delivery. Without a topic, alerts print to the console. |
| `NTFY_TOPIC_RESUMES`, `NTFY_TOPIC_LOW` | Optional separate topics for handoff results ("Resume ready") and for low-priority alerts such as reposts. Each falls back to `NTFY_TOPIC`. |
| `HEARTBEAT_URL` | Optional dead-man's switch (e.g. healthchecks.io), hit after every cycle. |
| `HANDOFF_DIR` | Optional. Classified high-priority alerts are written to `inbox/` as JSON for another process; results it drops in `outbox/` are pushed as notifications. A `ready` result's `pdf` is attached to the push if it is a real PDF under 15 MB. Greenhouse and Ashby postings also carry their free-text application questions; a result's `answers` become one "Copy answers" button (ntfy app; trailing answers are left out past ~6 KB). |
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

If neither model can be reached, the posting stays pending and is retried each cycle for
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
