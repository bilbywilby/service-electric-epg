#!/usr/bin/env python3
"""
One-time setup helper: run this locally (not in CI) to find your exact
Schedules Direct lineup ID for Service Electric in Lehigh County, PA.

Schedules Direct has no lookup by provider name, only by postal code, so
this queries /headends for the zip code(s) you give it and filters the
results client-side. Lehigh County towns served by Service Electric span
several zip codes (Allentown 18101-18109, Bethlehem 18015/18017/18018/18020,
Easton 18042/18045, Emmaus 18049, etc.) -- if your first zip doesn't surface
a "Service Electric" headend, try a neighboring one.

Usage:
    python scripts/discover_lineup.py --postal-code 18101
    python scripts/discover_lineup.py --postal-code 18101 --provider-filter "service electric"
    python scripts/discover_lineup.py --postal-code 18101 --add USA-PAxxxxx-X

The SD_USERNAME / SD_PASSWORD environment variables are read if set;
otherwise you're prompted (password entry is hidden).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

try:
    from .sdclient import SDClient, SDError
except ImportError:
    from sdclient import SDClient, SDError

USER_AGENT = "service-electric-epg-discovery/1.0 (+https://github.com/)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--postal-code", required=True, help="5-digit US zip code, e.g. 18101 (Allentown)")
    parser.add_argument("--country", default="USA", help="3-letter country code (default: USA)")
    parser.add_argument(
        "--provider-filter",
        default="service electric",
        help="Case-insensitive substring to match against lineup names (default: 'service electric')",
    )
    parser.add_argument(
        "--add",
        metavar="LINEUP_ID",
        help="Skip the search and directly PUT this lineup ID onto your account, e.g. USA-PA12345-X",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    username = os.environ.get("SD_USERNAME") or input("Schedules Direct username (email): ").strip()
    password = os.environ.get("SD_PASSWORD") or getpass.getpass("Schedules Direct password: ")

    client = SDClient(username=username, password=password, user_agent=USER_AGENT)

    try:
        client.authenticate()
    except SDError as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return 1

    if args.add:
        try:
            client.add_lineup(args.add)
        except SDError as exc:
            print(f"Could not add lineup {args.add}: {exc}", file=sys.stderr)
            return 1
        print(f"Added lineup {args.add} to your account.")
        print("Set this as the SD_LINEUP_ID repository variable / secret:")
        print(f"  {args.add}")
        return 0

    try:
        headends = client.get_headends(args.country, args.postal_code)
    except SDError as exc:
        print(f"Lookup failed: {exc}", file=sys.stderr)
        return 1

    if not headends:
        print(f"No headends returned for {args.country} {args.postal_code}.")
        return 1

    matches: list[tuple[str, str, str, str]] = []  # (lineup_id, lineup_name, headend, location)
    for headend in headends:
        for lineup in headend.get("lineups", []):
            name = lineup.get("name", "")
            if args.provider_filter.lower() in name.lower():
                matches.append(
                    (lineup["lineup"], name, headend.get("headend", ""), headend.get("location", ""))
                )

    if not matches:
        print(
            f"No lineup name in {args.country} {args.postal_code} matched "
            f"'{args.provider_filter}'. All lineups found for this zip code:\n"
        )
        for headend in headends:
            for lineup in headend.get("lineups", []):
                print(f"  {lineup['lineup']:<24} {lineup.get('name', '')}  (headend {headend.get('headend')})")
        print("\nTry a neighboring Lehigh County zip code, or re-run with --provider-filter to widen the match.")
        return 1

    print(f"Found {len(matches)} matching lineup(s) for '{args.provider_filter}':\n")
    for idx, (lineup_id, name, headend_id, location) in enumerate(matches, start=1):
        print(f"  [{idx}] {lineup_id}")
        print(f"      name:     {name}")
        print(f"      headend:  {headend_id}  ({location})\n")

    if len(matches) == 1:
        choice = 1
    else:
        raw = input(f"Select a lineup to add [1-{len(matches)}]: ").strip()
        if not raw.isdigit() or not (1 <= int(raw) <= len(matches)):
            print("Invalid selection.", file=sys.stderr)
            return 1
        choice = int(raw)

    lineup_id = matches[choice - 1][0]
    try:
        client.add_lineup(lineup_id)
    except SDError as exc:
        print(f"Could not add lineup {lineup_id}: {exc}", file=sys.stderr)
        return 1

    print(f"\nAdded lineup {lineup_id} to your Schedules Direct account.")
    print("Set this as the SD_LINEUP_ID repository variable (Settings > Secrets and variables > Actions):")
    print(f"  {lineup_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
