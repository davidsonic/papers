#!/usr/bin/env python3
"""
Build an "evolution" dataset for a keyword: clusters papers into idea threads,
writes a per-paper "what it added" one-liner, extracts lab lineage, and groups
time into dynamic eras. Output feeds evolution.html.

Usage:
    OPENROUTER_API_KEY=... python3 scripts/build_evolution.py "world model"


Output: data/evolution/<slug>.json (+ cached intermediate steps)
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_DIR = DATA_DIR / "evolution"
CACHE_DIR = OUT_DIR / "_cache"
API_KEY = os.environ.get("OPENROUTER_API_KEY")
BASE_URL = "https://openrouter.ai/api"
# Model: z-ai/glm-5.3-flash via OpenRouter; override with EVOLUTION_MODEL.
MODEL = os.environ.get("EVOLUTION_MODEL") or "z-ai/glm-5.3-flash"
# Cache files are per-model so switching models never mixes or discards results.
MODEL_KEY = re.sub(r"[^A-Za-z0-9]+", "-", MODEL)

MODEL_WORKER = MODEL
MODEL_SYNTH = MODEL

MAX_PAPERS = 320          # cap per keyword (top by relevance score)
BATCH_SIZE = 20           # papers per one-liner call
ABSTRACT_CHARS = 700      # trim abstracts sent to the model


def call_claude(prompt: str, model: str, max_tokens: int, retries: int = 4) -> Optional[str]:
    # glm-5.3-flash is a reasoning model: thinking consumes the max_tokens
    # budget, so cap its effort and budget generously or the answer arrives
    # empty (finish_reason "length").
    payload = {
        "model": model,
        "max_tokens": max_tokens + 4096,  # headroom for thinking
        "temperature": 0.3,
        "reasoning": {"effort": "low"},
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "content-type": "application/json",
    }
    url = f"{BASE_URL}/v1/chat/completions"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode())
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            if choice.get("finish_reason") == "length" and not msg.get("content"):
                raise RuntimeError("model ran out of tokens while thinking")
            return msg.get("content")
        except Exception as e:
            print(f"  API error ({e}), retry {attempt + 1}/{retries}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    return None


def extract_json(text: Optional[str]) -> Optional[Any]:
    """Pull the first JSON object/array out of a model response."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    for start, end in (("{", "}"), ("[", "]")):
        s = text.find(start)
        if s == -1:
            continue
        depth = 0
        for i in range(s, len(text)):
            if text[i] == start:
                depth += 1
            elif text[i] == end:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[s:i + 1])
                    except json.JSONDecodeError:
                        break
    return None


def load_papers(source: str = "papers_all.json") -> List[Dict[str, Any]]:
    path = DATA_DIR / source
    with open(path) as f:
        data = json.load(f)
    return data


