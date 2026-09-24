#!/usr/bin/env python3
"""App-research agent for Composio toolkit triage.

Pipeline (each stage writes data/<stage>.jsonl and is resumable):

  baseline  closed-book LLM answer, no browsing        -> the "first pass" floor
  scout     research agent with WebSearch + WebFetch   -> pass 1
  ground    deterministic checks, no LLM: evidence URL liveness, quote-in-page
            match, Composio public toolkit probe        -> grounding report
  verify    adversarial second agent re-checks every scored field against
            primary docs, fed the grounding report      -> pass 2
  review    builds the human queue: rows the verifier changed, low confidence,
            or dead evidence                            -> data/review_queue.json
  score     compares baseline / scout / verify / final against data/gold.json
  export    merges final + human overrides into site/data.json

Agents run through Claude Code headless (`claude -p`) so there is no API key to
manage: the orchestrator is plain Python, and every LLM call is a separate
sandboxed process that can only use WebSearch and WebFetch.

  python agent/run.py scout --apps 1-100 --workers 8
  python agent/run.py all
"""
import argparse, concurrent.futures as cf, html, json, os, re, subprocess, sys, threading, time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROMPTS = ROOT / "agent" / "prompts"
SCHEMA = (ROOT / "agent" / "schema.json").read_text()
VSCHEMA = (ROOT / "agent" / "verify_schema.json").read_text()
RUBRIC = (PROMPTS / "rubric.md").read_text()
SCORED = ["primary_auth", "access", "api_style", "mcp", "verdict", "blocker"]


def load_apps():
    import csv
    return list(csv.DictReader(open(DATA / "apps.csv")))


def read_jsonl(name):
    p = DATA / f"{name}.jsonl"
    if not p.exists():
        return {}
    out = {}
    for line in p.open():
        if line.strip():
            r = json.loads(line)
            out[r["id"]] = r
    return out


TOKENISH = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}|\b(sk|rk|pk)_(live|test)_[A-Za-z0-9]{10,}")


def append_jsonl(name, row):
    # docs pages often show sample credentials; never persist anything token-shaped
    with (DATA / f"{name}.jsonl").open("a") as f:
        f.write(TOKENISH.sub("[redacted-example-token]", json.dumps(row)) + "\n")


def claude(prompt, schema, tools, model, timeout=900):
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--json-schema", schema,
           "--model", model, "--permission-mode", "dontAsk", "--no-session-persistence"]
    if tools:
        cmd += ["--allowedTools", *tools]
    else:
        cmd += ["--disallowedTools", "WebSearch", "WebFetch", "Bash", "Read", "Glob", "Grep"]
    t = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd="/tmp")
    res = json.loads(p.stdout)
    out = res.get("structured_output")
    if out is None:
        raise RuntimeError(res.get("result", p.stderr)[:300])
    u = res.get("usage", {}).get("server_tool_use", {})
    meta = {"secs": round(time.time() - t), "cost_usd": res.get("total_cost_usd"),
            "searches": u.get("web_search_requests"), "fetches": u.get("web_fetch_requests"),
            "turns": res.get("num_turns"), "model": model}
    return out, meta


def fill(tpl, **kw):
    s = (PROMPTS / tpl).read_text()
    for k, v in kw.items():
        s = s.replace("{" + k + "}", v)
    return s


def run_llm_stage(stage, apps, workers, model):
    done = read_jsonl(stage)
    todo = [a for a in apps if a["id"] not in done]
    scout = read_jsonl("scout")
    ground = read_jsonl("ground")

    def job(a):
        kw = dict(app=a["app"], category=a["category"], hint=a["hint"], rubric=RUBRIC)
        if stage == "baseline":
            out, meta = claude(fill("closedbook.md", **kw), SCHEMA, None, model)
        elif stage == "scout":
            out, meta = claude(fill("scout.md", **kw), SCHEMA, ["WebSearch", "WebFetch"], model)
        else:  # verify
            rec = dict(scout[a["id"]]["out"])
            kw["record"] = json.dumps(rec, indent=1)
            kw["grounding"] = json.dumps(ground.get(a["id"], {}).get("checks", []), indent=1)
            out, meta = claude(fill("verify.md", **kw), VSCHEMA, ["WebSearch", "WebFetch"], model)
        return {"id": a["id"], "app": a["app"], "out": out, "meta": meta}

    if stage == "verify":
        todo = [a for a in todo if a["id"] in scout]
        todo.sort(key=lambda a: verify_priority(a["id"], scout, ground))

    def guarded(a):  # hard spend cap across all stages, checked before every call
        if spent() >= BUDGET:
            raise RuntimeError(f"budget cap ${BUDGET} reached")
        return job(a)

    print(f"[{stage}] {len(todo)} to run, {len(done)} cached, ${spent():.2f} spent of ${BUDGET}", flush=True)
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(guarded, a): a for a in todo}
        for f in cf.as_completed(futs):
            a = futs[f]
            try:
                row = f.result()
                append_jsonl(stage, row)
                print(f"  ok  {a['id']:>3} {a['app']:<28} {row['meta']['secs']}s", flush=True)
            except Exception as e:  # leave it uncached so a rerun retries it
                print(f"  ERR {a['id']:>3} {a['app']:<28} {e}", flush=True)


