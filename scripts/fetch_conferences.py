#!/usr/bin/env python3
"""
Fetch accepted papers from gisbi-kim's conference explorers and bake a single
normalized dataset for evolution.html:

    NeurIPS 2026  github.com/gisbi-kim/conference-explorers  (neurips2026/data.json.gz)
    ICLR 2026     github.com/gisbi-kim/iclr2026-explorer     (output/iclr2026_papers.json)
    ICML 2026     github.com/gisbi-kim/icml2026-explorer     (output/icml2026_papers.json)
    CVPR 2026     github.com/gisbi-kim/cvpr2026-explorer     (data/cvpr2026_papers.json)

Output: data/conferences.json — papers with id/title/abstract/venue/status/orgs.
Run: python3 scripts/fetch_conferences.py
"""

import gzip
import json
import re
import sys
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"

SOURCES = [
    # (venue, raw URL, parser)
    ("NeurIPS 2026",
     "https://raw.githubusercontent.com/gisbi-kim/conference-explorers/master/neurips2026/data.json.gz",
     "neurips"),
    ("ICLR 2026",
     "https://raw.githubusercontent.com/gisbi-kim/iclr2026-explorer/master/output/iclr2026_papers.json",
     "iclr"),
    ("ICML 2026",
     "https://raw.githubusercontent.com/gisbi-kim/icml2026-explorer/main/output/icml2026_papers.json",
     "icml"),
    ("CVPR 2026",
     "https://raw.githubusercontent.com/gisbi-kim/cvpr2026-explorer/main/data/cvpr2026_papers.json",
     "cvpr"),
]

STATUS_NORMALIZE = {
    "oral": "oral", "spotlight": "spotlight", "highlight": "highlight",
    "poster": "poster", "poster only": "poster", "regular": "poster",
    "poster_oral": "oral", "notable top 5%": "oral", "notable top 25%": "spotlight",
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "papers-explorer"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def parse_neurips(raw: bytes) -> list:
    d = json.loads(gzip.decompress(raw).decode())
    out = []
    for p in d.get("papers", []):
        ptype = (p.get("presentation_type") or "").lower()
        out.append({
            "id": f"neurips26:{p.get('id')}",
            "title": p.get("title", ""),
            "abstract": p.get("abstract", ""),
            "venue": "NeurIPS 2026",
            "status": STATUS_NORMALIZE.get(ptype, "poster"),
            "orgs": p.get("institutions") or [],
            "url": p.get("official") or p.get("openreview") or "",
        })
    return out


def parse_iclr(raw: bytes) -> list:
    d = json.loads(raw.decode())
    out = []
    for p in d.get("papers", []):
        status = (p.get("status") or "").lower()
        out.append({
            "id": f"iclr26:{p.get('id')}",
            "title": p.get("title", ""),
            "abstract": p.get("abstract", ""),
            "venue": "ICLR 2026",
            "status": STATUS_NORMALIZE.get(status, "poster"),
            "orgs": p.get("institutions") or [],
            "url": f"https://openreview.net/forum?id={p.get('id')}" if p.get("id") else "",
        })
    return out


def parse_icml(raw: bytes) -> list:
    d = json.loads(raw.decode())
    papers = d.get("papers", []) if isinstance(d, dict) else d
    out = []
    for p in papers:
        decision = (p.get("decision") or "").lower()
        out.append({
            "id": f"icml26:{p.get('id')}",
            "title": p.get("title", ""),
            "abstract": p.get("abstract", ""),
            "venue": "ICML 2026",
            "status": STATUS_NORMALIZE.get(decision, "poster"),
            "orgs": p.get("institutions") or [],
            "url": "",
        })
    return out


def parse_cvpr(raw: bytes) -> list:
    papers = json.loads(raw.decode())
    out = []
    for i, p in enumerate(papers):
        ptype = (p.get("type") or "poster").lower()
        out.append({
            "id": f"cvpr26:{i}",
            "title": p.get("title", ""),
            "abstract": p.get("abstract", ""),
            "venue": "CVPR 2026",
            "status": STATUS_NORMALIZE.get(ptype, "poster"),
            "orgs": [s.strip() for s in (p.get("institutions") or "").split(";") if s.strip()],
            "url": "",
        })
    return out


def main():
    parsers = {"neurips": parse_neurips, "iclr": parse_iclr,
               "icml": parse_icml, "cvpr": parse_cvpr}
    all_papers = []
    for venue, url, kind in SOURCES:
        try:
            papers = parsers[kind](fetch(url))
            print(f"{venue}: {len(papers)} papers")
            all_papers.extend(papers)
        except Exception as e:
            print(f"{venue}: FAILED ({e})", file=sys.stderr)
    if not all_papers:
        print("No conference data fetched; keeping existing file", file=sys.stderr)
        sys.exit(1)
    out = {"built": "conference-fetch", "papers": all_papers}
    path = DATA_DIR / "conferences.json"
    path.write_text(json.dumps(out))
    print(f"Wrote {path} ({path.stat().st_size // 1024} KB), {len(all_papers)} papers")


if __name__ == "__main__":
    main()
