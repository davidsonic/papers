#!/usr/bin/env python3
"""
Backfill HuggingFace daily papers from 2023-05-04 to today.
Fetches papers for each date from the HF API and caches raw responses.
Then compacts into papers_all.json (deduplicated by arXiv ID).
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

# Configuration
START_DATE = datetime(2023, 5, 4)  # HF Daily Papers start date
TODAY = datetime.now()
DATA_DIR = Path(__file__).parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"
API_BASE = "https://huggingface.co/api/daily_papers"

def ensure_dirs():
    """Create data directories if they don't exist."""
    DATA_DIR.mkdir(exist_ok=True)
    RAW_DIR.mkdir(exist_ok=True)

def fetch_papers_for_date(date: datetime, force: bool = False, max_retries: int = 3) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch papers for a specific date from HF API.
    Caches result in data/raw/YYYY-MM-DD.json.
    Returns None if fetch fails, [] if no papers for that date.
    """
    date_str = date.strftime("%Y-%m-%d")
    cache_file = RAW_DIR / f"{date_str}.json"

    # Return cached if exists and not forcing refresh
    if cache_file.exists() and not force:
        try:
            with open(cache_file) as f:
                return json.load(f)
        except:
            pass  # Fall through to fetch

    # Fetch from API with retries
    url = f"{API_BASE}?date={date_str}"
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                papers = json.loads(response.read().decode())

            # Cache result
            with open(cache_file, 'w') as f:
                json.dump(papers, f)

            return papers
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # No papers for this date, return empty list
                with open(cache_file, 'w') as f:
                    json.dump([], f)
                return []
            elif e.code == 429:
                # Rate limited, retry with backoff
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # exponential backoff: 1s, 2s, 4s
                    print(f"Rate limited for {date_str}, waiting {wait_time}s...", file=sys.stderr)
                    time.sleep(wait_time)
                else:
                    print(f"Error fetching {date_str}: HTTP 429 (rate limited, max retries)", file=sys.stderr)
                    return None
            else:
                print(f"Error fetching {date_str}: HTTP {e.code}", file=sys.stderr)
                return None
        except Exception as e:
            print(f"Error fetching {date_str}: {e}", file=sys.stderr)
            return None

    return None

def extract_paper_fields(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract and normalize fields from HF daily paper entry.
    Entry is {paper: {...}, publishedAt, title, summary, thumbnail, ...}
    """
    paper = entry.get("paper", {})
    # HF paper object has: id, title, summary (abstract), upvotes, ai_summary, ai_keywords, githubRepo, githubStars
    return {
        "id": paper.get("id"),  # arXiv ID or unique ID
        "title": paper.get("title"),
        "abstract": paper.get("summary", ""),  # HF API calls it "summary" but it's the abstract
        "authors": paper.get("authors", []),
        "publishedDate": paper.get("publishedAt"),
        "hfUpvotes": paper.get("upvotes", 0),
        "githubRepo": paper.get("githubRepo"),  # Single GitHub repo URL
        "githubStars": paper.get("githubStars"),  # Star count from that repo
        "aiSummary": paper.get("ai_summary"),  # HF's AI-generated summary (if available)
        "aiKeywords": paper.get("ai_keywords", []),  # HF's AI-extracted keywords (if available)
        "thumbnail": entry.get("thumbnail"),  # HF thumbnail image URL
    }

def backfill_papers(start: datetime = START_DATE, end: datetime = TODAY, force: bool = False):
    """
    Backfill papers from start to end date.
    Creates data/papers_all.json with deduplicated papers.
    """
    ensure_dirs()

    # Collect all papers, deduped by ID
    all_papers: Dict[str, Dict[str, Any]] = {}

    current = start
    count_fetched = 0
    count_papers = 0

    print(f"Backfilling papers from {start.date()} to {end.date()}...")

    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        papers = fetch_papers_for_date(current, force=force)

        if papers is None:
            print(f"  {date_str}: FAILED (will skip)", file=sys.stderr)
        elif papers:
            count_fetched += 1
            print(f"  {date_str}: {len(papers)} papers", file=sys.stdout)
            for paper in papers:
                extracted = extract_paper_fields(paper)
                paper_id = extracted.get("id")
                if paper_id and paper_id not in all_papers:
                    all_papers[paper_id] = extracted
                    count_papers += 1
        else:
            # Empty result (no papers for this date)
            pass

        current += timedelta(days=1)

    # Write deduplicated archive
    papers_list = list(all_papers.values())
    output_file = DATA_DIR / "papers_all.json"
    with open(output_file, 'w') as f:
        json.dump(papers_list, f, indent=2)

    print(f"\nBackfill complete:")
    print(f"  Dates fetched: {count_fetched}")
    print(f"  Unique papers: {count_papers}")
    print(f"  Output: {output_file}")

if __name__ == "__main__":
    force = "--force" in sys.argv or "--refresh" in sys.argv
    backfill_papers(force=force)