BUDGET = float(os.environ.get("BUDGET_USD", "48"))
_lock = threading.Lock()


def spent():
    with _lock:
        return sum((r["meta"].get("cost_usd") or 0) for st in ("baseline", "scout", "verify")
                   for r in read_jsonl(st).values())


def verify_priority(i, scout, ground):
    """Gold-sample apps first (so accuracy is measured on the same policy), then the
    riskiest rows: low confidence and evidence that failed grounding."""
    gold = json.load(open(DATA / "gold.json")) if (DATA / "gold.json").exists() else {}
    checks = ground.get(i, {}).get("checks", [])
    bad = sum(c["status"] in ("dead", "quote_not_found") for c in checks)
    return (i not in gold, scout[i]["out"]["confidence"] - 0.1 * bad)


# ---------- grounding: deterministic, no LLM ----------
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/126 Safari/537.36"}


def fetch_text(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(3_000_000).decode("utf-8", "ignore")
            code = r.status
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    return code, norm(html.unescape(re.sub(r"<[^>]+>", " ", raw)))


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def quote_match(quote, text):
    """1.0 if the quote appears verbatim; else share of its 4-word shingles on the page."""
    q = norm(quote)
    if not q or not text:
        return 0.0
    if q in text:
        return 1.0
    w = q.split()
    sh = [" ".join(w[i:i + 4]) for i in range(max(1, len(w) - 3))]
    return round(sum(s in text for s in sh) / len(sh), 2)


def composio_probe(app):
    base = re.sub(r"\(.*?\)", "", app).strip().lower()
    slugs = {re.sub(r"[^a-z0-9]", "", base), re.sub(r"[^a-z0-9]+", "_", base).strip("_")}
    for s in slugs:
        code, _ = fetch_text(f"https://composio.dev/toolkits/{s}")
        if code == 200:
            return s
    return None


def ground(apps, workers):
    scout = read_jsonl("scout")
    done = read_jsonl("ground")

    def job(a):
        rec = scout[a["id"]]["out"]
        pages, checks = {}, []
        for ev in rec.get("evidence", []):
            url = ev["url"]
            if url not in pages:
                pages[url] = fetch_text(url)
            code, text = pages[url]
            if code != 200:
                status = "dead" if code in (404, 410) or code == 0 else f"http_{code}"
            elif len(text) < 400:
                status = "js_rendered_unverifiable"
            else:
                m = quote_match(ev["quote"], text)
                status = "quote_verbatim" if m == 1 else "quote_partial" if m >= .6 else "quote_not_found"
            checks.append({"field": ev["field"], "url": url, "http": code, "status": status})
        return {"id": a["id"], "app": a["app"], "checks": checks,
                "composio_slug": composio_probe(a["app"])}

    todo = [a for a in apps if a["id"] in scout and a["id"] not in done]
    print(f"[ground] {len(todo)} to run")
    with cf.ThreadPoolExecutor(workers) as ex:
        for row in ex.map(job, todo):
            append_jsonl("ground", row)
            bad = sum(c["status"] in ("dead", "quote_not_found") for c in row["checks"])
            print(f"  {row['id']:>3} {row['app']:<28} evidence={len(row['checks'])} bad={bad} composio={row['composio_slug']}")


# ---------- human review queue ----------
def review(apps):
    verify, ground_, scout = read_jsonl("verify"), read_jsonl("ground"), read_jsonl("scout")
    q = []
    for a in apps:
        v, g = verify.get(a["id"]), ground_.get(a["id"], {})
        rec = (v or scout[a["id"]])["out"]
        reasons = []
        scored_changes = [c for c in (v or {}).get("out", {}).get("changes", []) if c["field"] in SCORED]
        if scored_changes:
            reasons.append("verifier changed " + ", ".join(sorted({c['field'] for c in scored_changes})))
        if rec["confidence"] < 0.6:
            reasons.append(f"low confidence {rec['confidence']}")
        if sum(c["status"] == "quote_not_found" for c in g.get("checks", [])) >= 2:
            reasons.append("2+ quotes not found on cited pages")
        if any(c["status"] == "dead" for c in g.get("checks", [])):
            reasons.append("dead evidence link in pass 1")
        if reasons:
            q.append({"id": a["id"], "app": a["app"], "reasons": reasons})
    json.dump(q, open(DATA / "review_queue.json", "w"), indent=1)
    print(f"[review] {len(q)} rows queued for a human")


# ---------- scoring against the hand-checked gold sample ----------
def field_ok(field, pred, gold):
    if field == "api_style":  # right if it names the gold primary style
        return gold[0] in pred
    if isinstance(gold, list):  # gold may accept more than one defensible answer
        return pred in gold
    return pred == gold


def lint(rec):
    """Deterministic consistency rules between fields (no LLM). Returns (fixed_record, notes)."""
    r, notes = dict(rec), []
    if r["access"] == "self_serve_free" and r["blocker"] == "none" and r["verdict"] != "build_now":
        notes.append(f"verdict {r['verdict']} -> build_now: free access and no blocker"); r["verdict"] = "build_now"
    if r["access"] in ("partner_or_sales_gated", "no_public_api") and r["verdict"] == "build_now":
        notes.append("verdict build_now contradicts gated access -> build_with_friction"); r["verdict"] = "build_with_friction"
    if r["verdict"] == "build_now" and r["blocker"] != "none":
        notes.append(f"blocker {r['blocker']} -> none: verdict is build_now"); r["blocker"] = "none"
    if r["verdict"] != "build_now" and r["blocker"] == "none":
        notes.append("verdict not build_now but no blocker named: flagged for review")
    return r, notes


def final_rows():
    verify, scout = read_jsonl("verify"), read_jsonl("scout")
    scout = {i: {**r, "out": lint(r["out"])[0]} for i, r in scout.items()}
    over = json.load(open(DATA / "human_overrides.json")) if (DATA / "human_overrides.json").exists() else {}
    out = {}
    for i, r in {**scout, **verify}.items():
        rec = {k: v for k, v in r["out"].items() if k != "changes"}
        rec["verified"] = i in verify
        rec.update(over.get(i, {}).get("set", {}))
        out[i] = rec
    return out


def score():
    gold = json.load(open(DATA / "gold.json"))
    scout = {i: r["out"] for i, r in read_jsonl("scout").items()}
    verify = {i: r["out"] for i, r in read_jsonl("verify").items()}
    passes = {"baseline": {i: r["out"] for i, r in read_jsonl("baseline").items()},
              "scout": scout,
              "lint": {i: lint(r)[0] for i, r in scout.items()},
              # verifier only reached part of the sample: score it, and the scout, on that subset
              "scout_on_verified": {i: r for i, r in scout.items() if i in verify},
              "verify": verify}
    report = {}
    for name, rows in passes.items():
        per_field = {f: [0, 0] for f in SCORED}
        misses = []
        for i, g in gold.items():
            if i not in rows:
                continue
            for f in SCORED:
                ok = field_ok(f, rows[i][f], g[f])
                per_field[f][0] += ok
                per_field[f][1] += 1
                if not ok:
                    misses.append({"id": i, "app": g["app"], "field": f, "got": rows[i][f], "gold": g[f]})
        hit = sum(v[0] for v in per_field.values()); tot = sum(v[1] for v in per_field.values())
        report[name] = {"accuracy": round(hit / tot, 3) if tot else None, "hits": hit, "total": tot,
                        "per_field": {f: round(v[0] / v[1], 3) if v[1] else None for f, v in per_field.items()},
                        "misses": misses}
        print(f"[score] {name:<9} {hit}/{tot} = {report[name]['accuracy']}")
    json.dump(report, open(DATA / "score.json", "w"), indent=1)


def export():
    apps = {a["id"]: a for a in load_apps()}
    fin, ground_, scout, verify = final_rows(), read_jsonl("ground"), read_jsonl("scout"), read_jsonl("verify")
    over = json.load(open(DATA / "human_overrides.json")) if (DATA / "human_overrides.json").exists() else {}
    rows = []
    for i, a in apps.items():
        if i not in fin:
            continue
        r = dict(fin[i]); r.update(id=int(i), category=a["category"], hint=a["hint"])
        r["composio_slug"] = ground_.get(i, {}).get("composio_slug")
        r["verifier_changes"] = verify[i]["out"].get("changes", []) if i in verify else None
        r["human"] = over.get(i, {}).get("note")
        r["cost"] = round(sum((x[i]["meta"]["cost_usd"] or 0) for x in (scout, verify) if i in x), 3)
        rows.append(r)
    checks = [c for g in ground_.values() for c in g["checks"]]
    st = count_by(checks, "status")
    grounding = {"urls": len(checks), "live": sum(c["http"] == 200 for c in checks),
                 "dead": st.get("dead", 0), "verbatim": st.get("quote_verbatim", 0),
                 "partial": st.get("quote_partial", 0),
                 "checkable": sum(st.get(k, 0) for k in ("quote_verbatim", "quote_partial", "quote_not_found")),
                 "by_status": st}
    metas = [r["meta"] for s in ("baseline", "scout", "verify") for r in read_jsonl(s).values()]
    run = {"llm_calls": len(metas), "cost": sum(m["cost_usd"] or 0 for m in metas),
           "agent_minutes": round(sum(m["secs"] for m in metas) / 60), "overrides": len(over)}
    content = json.load(open(ROOT / "site" / "content.json"))
    payload = {"generated": time.strftime("%Y-%m-%d"), "rows": rows,
               "score": json.load(open(DATA / "score.json")),
               "gold": json.load(open(DATA / "gold.json")),
               "review_queue": json.load(open(DATA / "review_queue.json")),
               "human_overrides": over, "grounding": grounding, "run": run,
               "verified_ids": sorted(verify, key=int),
               "lintFixes": sum(bool(lint(r["out"])[1]) and lint(r["out"])[0] != r["out"] for r in scout.values()),
               **content}
    site = ROOT / "site"
    (site / "data.json").write_text(json.dumps(payload, indent=1))
    html_ = (site / "template.html").read_text().replace("__REPO__", content["repo"]).replace("__DATE__", payload["generated"])
    (site / "index.html").write_text(html_.replace("__DATA__", json.dumps(payload).replace("</", "<\\/")))
    import csv
    cols = ["id", "app", "category", "one_liner", "primary_auth", "auth_methods", "access", "access_detail", "api_style",
            "api_breadth", "mcp", "mcp_url", "webhooks", "verdict", "blocker", "blocker_detail", "composio_slug", "confidence", "evidence_urls"]
    with open(site / "findings.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(cols)
        for r in rows:
            r2 = dict(r, auth_methods="|".join(r["auth_methods"]), api_style="|".join(r["api_style"]),
                      evidence_urls="|".join(e["url"] for e in r["evidence"]))
            w.writerow([r2.get(c) for c in cols])
    lines = [f"# Toolkit triage: {len(rows)} apps researched for Composio", "",
             "Machine-readable: data.json (full records, scores, gold sample), findings.csv (one row per app).",
             "Fields: primary_auth, access (self_serve_free|self_serve_trial|paid_plan_required|approval_required|partner_or_sales_gated|no_public_api), mcp (official|community|none_found), verdict (build_now|build_with_friction|blocked), blocker.",
             f"Accuracy on a {len(payload['gold'])}-app hand-audited sample: " + ", ".join(f"{k} {v['accuracy']:.0%}" for k, v in payload["score"].items()), "", "## Apps"]
    lines += [f"- {r['app']} ({r['category']}): {r['verdict']}; auth={r['primary_auth']}; access={r['access']}; mcp={r['mcp']}; blocker={r['blocker']}" for r in rows]
    (site / "llms.txt").write_text("\n".join(lines) + "\n")
    print(f"[export] {len(rows)} rows -> site/index.html, data.json, findings.csv, llms.txt")


def count_by(xs, k):
    out = {}
    for x in xs:
        out[x[k]] = out.get(x[k], 0) + 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["baseline", "scout", "ground", "verify", "review", "score", "export", "all"])
    ap.add_argument("--apps", default="1-100", help="id range, e.g. 1-10 or 3,7,9")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args()
    ids = set()
    for part in a.apps.split(","):
        lo, _, hi = part.partition("-")
        ids |= {str(i) for i in range(int(lo), int(hi or lo) + 1)}
    apps = [x for x in load_apps() if x["id"] in ids]
    stages = ["baseline", "scout", "ground", "verify", "review", "score", "export"] if a.stage == "all" else [a.stage]
    for s in stages:
        if s in ("baseline", "scout", "verify"):
            run_llm_stage(s, apps, a.workers, a.model)
        elif s == "ground":
            ground(apps, a.workers)
        elif s == "review":
            review(apps)
        elif s == "score":
            score() if (DATA / "gold.json").exists() else print("[score] no data/gold.json yet")
        else:
            export()


if __name__ == "__main__":
    sys.exit(main())
