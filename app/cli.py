"""savr developer CLI — the ONLY way recipes get into the app.

    python -m app.cli scrape https://site.com/recipes/dinner/ --max 25
    python -m app.cli scrape --from-file sources.txt
    python -m app.cli refresh                 # re-check ratings of everything scraped
    python -m app.cli rewrite --llm           # Claude rewrite (needs ANTHROPIC_API_KEY)
    python -m app.cli samples                 # (re)build the bundled original recipes
    python -m app.cli remove "Soggy Casserole"
    python -m app.cli stats
    python -m app.cli sync                    # push library.json into the app database

Every command edits app/data/library.json (or --library PATH); the running app
picks up the change on its next start (or immediately with `sync`).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .library import DEFAULT_LIBRARY, Library, sync_library
from .pipeline import process_recipe
from .rewrite import apply_rewrite
from .scraper import DEFAULT_MIN_RATING, PoliteFetcher, crawl, parse_rating, passes_rating, scrape_url

SAMPLES_FILE = os.path.join(os.path.dirname(__file__), "data", "sample_recipes.json")


def _read_urls(args) -> list[str]:
    urls = list(args.urls or [])
    if getattr(args, "from_file", None):
        with open(args.from_file, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    return urls


def _fetcher(args):
    return PoliteFetcher(delay_seconds=args.delay, respect_robots=not args.ignore_robots)


def cmd_scrape(args, fetch=None) -> int:
    urls = _read_urls(args)
    if not urls:
        print("No URLs given.", file=sys.stderr)
        return 2
    lib = Library(args.library)
    fetch = fetch or _fetcher(args)
    results = crawl(urls, fetch, min_rating=args.min_rating, min_reviews=args.min_reviews,
                    max_recipes=args.max, max_pages=args.max_pages, follow_links=not args.no_follow)
    counts: dict[str, int] = {}
    for r in results:
        if r.status == "saved":
            rec = apply_rewrite(process_recipe(r.recipe, r.url), use_llm=args.llm)
            r.status = lib.upsert(rec)
        counts[r.status] = counts.get(r.status, 0) + 1
        print(f"{r.status:10} {r.url}  {r.detail}")
    lib.save()
    print(f"\n{counts} → {len(lib.recipes)} recipes in {args.library}")
    return 0


def cmd_refresh(args, fetch=None) -> int:
    """Re-scrape every web recipe: update ratings/content, drop ones that fell below the bar."""
    lib = Library(args.library)
    fetch = fetch or _fetcher(args)
    dropped = updated = failed = 0
    for rec in list(lib.recipes):
        if not rec["url"].startswith("http") or "sample.local" in rec["url"]:
            continue
        res, _ = scrape_url(rec["url"], fetch, min_rating=0, min_reviews=0, allow_unrated=True)
        if res.status != "saved":
            failed += 1
            print(f"skip      {rec['url']}  {res.status} {res.detail}")
            continue
        rating, count = parse_rating(res.recipe)
        if not passes_rating(rating, count, args.min_rating, args.min_reviews):
            lib.remove(rec["url"]); dropped += 1
            print(f"dropped   {rec['url']}  rating={rating} ({count})")
            continue
        new = process_recipe(res.recipe, rec["url"])
        if rec.get("voice_source", "").startswith("claude") and not args.llm:
            # keep the expensive rewrite if instructions didn't change
            same = [s["text"] for s in rec.get("steps_rules", [])] == [s["text"] for s in new["steps"]]
            if same:
                new.update({k: rec[k] for k in ("steps", "steps_rules", "equipment", "voice_source") if k in rec})
                lib.upsert(new); updated += 1
                continue
        lib.upsert(apply_rewrite(new, use_llm=args.llm)); updated += 1
        print(f"updated   {rec['url']}  rating={rating} ({count})")
    lib.save()
    print(f"\nupdated {updated}, dropped {dropped}, unreachable {failed}")
    return 0


def cmd_rewrite(args) -> int:
    lib = Library(args.library)
    if args.llm and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY to use --llm.", file=sys.stderr)
        return 2
    done = 0
    for rec in lib.recipes:
        if args.only_missing and rec.get("voice_source", "").startswith("claude"):
            continue
        if args.limit and done >= args.limit:
            break
        if rec.get("steps_rules"):
            rec["steps"] = rec["steps_rules"]  # always rewrite from the deterministic base
        apply_rewrite(rec, use_llm=args.llm, model=args.model)
        done += 1
        print(f"{rec['voice_source']:28} {rec['name']}" + (f"  (fallback: {rec.get('rewrite_error')})" if rec.get("rewrite_error") else ""))
        rec.pop("rewrite_error", None)
        if done % 10 == 0:
            lib.save()  # checkpoint long LLM runs
    lib.save()
    print(f"\nrewrote {done} recipes")
    return 0


def cmd_samples(args) -> int:
    with open(SAMPLES_FILE, encoding="utf-8") as f:
        raws = json.load(f)
    lib = Library(args.library)
    added = 0
    for raw in raws:
        rating, count = parse_rating(raw)
        if not passes_rating(rating, count, args.min_rating):
            continue
        slug = "".join(c if c.isalnum() else "-" for c in raw["name"].lower()).strip("-")
        rec = process_recipe(raw, f"https://sample.local/{slug}")
        rec["source"] = "savr kitchen"
        rec["author"] = rec.get("author") or "savr kitchen"
        lib.upsert(apply_rewrite(rec, use_llm=args.llm))
        added += 1
    lib.save()
    print(f"{added} sample recipes in {args.library} ({len(lib.recipes)} total)")
    return 0


def cmd_remove(args) -> int:
    lib = Library(args.library)
    n = sum(lib.remove(k) for k in args.keys)
    lib.save()
    print(f"removed {n}")
    return 0


def cmd_stats(args) -> int:
    lib = Library(args.library)
    from collections import Counter
    print(f"{len(lib.recipes)} recipes in {args.library}")
    for field in ("meal_type", "cuisine", "difficulty_label", "voice_source", "source"):
        print(f"  {field}: {dict(Counter(r.get(field) for r in lib.recipes).most_common())}")
    return 0


def cmd_sync(args) -> int:
    from .db import RecipeDB
    db_path = args.db or os.environ.get("DATABASE_PATH", os.path.join(os.getcwd(), "data", "savr.db"))
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    n = sync_library(RecipeDB(db_path), args.library, force=True)
    print(f"synced {n} recipes into {db_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="savr recipe library tools (developer only)")
    p.add_argument("--library", default=os.environ.get("LIBRARY_PATH", DEFAULT_LIBRARY))
    sub = p.add_subparsers(dest="cmd", required=True)

    def scrape_opts(sp):
        sp.add_argument("--min-rating", type=float, default=float(os.environ.get("MIN_RATING", DEFAULT_MIN_RATING)))
        sp.add_argument("--min-reviews", type=int, default=int(os.environ.get("MIN_REVIEWS", 5)))
        sp.add_argument("--delay", type=float, default=float(os.environ.get("SCRAPE_DELAY", 1.5)))
        sp.add_argument("--ignore-robots", action="store_true", help=argparse.SUPPRESS)
        sp.add_argument("--llm", action="store_true", help="rewrite with Claude (ANTHROPIC_API_KEY)")

    s = sub.add_parser("scrape", help="scrape recipe/listing pages or sitemaps")
    s.add_argument("urls", nargs="*")
    s.add_argument("--from-file")
    s.add_argument("--max", type=int, default=25, help="max recipes to save")
    s.add_argument("--max-pages", type=int, default=120)
    s.add_argument("--no-follow", action="store_true")
    scrape_opts(s)
    s.set_defaults(func=cmd_scrape)

    r = sub.add_parser("refresh", help="re-check every scraped recipe")
    scrape_opts(r)
    r.set_defaults(func=cmd_refresh)

    w = sub.add_parser("rewrite", help="regenerate equipment + standard/unhinged voices")
    w.add_argument("--llm", action="store_true")
    w.add_argument("--model", default=os.environ.get("SAVR_REWRITE_MODEL", "claude-haiku-5-5"))
    w.add_argument("--only-missing", action="store_true", help="skip recipes already rewritten by Claude")
    w.add_argument("--limit", type=int, default=0)
    w.set_defaults(func=cmd_rewrite)

    sm = sub.add_parser("samples", help="(re)build bundled original recipes")
    sm.add_argument("--min-rating", type=float, default=DEFAULT_MIN_RATING)
    sm.add_argument("--llm", action="store_true")
    sm.set_defaults(func=cmd_samples)

    rm = sub.add_parser("remove", help="remove recipes by URL or exact name")
    rm.add_argument("keys", nargs="+")
    rm.set_defaults(func=cmd_remove)

    sub.add_parser("stats").set_defaults(func=cmd_stats)
    sy = sub.add_parser("sync", help="load library.json into the app database now")
    sy.add_argument("--db")
    sy.set_defaults(func=cmd_sync)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
