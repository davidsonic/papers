#!/usr/bin/env python3
"""
Summarize and tag a subset of papers using Claude API.
Runs Claude on each paper to generate a summary and extract keywords.
Maintains a canonical tag taxonomy to keep tags consistent.
"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional
import urllib.request
import urllib.error

# Configuration
DATA_DIR = Path(__file__).parent.parent / "data"
API_KEY = os.environ.get("ANTHROPIC_API_KEY")

def load_json(path: Path) -> Any:
    """Load JSON file, return None if not found."""
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)

def save_json(path: Path, data: Any):
    """Save JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def call_claude(prompt: str, temperature: float = 0.7) -> Optional[str]:
    """
    Call Claude API with a prompt.
    Returns the assistant's text response, or None on error.
    """
    if not API_KEY:
        print("Error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return None

    import json as json_lib

    payload = {
        "model": "claude-haiku-4-5",  # Cheaper model for tagging
        "max_tokens": 1024,
        "temperature": temperature,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ]
    }

    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    url = "https://api.anthropic.com/v1/messages"

    try:
        req = urllib.request.Request(
            url,
            data=json_lib.dumps(payload).encode('utf-8'),
            headers=headers,
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json_lib.loads(response.read().decode())
            if 'content' in result and len(result['content']) > 0:
                return result['content'][0]['text']
            return None
    except Exception as e:
        print(f"Error calling Claude API: {e}", file=sys.stderr)
        return None

def summarize_paper(paper: Dict[str, Any], existing_tags: List[str]) -> Optional[Dict[str, Any]]:
    """
    Use Claude to generate summary and tags for a paper.
    Returns {summary, tags, primaryCategory} or None on error.
    """
    title = paper.get("title", "")
    abstract = paper.get("abstract", "")[:2000]  # Truncate to avoid token limits

    # Use HF's tags if available, as initial suggestions
    hf_tags = paper.get("aiKeywords", [])

    tags_list_str = ", ".join(existing_tags[:30])  # Show first 30 canonical tags

    prompt = f"""You are a research paper categorizer. Given a paper's title and abstract, generate:
1. A 2-3 sentence summary emphasizing the key contribution
2. 3-5 keyword tags that categorize the paper

Existing canonical tags: {tags_list_str}

Paper:
Title: {title}
Abstract: {abstract}

IMPORTANT: Reuse existing tags from the list above when they fit. Only create new tags if none of the existing tags are appropriate.

Respond in this JSON format (no markdown, just raw JSON):
{{
  "summary": "2-3 sentence summary of the paper's key contribution",
  "tags": ["tag1", "tag2", "tag3"],
  "primaryCategory": "the most relevant single tag"
}}
"""

    response = call_claude(prompt)
    if not response:
        return None

    # Try to parse JSON from response
    try:
        # Find JSON in the response (in case there's extra text)
        start = response.find('{')
        end = response.rfind('}') + 1
        if start != -1 and end > start:
            json_str = response[start:end]
            result = json.loads(json_str)
            return result
    except json.JSONDecodeError:
        print(f"Failed to parse JSON for '{title[:50]}...': {response[:200]}", file=sys.stderr)
        return None

    return None

def extract_all_tags(papers: List[Dict[str, Any]]) -> List[str]:
    """Extract all unique tags from a list of papers."""
    tags = set()
    for paper in papers:
        if "tags" in paper:
            tags.update(paper["tags"])
        if "aiKeywords" in paper:
            tags.update(paper["aiKeywords"])
    return sorted(list(tags))

def summarize_papers(paper_ids: List[str], refresh: bool = False):
    """
    Summarize a list of papers by ID.
    Loads papers_all.json, processes specified IDs, writes papers_prototype.json.
    """
    all_papers_file = DATA_DIR / "papers_all.json"
    prototype_file = DATA_DIR / "papers_prototype.json"
    tags_file = DATA_DIR / "tags.json"

    # Load all papers
    all_papers = load_json(all_papers_file)
    if not all_papers:
        print(f"Error: {all_papers_file} not found", file=sys.stderr)
        return

    # Load existing prototype and tags if available
    prototype_papers = load_json(prototype_file) or {}
    existing_tags = load_json(tags_file) or []

    # Create a map of papers by ID for quick lookup
    papers_by_id = {p.get("id"): p for p in all_papers}

    # Initialize tag set
    all_tags_set = set(existing_tags) if isinstance(existing_tags, list) else set()

    # Process specified paper IDs
    for paper_id in paper_ids:
        if paper_id not in papers_by_id:
            print(f"Paper {paper_id} not found in papers_all.json", file=sys.stderr)
            continue

        # Skip if already summarized and not forcing refresh
        if isinstance(prototype_papers, dict) and paper_id in prototype_papers and not refresh:
            print(f"  {paper_id}: already summarized, skipping")
            continue

        if isinstance(prototype_papers, dict) and paper_id in prototype_papers:
            print(f"  {paper_id}: re-summarizing...")
        else:
            print(f"  {paper_id}: summarizing...")

        paper = papers_by_id[paper_id]

        # Summarize using Claude
        result = summarize_paper(paper, sorted(list(all_tags_set)))
        if result:
            # Merge result into paper
            paper_with_summary = {**paper, **result}

            # Update tags set
            if "tags" in result:
                all_tags_set.update(result["tags"])

            # Store in prototype map
            if isinstance(prototype_papers, dict):
                prototype_papers[paper_id] = paper_with_summary
            else:
                prototype_papers = {paper_id: paper_with_summary}
        else:
            print(f"  {paper_id}: summarization failed, skipping", file=sys.stderr)

    # Convert prototype map to list for compatibility
    if isinstance(prototype_papers, dict):
        prototype_list = list(prototype_papers.values())
    else:
        prototype_list = prototype_papers

    # Save results
    save_json(prototype_file, prototype_list)
    save_json(tags_file, sorted(list(all_tags_set)))

    print(f"\nSummarization complete:")
    print(f"  Papers in prototype: {len(prototype_list)}")
    print(f"  Unique tags: {len(all_tags_set)}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: summarize_papers.py <paper_id1> [<paper_id2> ...]", file=sys.stderr)
        print("Or: summarize_papers.py --refresh <paper_id1> [...]", file=sys.stderr)
        sys.exit(1)

    refresh = "--refresh" in sys.argv
    if refresh:
        sys.argv.remove("--refresh")

    paper_ids = sys.argv[1:]
    summarize_papers(paper_ids, refresh=refresh)