def select_papers(keyword: str, papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keyword match on title/abstract/aiKeywords, ranked by relevance + traction."""
    kw = keyword.lower()
    scored = []
    for p in papers:
        title = (p.get("title") or "").lower()
        abstract = (p.get("abstract") or "").lower()
        kws = " ".join(p.get("aiKeywords") or []).lower()
        title_hits = title.count(kw)
        abs_hits = abstract.count(kw)
        kw_hits = kws.count(kw)
        if title_hits == 0 and abs_hits == 0 and kw_hits == 0:
            continue
        # Title matches weigh far more than a passing abstract mention.
        score = title_hits * 8 + min(abs_hits, 4) + kw_hits * 2
        score += (p.get("hfUpvotes") or 0) / 50.0
        score += min(p.get("githubStars") or 0, 5000) / 5000.0
        scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:MAX_PAPERS]]


def compact_paper(p: Dict[str, Any]) -> Dict[str, Any]:
    org_name = derive_org(p, {})
    return {
        "id": p["id"],
        "title": p.get("title"),
        "date": (p.get("publishedDate") or "")[:10],
        "org": org_name,
        "upvotes": p.get("hfUpvotes") or 0,
        "stars": p.get("githubStars") or 0,
        "github": p.get("githubRepo"),
        "venue": p.get("venue", ""),
        "status": p.get("status", ""),
        "url": p.get("url", ""),
        "abstract": (p.get("abstract") or "")[:ABSTRACT_CHARS],
    }


def write_one_liners(papers: List[Dict[str, Any]]) -> Dict[str, str]:
    """Per-paper: one sentence on what it added relative to prior work in its space."""
    cache = CACHE_DIR / f"one_liners.{MODEL_KEY}.json"
    if cache.exists():
        with open(cache) as f:
            return json.load(f)

    results: Dict[str, str] = {}
    batches = [papers[i:i + BATCH_SIZE] for i in range(0, len(papers), BATCH_SIZE)]
    for bi, batch in enumerate(batches):
        listing = json.dumps(
            [{"id": p["id"], "title": p["title"], "date": p["date"], "abstract": p["abstract"]}
             for p in batch],
            indent=1)
        prompt = (
            "Below are research papers (arXiv-style) with abstracts, sorted roughly by date. "
            "For EACH paper write ONE sentence (max 25 words) describing what NEW capability, "
            "technique, or problem shift it introduced relative to work that came before it "
            "(the earlier papers in the list are context for the later ones). "
            "Be concrete and technical, not generic. Do not praise.\n\n"
            f"Papers:\n{listing}\n\n"
            'Return ONLY a JSON array: [{"id": "<id>", "added": "<sentence>"}, ...] '
            "with exactly one entry per input paper, same ids."
        )
        print(f"  one-liners batch {bi + 1}/{len(batches)}", file=sys.stderr)
        out = extract_json(call_claude(prompt, MODEL_WORKER, 4096))
        items = out if isinstance(out, list) else (
            next((out[k] for k in ("papers", "results", "items", "entries", "list", "data")
                  if isinstance(out, dict) and isinstance(out.get(k), list)), None)
            if out else None)
        if items:
            for item in items:
                if isinstance(item, dict) and "id" in item and "added" in item:
                    results[item["id"]] = item["added"]
    with open(cache, "w") as f:
        json.dump(results, f, indent=1)
    return results


def write_threads(papers: List[Dict[str, Any]], one_liners: Dict[str, str]) -> List[List[str]]:
    """Cluster papers into idea threads; returns lists of paper ids per thread."""
    cache = CACHE_DIR / f"threads.{MODEL_KEY}.json"
    if cache.exists():
        with open(cache) as f:
            return json.load(f)

    listing = json.dumps([
        {"id": p["id"], "title": p["title"], "date": p["date"],
         "added": one_liners.get(p["id"], "")}
        for p in papers
    ], indent=1)
    prompt = (
        "Below are papers on one research area, each with a one-line 'added' note. "
        "Cluster them into distinct RESEARCH THREADS — a thread is a lineage of work "
        "tackling the same core problem or building on the same base idea (e.g. "
        "'interactive world generation', 'world model for autonomous driving', "
        "'long-video consistency'). Rules:\n"
        "- 6 to 14 threads total. Every paper goes in exactly one thread.\n"
        "- Threads should be distinguishable by the problem they attack, not by venue or team.\n"
        "- Papers of the same direct lineage belong together even across years.\n\n"
        f"Papers:\n{listing}\n\n"
        'Return ONLY JSON: {"threads": [{"name": "<short thread name, 3-6 words>", '
        '"ids": ["<id>", ...]}, ...]}'
    )
    print("  clustering threads", file=sys.stderr)
    out = extract_json(call_claude(prompt, MODEL_SYNTH, 8192))
    threads = []
    if out and isinstance(out, dict):
        threads = [
            {"name": t.get("name", "?"), "ids": [i for i in t.get("ids", [])]}
            for t in out.get("threads", [])
            if isinstance(t, dict) and t.get("ids")
        ]
    ids = [p["id"] for p in papers]
    known = {i for t in threads for i in t["ids"]}
    leftovers = [i for i in ids if i not in known]
    if leftovers:
        threads.append({"name": "Other", "ids": leftovers})
    with open(cache, "w") as f:
        json.dump(threads, f, indent=1)
    return threads


def write_genealogy(threads: List[Dict[str, Any]], one_liners: Dict[str, str],
                    papers_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, List[List[str]]]:
    """Per thread: parent->child 'builds on' edges. Returns {thread_name: [[child, parent], ...]}."""
    cache = CACHE_DIR / f"genealogy.{MODEL_KEY}.json"
    if cache.exists():
        with open(cache) as f:
            return json.load(f)

    edges: Dict[str, List[List[str]]] = {}
    for t in threads:
        name = t["name"]
        ps = sorted(t["ids"], key=lambda i: papers_by_id.get(i, {}).get("date", ""))
        if len(ps) < 3:
            edges[name] = []
            continue
        listing = json.dumps([
            {"id": i, "date": papers_by_id.get(i, {}).get("date", ""),
             "title": papers_by_id.get(i, {}).get("title", ""),
             "added": one_liners.get(i, "")}
            for i in ps
        ], indent=1)
        prompt = (
            "Papers in one research thread, in date order, each with what it added. "
            "Build the genealogy: for each paper, which earlier paper(s) does it directly "
            "BUILD ON (extends, fixes, scales, or applies the same core idea)? A paper can "
            "have 0 or 1 parents here (pick the single most direct predecessor; use 0 only "
            "for the first paper or genuinely new starts). Do not force chains — papers "
            "attacking different sub-problems should not be linked.\n\n"
            f"Papers:\n{listing}\n\n"
            'Return ONLY JSON: {"edges": [["<child id>", "<parent id>"], ...]} '
            "with one entry per child that has a parent."
        )
        out = extract_json(call_claude(prompt, MODEL_WORKER, 4096))
        got = []
        if out and isinstance(out, dict):
            for e in out.get("edges", []):
                if (isinstance(e, list) and len(e) == 2
                        and e[0] in ps and e[1] in ps and e[0] != e[1]):
                    got.append([e[0], e[1]])
        edges[name] = got

    with open(cache, "w") as f:
        json.dump(edges, f, indent=1)
    return edges


def write_eras(papers: List[Dict[str, Any]], threads: List[Dict[str, Any]],
               one_liners: Dict[str, str], top_orgs: List[str]) -> Dict[str, Any]:
    """Dynamic time eras + per-lab lineage narrative. One synthesis call."""
    cache = CACHE_DIR / f"synthesis.{MODEL_KEY}.json"
    if cache.exists():
        with open(cache) as f:
            return json.load(f)

    months = Counter(p["date"][:7] for p in papers)
    thread_summary = json.dumps([
        {"name": t["name"], "count": len(t["ids"]),
         "span": [min(p["date"] for p in papers if p["id"] in t["ids"]),
                  max(p["date"] for p in papers if p["id"] in t["ids"])]}
        for t in threads
    ], indent=1)
    month_summary = json.dumps(dict(sorted(months.items())), indent=1)
    # Sample representative one-liners across time for era naming.
    sample = sorted(papers, key=lambda p: p["date"])
    step = max(1, len(sample) // 60)
    timeline_sample = json.dumps([
        {"date": p["date"], "added": one_liners.get(p["id"], "")[:120]} for p in sample[::step]
    ], indent=1)

    prompt = (
        "You are analyzing how a research field evolved over time.\n"
        f"Paper counts by month:\n{month_summary}\n\n"
        f"Research threads (name, count, first/last date):\n{thread_summary}\n\n"
        f"Sample of paper contributions across time (date, what it added):\n{timeline_sample}\n\n"
        "Task 1: Group the timeline into 3-6 ERAS. An era is a period (consecutive months) "
        "where the field was focused on a recognizable set of problems — eras need NOT be "
        "equal-length months; they are dynamic. For each era give: start month, end month, "
        "a 4-8 word label, a 2-3 sentence description of what the field was focused on, "
        "and 3-6 paper ids that best REPRESENT the era (influential or characteristic ones).\n\n"
        "Task 2: For the top labs in the data, write a 2-4 sentence LINEAGE note: how that "
        "lab's world-model work evolved paper to paper and what it pushed toward.\n\n"
        "Task 3: Write a 4-6 sentence FIELD NARRATIVE: how the field moved from era to era, "
        "what problems drove each shift, and where it seems to be heading.\n\n"
        "IMPORTANT for Task 2: use these EXACT org names as keys: "
        f"{json.dumps(top_orgs)}.\n\n"
        'Return ONLY JSON:\n{"eras": [{"start": "YYYY-MM", "end": "YYYY-MM", "label": "...", '
        '"focus": "...", "representativeIds": ["id", ...]}],\n'
        ' "labLineage": [{"org": "<exact org name from the list>", "note": "..."}],\n'
        ' "fieldNarrative": "..."}'
    )
    print("  synthesizing eras + lab lineage", file=sys.stderr)
    out = extract_json(call_claude(prompt, MODEL_SYNTH, 8192)) or {}
    # The model can't reliably pick paper ids it never saw individually; derive
    # era representatives locally from top-upvoted papers inside each era window.
    known_ids = {p["id"] for p in papers}
    for era in out.get("eras", []):
        if not isinstance(era, dict):
            continue
        start, end = era.get("start", ""), era.get("end", "")
        window = [p for p in papers
                  if p["id"][:7].replace("-", "") and start <= p["date"][:7] <= end]
        window.sort(key=lambda p: (-p["upvotes"], p["date"]))
        era["representativeIds"] = [p["id"] for p in window[:6]]
        _ = known_ids
    with open(cache, "w") as f:
        json.dump(out, f, indent=1)
    return out


def derive_org(p: Dict[str, Any], one_liners: Dict[str, str]) -> str:
    org = p.get("organization")
    if isinstance(org, dict) and org.get("name"):
        return org["name"]
    if isinstance(org, str) and org:
        return org
    # Fall back to GitHub repo owner (usually the lab/first org).
    repo = p.get("githubRepo") or ""
    if "/" in repo:
        owner = repo.split("/")[0]
        # Only plausible org handles; skip URLs/malformed entries.
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", owner) and not owner.lower().startswith(("http", "www", "github")):
            return owner
    return "Independent"


def build(keyword: str):
    if not API_KEY:
        print("Error: OPENROUTER_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    all_papers = load_papers()
    selected = select_papers(keyword, all_papers)
    print(f"{keyword}: {len(selected)} papers selected", file=sys.stderr)

    compact = [compact_paper(p) for p in selected]
    one_liners = write_one_liners(compact)
    missing = [p["id"] for p in compact if p["id"] not in one_liners]
    for mid in missing:
        one_liners[mid] = ""
    threads = write_threads(compact, one_liners)

    org_counts = Counter(p["org"] for p in compact)
    top_orgs = [o for o, _ in org_counts.most_common(12) if o != "Independent"][:10]
    synth = write_eras(compact, threads, one_liners, top_orgs)
    genealogy = write_genealogy(threads, one_liners, {p["id"]: p for p in compact})

    by_id = {p["id"]: p for p in selected}
    threads_out = []
    for t in threads:
        papers_t = [compact_paper(by_id[i]) for i in t["ids"] if i in by_id]
        papers_t.sort(key=lambda p: p["date"])
        threads_out.append({
            "name": t["name"],
            "count": len(papers_t),
            "edges": genealogy.get(t["name"], []),
            "papers": [{
                "id": p["id"], "title": p["title"], "date": p["date"][:10],
                "org": p["org"], "upvotes": p["upvotes"],
                "stars": p["stars"], "github": p["github"],
                "venue": p.get("venue", ""), "status": p.get("status", ""),
                "url": p.get("url", ""),
                "added": one_liners.get(p["id"], ""),
            } for p in papers_t],
        })
    threads_out.sort(key=lambda t: -t["count"])

    # Lab lineage: top orgs by paper count in this keyword set.
    lineage_by_org = {l.get("org"): l.get("note", "") for l in synth.get("labLineage", [])
                      if isinstance(l, dict)}

    result = {
        "keyword": keyword,
        "source": "papers_all.json",
        "builtAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "paperCount": len(selected),
        "dateRange": [min(p["date"] for p in compact), max(p["date"] for p in compact)],
        "threads": threads_out,
        "eras": synth.get("eras", []),
        "labs": [{"org": o, "count": org_counts[o], "note": lineage_by_org.get(o, "")}
                 for o in top_orgs],
        "fieldNarrative": synth.get("fieldNarrative", ""),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{slug}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=1)
    print(f"Wrote {out_path} ({out_path.stat().st_size // 1024} KB)", file=sys.stderr)

    # Update manifest for evolution.html.
    manifest_path = OUT_DIR / "manifest.json"
    manifest = []
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
    manifest = [m for m in manifest if m.get("slug") != slug]
    manifest.append({
        "slug": slug, "keyword": keyword, "builtAt": result["builtAt"],
        "paperCount": result["paperCount"],
        "threads": len(threads_out),
        "source": "papers_all.json",
    })
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python3 scripts/build_evolution.py "world model"', file=sys.stderr)
        sys.exit(1)
    build(sys.argv[1])
