#!/usr/bin/env python3
"""
Fetch and populate organization data for papers from HuggingFace Papers API
Run this script to enrich your papers_all.json with organization information

Usage:
    python3 fetch_organizations.py                    # Fetch all missing data
    python3 fetch_organizations.py --start 2026-09-01 # Start from specific date
    python3 fetch_organizations.py --limit 50          # Fetch only 50 dates
    python3 fetch_organizations.py --delay 2           # 2 second delay between requests
"""

import json
import urllib.request
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta

def fetch_organizations(
    json_file='data/papers_all.json',
    start_date=None,
    limit=None,
    delay=1.0,
    verbose=True
):
    """
    Fetch organization data from HuggingFace Papers API and update papers JSON

    Args:
        json_file: Path to papers_all.json
        start_date: Start fetching from this date (YYYY-MM-DD). If None, starts from newest
        limit: Maximum number of dates to process. If None, processes all
        delay: Delay in seconds between API requests (default: 1.0)
        verbose: Print detailed output
    """

    papers_path = Path(json_file)

    if not papers_path.exists():
        print(f'Error: {papers_path} not found')
        return False

    print(f'📂 Loading {papers_path}...')
    with open(papers_path, 'r') as f:
        papers = json.load(f)

    print(f'✅ Loaded {len(papers)} papers\n')

    # Get unique dates
    dates = sorted(set(p['publishedDate'].split('T')[0] for p in papers), reverse=True)
    print(f'📅 Found {len(dates)} unique dates')

    # Filter by start_date if provided
    if start_date:
        try:
            start = datetime.strptime(start_date, '%Y-%m-%d')
            dates = [d for d in dates if datetime.strptime(d, '%Y-%m-%d') >= start]
            print(f'🔍 Filtered to {len(dates)} dates from {start_date} onwards')
        except ValueError:
            print(f'❌ Invalid date format: {start_date} (use YYYY-MM-DD)')
            return False

    # Limit number of dates
    if limit:
        dates = dates[:limit]
        print(f'⏸️  Limited to first {limit} dates\n')
    else:
        print()

    updated_count = 0
    total_processed = 0
    skipped_count = 0
    error_count = 0

    start_time = time.time()

    for i, date in enumerate(dates, 1):
        try:
            # Fetch from HuggingFace API
            url = f'https://huggingface.co/api/papers?date={date}&limit=100'

            if verbose:
                print(f'[{i}/{len(dates)}] Fetching {date}... ', end='', flush=True)

            with urllib.request.urlopen(url, timeout=15) as response:
                hf_papers = json.loads(response.read().decode())

            # Build organization map
            org_map = {}
            for hf_paper in hf_papers:
                if 'organization' in hf_paper and hf_paper['organization']:
                    org_map[hf_paper['id']] = hf_paper['organization']

            if verbose:
                print(f'{len(org_map)} orgs found', end='')

            # Update papers with organization data
            updated_this_date = 0
            for paper in papers:
                if paper['publishedDate'].split('T')[0] == date:
                    total_processed += 1
                    if 'organization' not in paper and paper['id'] in org_map:
                        paper['organization'] = org_map[paper['id']]
                        updated_count += 1
                        updated_this_date += 1

            if verbose:
                if updated_this_date > 0:
                    print(f' ✅ ({updated_this_date} updated)')
                else:
                    print(f' (no new updates)')

            # Delay to avoid rate limiting
            if i < len(dates):  # Don't delay after last request
                time.sleep(delay)

        except urllib.error.HTTPError as e:
            error_count += 1
            if verbose:
                if e.code == 429:
                    elapsed = time.time() - start_time
                    print(f'⚠️  Rate limited (429)')
                    print(f'\n⏸️  Rate limited! Please wait before running again.')
                    print(f'   Processed {i} of {len(dates)} dates')
                    print(f'   Successfully updated {updated_count} papers')
                    print(f'   Consider increasing --delay (currently {delay}s)')
                    break
                else:
                    print(f'❌ Error {e.code}')
        except Exception as e:
            error_count += 1
            if verbose:
                print(f'❌ Error: {str(e)[:50]}...')

    # Save updated papers
    print(f'\n💾 Saving updated papers...')
    with open(papers_path, 'w') as f:
        json.dump(papers, f, indent=2)

    # Summary
    elapsed = time.time() - start_time
    print(f'\n{"="*60}')
    print(f'✅ Complete!')
    print(f'{"="*60}')
    print(f'⏱️  Time elapsed: {elapsed:.1f}s')
    print(f'📊 Papers processed: {total_processed}')
    print(f'✨ Papers updated: {updated_count}')
    print(f'❌ Errors: {error_count}')
    if total_processed > 0:
        print(f'📈 Success rate: {(updated_count / total_processed * 100):.1f}%')
    print(f'{"="*60}')

    return True

def main():
    parser = argparse.ArgumentParser(
        description='Fetch organization data for papers from HuggingFace Papers API',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python3 fetch_organizations.py                           # Fetch all dates
  python3 fetch_organizations.py --limit 50                # Fetch first 50 dates
  python3 fetch_organizations.py --start 2026-09-01        # Start from Sept 1, 2026
  python3 fetch_organizations.py --delay 2 --limit 100     # 2s delay, 100 dates max
  python3 fetch_organizations.py --limit 10 --no-verbose   # Quiet mode
        '''
    )

    parser.add_argument(
        '--file', '-f',
        default='data/papers_all.json',
        help='Path to papers_all.json (default: data/papers_all.json)'
    )
    parser.add_argument(
        '--start', '-s',
        help='Start from this date (YYYY-MM-DD). If None, starts from newest'
    )
    parser.add_argument(
        '--limit', '-l',
        type=int,
        help='Maximum number of dates to process (default: all)'
    )
    parser.add_argument(
        '--delay', '-d',
        type=float,
        default=1.0,
        help='Delay in seconds between API requests (default: 1.0, increase if rate limited)'
    )
    parser.add_argument(
        '--no-verbose',
        action='store_true',
        help='Suppress verbose output'
    )

    args = parser.parse_args()

    fetch_organizations(
        json_file=args.file,
        start_date=args.start,
        limit=args.limit,
        delay=args.delay,
        verbose=not args.no_verbose
    )

if __name__ == '__main__':
    main()
