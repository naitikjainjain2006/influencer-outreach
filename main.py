#!/usr/bin/env python3
"""Micro-influencer outreach pipeline CLI.

Discovery -> Filtering -> Enrichment -> AI Personalization -> Review -> Sending -> Tracking
"""
import argparse
import logging
import sys

from src.config import env, load_config
from src.discovery import discover_youtube, import_csv
from src.enrichment import run_enrichment
from src.exporter import export_all
from src.filtering import run_filter
from src.personalization import run_personalization
from src.sender import send_outreach
from src.storage import Store


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="fetch influencers")
    d.add_argument("--source", choices=["youtube", "csv"], default="youtube")
    d.add_argument("--csv", help="path to CSV (with --source csv)")
    sub.add_parser("filter", help="classify + pass/fail every influencer")
    sub.add_parser("enrich", help="enrich shortlisted influencers (email, links, audience fields)")
    p = sub.add_parser("personalize", help="generate email + DM for shortlisted influencers")
    p.add_argument("--no-llm", action="store_true", help="skip the LLM, use the template fallback")
    p.add_argument("--force", action="store_true", help="regenerate existing messages")
    r = sub.add_parser("review", help="human review gate: approve generated messages")
    r.add_argument("--approve-all", action="store_true")
    r.add_argument("--approve", nargs="*", help="influencer ids to approve")
    s = sub.add_parser("send", help="send (dry-run by default) approved outreach")
    s.add_argument("--live", action="store_true", help="really send via SMTP")
    s.add_argument("--limit", type=int)
    sub.add_parser("export", help="write dataset / messages / tracker CSVs")
    sub.add_parser("stats", help="print pipeline counts")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(a.config)
        store = Store(cfg["storage"]["db_path"])

        if a.cmd == "discover":
            if a.source == "csv":
                if not a.csv:
                    ap.error("--csv is required with --source csv")
                n = import_csv(a.csv, store)
            else:
                n = discover_youtube(cfg, store, env("YOUTUBE_API_KEY"))
            print(f"discovered/saved {n} influencers (total in DB: {len(store.influencers())})")
        elif a.cmd == "filter":
            print(run_filter(store, cfg))
        elif a.cmd == "enrich":
            print(run_enrichment(store, cfg))
        elif a.cmd == "personalize":
            print(run_personalization(store, cfg, use_llm=not a.no_llm, force=a.force))
        elif a.cmd == "review":
            ids = None if a.approve_all else a.approve
            if ids is None and not a.approve_all:
                ap.error("use --approve-all or --approve <ids>")
            print(f"approved {store.approve(ids)} messages")
        elif a.cmd == "send":
            print(send_outreach(store, cfg, live=a.live, limit=a.limit))
        elif a.cmd == "export":
            print(export_all(store))
        elif a.cmd == "stats":
            infs = store.influencers()
            print({"total": len(infs),
                   "shortlisted": sum(i["status"] == "shortlisted" for i in infs),
                   "rejected": sum(i["status"] == "rejected" for i in infs),
                   "emails_found": sum(bool(i["email"]) and i["email"] != "Not Found" for i in infs),
                   "messages": len(store.messages())})
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
