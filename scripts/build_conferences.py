#!/usr/bin/env python3
"""
Build the conference-page dataset for a keyword: select ALL accepted papers
matching the keyword across the four conferences, write per-paper one-liners,
and group them into summarized categories. Output feeds conference.html.

Usage:
    OPENROUTER_API_KEY=... python3 scripts/build_conferences.py "world model"

Output: data/evolution/conferences-<slug>.json (+ cached intermediate steps)
This script is entirely independent of build_evolution.py (evolution.html).
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_DIR = DATA_DIR / "evolution"
CACHE_DIR = OUT_DIR / "_cache"
API_KEY = os.environ.get("OPENROUTER_API_KEY")
BASE_URL = "https://openrouter.ai/api"
MODEL = os.environ.get("EVOLUTION_MODEL") or "z-ai/glm-5.3-flash"
MODEL_KEY = re.sub(r"[^A-Za-z0-9]+", "-", MODEL)

SOURCE = "conferences.json"
MAX_PAPERS = 5000        # safety cap; all matching papers normally fit well below
BATCH_SIZE = 20          # papers per one-liner call
ABSTRACT_CHARS = 700     # trim abstracts sent to the model


def call_model(prompt: str, max_tokens: int, retries: int = 4) -> Optional[str]:
    """OpenAI-format chat completion against OpenRouter (glm-5.3-flash is a
    reasoning model: thinking consumes max_tokens, so budget headroom)."""
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens + 4096,
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


def load_papers() -> List[Dict[str, Any]]:
    with open(DATA_DIR / SOURCE) as f:
        data = json.load(f)
    papers = data.get("papers", [])
    for p in papers:
        # Score oral/spotlight above posters, since there are no upvotes here.
        p["hfUpvotes"] = {"oral": 60, "spotlight": 30, "highlight": 20}.get(p.get("status"), 0)
    return papers


def select_papers(keyword: str, papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """ALL papers matching on title/abstract, ranked by relevance + status."""
    kw = keyword.lower()
    scored = []
    for p in papers:
        title = (p.get("title") or "").lower()
        abstract = (p.get("abstract") or "").lower()
        th = title.count(kw)
        ab = abstract.count(kw)
        if th == 0 and ab == 0:
            continue
        scored.append((th * 8 + min(ab, 4) + p["hfUpvotes"] / 50.0, p))
    scored.sort(key=lambda x: -x[0])
    if len(scored) > MAX_PAPERS:
        print(f"  warning: {len(scored)} matches, capping at {MAX_PAPERS} for cost",
              file=sys.stderr)
    return [p for _, p in scored[:MAX_PAPERS]]


def write_one_liners(papers: List[Dict[str, Any]], slug: str) -> Dict[str, str]:
    """Per-paper: one sentence on what it newly contributes."""
    cache = CACHE_DIR / f"one_liners.{MODEL_KEY}.conferences-{slug}.json"
    if cache.exists():
        with open(cache) as f:
            results = json.load(f)
        # Incremental: only compute for papers not yet cached, so growing the
        # selection (e.g. cap raised, refreshed conference data) reuses what's
        # already paid for.
        missing = [p for p in papers if p["id"] not in results]
        if not missing:
            return results
        papers = missing
    else:
        results = {}

    batches = [papers[i:i + BATCH_SIZE] for i in range(0, len(papers), BATCH_SIZE)]
    for bi, batch in enumerate(batches):
        listing = json.dumps(
            [{"id": p["id"], "title": p["title"], "abstract": (p.get("abstract") or "")[:ABSTRACT_CHARS]}
             for p in batch],
            indent=1)
        prompt = (
            "Below are accepted conference papers with abstracts, from NeurIPS/ICLR/ICML/CVPR 2026. "
            "For EACH paper write ONE sentence (max 25 words) describing what NEW capability, "
            "technique, or problem shift it introduces. Be concrete and technical, not generic. "
            "Do not praise.\n\n"
            f"Papers:\n{listing}\n\n"
            'Return ONLY a JSON array: [{"id": "<id>", "added": "<sentence>"}, ...] '
            "with exactly one entry per input paper, same ids."
        )
        print(f"  one-liners batch {bi + 1}/{len(batches)}", file=sys.stderr)
        out = extract_json(call_model(prompt, 4096))
        items = out if isinstance(out, list) else (
            next((out[k] for k in ("papers", "results", "items", "entries", "list", "data")
                  if isinstance(out, dict) and isinstance(out.get(k), list)), None)
            if out else None)
        if items:
            for item in items:
                if isinstance(item, dict) and "id" in item and "added" in item:
                    results[item["id"]] = item["added"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache, "w") as f:
        json.dump(results, f, indent=1)
    return results


def write_categories(papers: List[Dict[str, Any]], one_liners: Dict[str, str],
                     slug: str) -> List[Dict[str, Any]]:
    """Group papers into summarized categories (one call)."""
    cache = CACHE_DIR / f"categories.{MODEL_KEY}.conferences-{slug}.json"
    if cache.exists():
        with open(cache) as f:
            return json.load(f)

    listing = json.dumps([
        {"id": p["id"], "title": p["title"], "added": one_liners.get(p["id"], "")}
        for p in papers
    ], indent=1)
    prompt = (
        "Below are accepted conference papers on one research area, each with a one-line "
        "'added' note. Group them into distinct CATEGORIES — a category is a set of papers "
        "tackling the same core problem or aspect of the research area. Rules:\n"
        "- 6 to 14 categories total. Every paper goes in exactly one category.\n"
        "- Categories should be distinguishable by the problem they attack, not by venue or team.\n"
        "- For each category also write a 1-2 sentence description of what this aspect of "
        "the field is about.\n\n"
        f"Papers:\n{listing}\n\n"
        'Return ONLY JSON: {"categories": [{"name": "<short category name, 3-6 words>", '
        '"description": "<1-2 sentences>", "ids": ["<id>", ...]}, ...]}'
    )
    print("  grouping categories", file=sys.stderr)
    out = extract_json(call_model(prompt, 8192))
    cats = []
    if out and isinstance(out, dict):
        cats = [
            {"name": t.get("name", "?"), "description": t.get("description", ""),
             "ids": list(t.get("ids", []))}
            for t in out.get("categories", [])
            if isinstance(t, dict) and t.get("ids")
        ]
    ids = [p["id"] for p in papers]
    known = {i for t in cats for i in t["ids"]}
    leftovers = [i for i in ids if i not in known]
    if leftovers:
        cats.append({"name": "Other", "description": "", "ids": leftovers})
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache, "w") as f:
        json.dump(cats, f, indent=1)
    return cats


def build(keyword: str):
    if not API_KEY:
        print("Error: OPENROUTER_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")

    all_papers = load_papers()
    selected = select_papers(keyword, all_papers)
    print(f"{keyword}: {len(selected)} papers selected", file=sys.stderr)
    if not selected:
        print("Nothing matched; aborting", file=sys.stderr)
        sys.exit(1)

    compact = [{
        "id": p["id"],
        "title": p.get("title"),
        "org": (p.get("orgs") or [""])[0] if p.get("orgs") else "Independent",
        "upvotes": p["hfUpvotes"],
        "venue": p.get("venue", ""),
        "status": p.get("status", ""),
        "url": p.get("url", ""),
        "abstract": (p.get("abstract") or "")[:ABSTRACT_CHARS],
    } for p in selected]

    one_liners = write_one_liners(compact, slug)
    for c in compact:
        one_liners.setdefault(c["id"], "")
    cats = write_categories(compact, one_liners, slug)

    by_id = {c["id"]: c for c in compact}
    categories_out = []
    for t in cats:
        papers_t = [by_id[i] for i in t["ids"] if i in by_id]
        papers_t.sort(key=lambda p: (-p["upvotes"], p["title"] or ""))
        categories_out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "count": len(papers_t),
            "papers": [{
                "id": p["id"], "title": p["title"],
                "org": p["org"], "upvotes": p["upvotes"],
                "venue": p["venue"], "status": p["status"], "url": p["url"],
                "added": one_liners.get(p["id"], ""),
            } for p in papers_t],
        })
    categories_out.sort(key=lambda t: -t["count"])

    result = {
        "keyword": keyword,
        "source": SOURCE,
        "builtAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "paperCount": len(selected),
        "categories": categories_out,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"conferences-{slug}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=1)
    print(f"Wrote {out_path} ({out_path.stat().st_size // 1024} KB)", file=sys.stderr)

    # Update manifest for conference.html.
    manifest_path = OUT_DIR / "manifest.json"
    manifest = []
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
    manifest = [m for m in manifest if m.get("slug") != f"conferences-{slug}"]
    manifest.append({
        "slug": f"conferences-{slug}", "keyword": keyword,
        "builtAt": result["builtAt"], "paperCount": result["paperCount"],
        "categories": len(categories_out), "source": SOURCE,
    })
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python3 scripts/build_conferences.py "world model"', file=sys.stderr)
        sys.exit(1)
    build(sys.argv[1])
