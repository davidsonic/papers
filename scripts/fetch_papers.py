#!/usr/bin/env python3
"""
Fetch new papers from HuggingFace Daily Papers (last 7 days).
Intended to be run daily by GitHub Actions.
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
DATA_DIR = Path(__file__).parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"
API_BASE = "https://huggingface.co/api/daily_papers"

def ensure_dirs():
    """Create data directories if they don't exist."""
    DATA_DIR.mkdir(exist_ok=True)
    RAW_DIR.mkdir(exist_ok=True)

def fetch_papers_for_date(date: datetime, max_retries: int = 3) -> Optional[List[Dict[str, Any]]]:
    """Fetch papers for a specific date from HF API with caching and retries."""
    date_str = date.strftime("%Y-%m-%d")
    cache_file = RAW_DIR / f"{date_str}.json"

    # Return cached if exists
    if cache_file.exists():
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
                # No papers for this date
                with open(cache_file, 'w') as f:
                    json.dump([], f)
                return []
            elif e.code == 429:
                # Rate limited, retry with backoff
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"Rate limited for {date_str}, waiting {wait_time}s...", file=sys.stderr)
                    time.sleep(wait_time)
                else:
                    print(f"Error fetching {date_str}: HTTP 429 (rate limited)", file=sys.stderr)
                    return None
            else:
                print(f"Error fetching {date_str}: HTTP {e.code}", file=sys.stderr)
                return None
        except Exception as e:
            print(f"Error fetching {date_str}: {e}", file=sys.stderr)
            return None

    return None

def extract_paper_fields(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Extract fields from HF daily paper entry."""
    paper = entry.get("paper", {})
    return {
        "id": paper.get("id"),
        "title": paper.get("title"),
        "abstract": paper.get("summary", ""),
        "authors": paper.get("authors", []),
        "publishedDate": paper.get("publishedAt"),
        "hfUpvotes": paper.get("upvotes", 0),
        "githubRepo": paper.get("githubRepo"),
        "githubStars": paper.get("githubStars"),
        "aiSummary": paper.get("ai_summary"),
        "aiKeywords": paper.get("ai_keywords", []),
        "thumbnail": entry.get("thumbnail"),  # HF thumbnail image URL
    }

def fetch_new_papers(days_back: int = 7):
    """
    Fetch papers from the last N days.
    Merges with papers_all.json (deduplicated by ID).
    """
    ensure_dirs()

    all_papers_file = DATA_DIR / "papers_all.json"

    # Load existing papers
    existing_papers = {}
    if all_papers_file.exists():
        try:
            with open(all_papers_file) as f:
                papers_list = json.load(f)
                existing_papers = {p.get("id"): p for p in papers_list}
        except json.JSONDecodeError:
            print(f"Warning: could not parse {all_papers_file}", file=sys.stderr)

    # Fetch new papers from the last N days
    new_papers = {}
    current = datetime.now()
    count_fetched = 0
    count_new = 0

    print(f"Fetching papers from the last {days_back} days...")

    for i in range(days_back):
        date = current - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        papers = fetch_papers_for_date(date)

        if papers is None:
            print(f"  {date_str}: FAILED (will skip)", file=sys.stderr)
        elif papers:
            count_fetched += 1
            print(f"  {date_str}: {len(papers)} papers")
            for entry in papers:
                extracted = extract_paper_fields(entry)
                paper_id = extracted.get("id")
                if paper_id and paper_id not in existing_papers and paper_id not in new_papers:
                    new_papers[paper_id] = extracted
                    count_new += 1
        else:
            # Empty result (no papers for this date)
            pass

    # Merge with existing papers
    all_papers = {**existing_papers, **new_papers}
    papers_list = list(all_papers.values())

    # Save updated archive
    with open(all_papers_file, 'w') as f:
        json.dump(papers_list, f, indent=2)

    print(f"\nFetch complete:")
    print(f"  Dates fetched: {count_fetched}")
    print(f"  New papers: {count_new}")
    print(f"  Total papers in archive: {len(papers_list)}")

if __name__ == "__main__":
    days_back = 7
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            days_back = int(sys.argv[idx + 1])

    fetch_new_papers(days_back=days_back)
