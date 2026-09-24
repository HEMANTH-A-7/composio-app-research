# Toolkit Triage: 100 apps, researched by an agent, checked by a second agent and a human

**Live case study:** https://toolkit-triage.vercel.app

This repo holds a research pipeline that decides, for each app, whether Composio can build an agent toolkit today. It captures:

- category and a one-liner
- auth methods and the primary auth a multi-tenant toolkit would use
- self-serve vs gated access
- API style and breadth
- whether an MCP server already exists (official or community)
- a buildability verdict with the main blocker
- verbatim evidence quotes with URLs

It then measures how accurate those answers are.

## How it works

```
apps.csv ─► baseline ─► scout ─► ground ─► verify ─► review ─► score / export
            (no web)   (web)    (no LLM)   (web,     (human
                                           adversarial) queue)
```

| Stage | What runs | Output |
|---|---|---|
| `baseline` | Same model and schema with browsing disabled. This is the floor we measure against. | `data/baseline.jsonl` |
| `scout` | Headless Claude Code worker limited to `WebSearch` and `WebFetch`. Returns JSON against `agent/schema.json`, which uses enums so every field can be scored. | `data/scout.jsonl` |
| `ground` | Plain Python with no LLM. Fetches every cited URL, checks that the quoted sentence is on the page, and probes `composio.dev/toolkits/<slug>`. | `data/ground.jsonl` |
| `verify` | A second, adversarial agent re-checks each scored field against primary docs, using the grounding report as input. It returns a corrected record plus a change log. | `data/verify.jsonl` |
| `review` | Queues rows for a human when the verifier changed a scored field, confidence is below 0.7, or an evidence link is dead. Human decisions go in `data/human_overrides.json`. | `data/review_queue.json` |
| `score` | Scores all four passes against `data/gold.json`, a hand-audited answer key for 25 apps. | `data/score.json` |
| `export` | Builds the page and machine-readable outputs. | `site/index.html`, `data.json`, `findings.csv`, `llms.txt` |

## Run it

Requirements: Python 3.10+ and a logged-in [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI (`claude`). There are no pip dependencies and no API keys.

```bash
# one app, end to end (41 = Shopify; ids are in data/apps.csv)
python agent/run.py scout  --apps 41
python agent/run.py ground --apps 41
python agent/run.py verify --apps 41

# all 100 apps; waits out rate/session limits and resumes
./agent/run_all.sh
python agent/run.py score
python agent/run.py export      # rebuilds site/
```

Each stage appends to a JSONL file keyed by app id. A rerun only redoes apps that failed, so you can kill it at any point. To research a different set of apps, edit `data/apps.csv`. To change the rubric, edit `agent/prompts/rubric.md`: it is shared by all three agents and is the single source of truth for what each enum means.

## Where a human was needed

- **Writing the gold answer key.** `data/gold.json` covers 25 apps drawn by stratified random sampling (seed 2026). Each field was checked against primary docs, and the source is recorded per app. Fields with two defensible readings accept both.
- **Clearing the review queue.** Each override is logged with a reason in `data/human_overrides.json`, and the page shows it on the row.
- **Rubric design.** Deciding what "self-serve" means for a trial with credit, or whether a local CLI counts as buildable, is a product judgment, not a research one.

## Layout

```
agent/run.py            orchestrator (all stages)
agent/run_all.sh        resumable full run
agent/schema.json       output contract (enums)
agent/prompts/*.md      scout, verifier, closed-book prompts + shared rubric
data/apps.csv           the 100 apps
data/gold.json          hand-audited answer key (25 apps)
data/*.jsonl            raw per-stage outputs, with cost/time metadata
site/                   the deployed case study
```
