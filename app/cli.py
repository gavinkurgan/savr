"""savr developer CLI — the ONLY way recipes get into the app.

    python -m app.cli scrape https://site.com/recipes/dinner/ --max 25
    python -m app.cli scrape --from-file sources.txt
    python -m app.cli scrape-sites            # many sites from recipe_sites.txt
    python -m app.cli scrape-sites my_sites.txt --per-site 20 --only allrecipes.com,seriouseats.com
    python -m app.cli refresh                 # re-check ratings of everything scraped
    python -m app.cli rewrite --llm           # Claude rewrite (needs ANTHROPIC_API_KEY)
    python -m app.cli reprocess               # re-apply cleanups offline (no re-scrape)
    python -m app.cli prune --min-score 4.2 --dedupe --dry-run   # raise the bar without re-scraping
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

from .library import DEFAULT_LIBRARY, Library, is_sample, sync_library
from .pipeline import process_recipe, reprocess_recipe
from .quality import quality_score
from .rewrite import apply_rewrite
from .scraper import (DEFAULT_MIN_RATING, Checks, PoliteFetcher, crawl, crawl_sites, group_by_site,
                      parse_rating, passes_rating, read_sources, scrape_url)

SAMPLES_FILE = os.path.join(os.path.dirname(__file__), "data", "sample_recipes.json")
# Default site list for `scrape-sites`, at the repo root (/srv in the Docker image).
DEFAULT_SITES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recipe_sites.txt")


def _read_urls(args) -> list[str]:
    urls = list(args.urls or [])
    if getattr(args, "from_file", None):
        urls += read_sources(args.from_file)
    return urls


def _fetcher(args):
    return PoliteFetcher(delay_seconds=args.delay, respect_robots=not args.ignore_robots)


def _checks(args) -> Checks:
    return Checks(min_rating=args.min_rating, min_reviews=args.min_reviews, min_score=args.min_score,
                  allow_unrated=args.allow_unrated, min_ingredients=args.min_ingredients,
                  require_instructions=True, require_image=not args.allow_no_image)


def _known(args, lib: Library) -> set[str]:
    return set() if args.refresh_known else lib.urls()


def _score(r: dict) -> float:
    return quality_score(r.get("rating"), r.get("rating_count"))


def _save(lib: Library, raw: dict, url: str, args) -> tuple[str, str]:
    """Process and store one scraped recipe, keeping only the best-scoring copy of
    a dish. Returns (status, note): added | updated | duplicate | replaced."""
    rec = process_recipe(raw, url)
    dup = lib.find_duplicate(rec)
    if dup and _score(dup) >= _score(rec):
        return "duplicate", f"same dish as {dup['url']} (score {_score(dup)} >= {_score(rec)})"
    rec = apply_rewrite(rec, use_llm=args.llm)
    note = ""
    if dup:
        lib.remove(dup["url"])
        note = f"replaced {dup['url']} (score {_score(dup)} < {_score(rec)})"
    status = lib.upsert(rec)
    return ("replaced" if dup and status == "added" else status), note


def _count_key(r) -> str:
    return f"error_{r.http_status}" if r.status == "error" and r.http_status else r.status


def cmd_scrape(args, fetch=None) -> int:
    urls = _read_urls(args)
    if not urls:
        print("No URLs given.", file=sys.stderr)
        return 2
    lib = Library(args.library)
    fetch = fetch or _fetcher(args)
    results = crawl(urls, fetch, max_recipes=args.max, max_pages=args.max_pages, follow_links=not args.no_follow,
                    known=_known(args, lib), checks=_checks(args))
    counts: dict[str, int] = {}
    for r in results:
        if r.status == "saved":
            r.status, note = _save(lib, r.recipe, r.url, args)
            r.detail = " ".join(x for x in (r.detail, note) if x)
        counts[_count_key(r)] = counts.get(_count_key(r), 0) + 1
        if r.status != "known":
            print(f"{r.status:10} {r.url}  {r.detail}")
    lib.save()
    print(f"\n{counts} → {len(lib.recipes)} recipes in {args.library}")
    return 0


def cmd_scrape_sites(args, fetch=None) -> int:
    """Scrape many sites from a text file, each with its own recipe budget."""
    path = args.sites_file
    if not os.path.exists(path):
        print(f"Sites file not found: {path}", file=sys.stderr)
        return 2
    sites = group_by_site(read_sources(path))
    if args.only:
        wanted = {w.strip().lower().removeprefix("www.") for w in args.only.split(",") if w.strip()}
        sites = {s: u for s, u in sites.items() if s in wanted}
    if args.skip:
        unwanted = {w.strip().lower().removeprefix("www.") for w in args.skip.split(",") if w.strip()}
        sites = {s: u for s, u in sites.items() if s not in unwanted}
    if not sites:
        print(f"No sites to scrape in {path}.", file=sys.stderr)
        return 2
    if args.list:
        for site, urls in sites.items():
            print(f"{site:32} {len(urls)} start URL(s)")
        print(f"\n{len(sites)} sites in {path}")
        return 0

    print(f"Scraping {len(sites)} sites from {path}: up to {args.per_site} recipes"
          f" / {args.max_pages_per_site} pages each" + (f", {args.max_total} total" if args.max_total else ""))
    lib = Library(args.library)
    fetch = fetch or _fetcher(args)
    per_site: dict[str, dict[str, int]] = {}
    pending = 0

    def on_result(site, r):
        nonlocal pending
        if r.status == "saved":
            r.status, note = _save(lib, r.recipe, r.url, args)
            r.detail = " ".join(x for x in (r.detail, note) if x)
            pending += 1
            if pending >= 10:  # checkpoint long runs
                lib.save()
                pending = 0
        c = per_site.setdefault(site, {})
        c[_count_key(r)] = c.get(_count_key(r), 0) + 1
        if r.status != "known":  # skipped without fetching; counted in the summary
            print(f"{r.status:10} {r.url}  {r.detail}", flush=True)

    all_urls = [u for urls in sites.values() for u in urls]
    unrated_sites = [s for s in (args.allow_unrated_from or "").split(",") if s.strip()]
    crawl_sites(all_urls, fetch, per_site=args.per_site, max_pages_per_site=args.max_pages_per_site,
                max_total=args.max_total, follow_links=not args.no_follow, on_result=on_result,
                known=_known(args, lib), use_sitemaps=not args.no_sitemaps, checks=_checks(args),
                allow_unrated_sites=unrated_sites)
    lib.save()

    print("\nPer site:")
    totals: dict[str, int] = {}
    for site in sites:
        c = per_site.get(site, {})
        for k, v in c.items():
            totals[k] = totals.get(k, 0) + v
        summary = ", ".join(f"{k} {v}" for k, v in sorted(c.items())) or "nothing fetched"
        print(f"  {site:32} {summary}")
    print(f"\n{totals} → {len(lib.recipes)} recipes in {args.library}")
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
        if not passes_rating(rating, count, args.min_rating, args.min_reviews, min_score=args.min_score):
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


def cmd_prune(args) -> int:
    """Drop recipes below a (new) bar and duplicate dishes, without re-scraping.
    The bundled samples are never pruned."""
    lib = Library(args.library)
    drop: dict[str, str] = {}
    for r in lib.recipes:
        if is_sample(r):
            continue
        rating, n = r.get("rating"), r.get("rating_count") or 0
        if rating is None:
            if args.drop_unrated:
                drop[r["url"]] = "unrated"
            continue
        if args.min_rating is not None and rating < args.min_rating:
            drop[r["url"]] = f"rating {rating} < {args.min_rating}"
        elif n < args.min_reviews:
            drop[r["url"]] = f"{n} reviews < {args.min_reviews}"
        elif args.min_score is not None and _score(r) < args.min_score:
            drop[r["url"]] = f"score {_score(r)} < {args.min_score}"
    if args.dedupe:
        from .quality import dish_key
        best: dict[str, dict] = {}
        for r in lib.recipes:
            if is_sample(r) or r["url"] in drop:
                continue
            k = dish_key(r["name"])
            if k in best and _score(best[k]) >= _score(r):
                drop[r["url"]] = f"duplicate of {best[k]['url']}"
            else:
                if k in best:
                    drop[best[k]["url"]] = f"duplicate of {r['url']}"
                best[k] = r
    for url, why in sorted(drop.items(), key=lambda x: x[1]):
        print(f"{'would drop' if args.dry_run else 'dropped':10} {url}  {why}")
    if not args.dry_run:
        for url in drop:
            lib.remove(url)
        lib.save()
    print(f"\n{'would drop' if args.dry_run else 'dropped'} {len(drop)}; "
          f"{len(lib.recipes) - (len(drop) if args.dry_run else 0)} recipes left in {args.library}")
    return 0


def cmd_reprocess(args) -> int:
    """Re-apply offline cleanups (cuisine, hands-off time, difficulty) to every recipe."""
    from collections import Counter
    lib = Library(args.library)
    changed: Counter = Counter()
    for rec in lib.recipes:
        before = {k: rec.get(k) for k in ("cuisine", "active_minutes", "hands_off_minutes", "difficulty")}
        reprocess_recipe(rec)
        for k, v in before.items():
            changed[k] += rec.get(k) != v
    lib.save()
    print(f"reprocessed {len(lib.recipes)} recipes in {args.library}; changed: {dict(changed)}")
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
        sp.add_argument("--min-score", type=float, default=None,
                        help="also require this review-weighted score (rating pulled toward 4.0 until ~20 reviews)")
        sp.add_argument("--allow-unrated", action="store_true", help="keep recipes that have no rating")
        sp.add_argument("--min-ingredients", type=int, default=3)
        sp.add_argument("--allow-no-image", action="store_true")
        sp.add_argument("--refresh-known", action="store_true",
                        help="re-fetch recipes already in the library instead of skipping them")

    s = sub.add_parser("scrape", help="scrape recipe/listing pages or sitemaps")
    s.add_argument("urls", nargs="*")
    s.add_argument("--from-file")
    s.add_argument("--max", type=int, default=25, help="max recipes to save")
    s.add_argument("--max-pages", type=int, default=120)
    s.add_argument("--no-follow", action="store_true")
    scrape_opts(s)
    s.set_defaults(func=cmd_scrape)

    ms = sub.add_parser("scrape-sites", help="scrape many sites listed in a text file (default: recipe_sites.txt)")
    ms.add_argument("sites_file", nargs="?", default=os.environ.get("SITES_FILE", DEFAULT_SITES_FILE),
                    help="one URL or domain per line, # for comments")
    ms.add_argument("--per-site", type=int, default=10, help="max recipes to save per site")
    ms.add_argument("--max-pages-per-site", type=int, default=100,
                    help="max pages to fetch per site (recipes already in the library don't count)")
    ms.add_argument("--allow-unrated-from", help="comma-separated domains whose unrated recipes are kept, "
                    "e.g. kingarthurbaking.com,bettycrocker.com")
    ms.add_argument("--no-sitemaps", action="store_true", help="don't use the sitemaps listed in robots.txt")
    ms.add_argument("--max-total", type=int, default=0, help="stop after this many recipes overall (0 = no limit)")
    ms.add_argument("--only", help="comma-separated domains to include, e.g. allrecipes.com,seriouseats.com")
    ms.add_argument("--skip", help="comma-separated domains to leave out")
    ms.add_argument("--list", action="store_true", help="show the sites that would be scraped and exit")
    ms.add_argument("--no-follow", action="store_true")
    scrape_opts(ms)
    ms.set_defaults(func=cmd_scrape_sites)

    r = sub.add_parser("refresh", help="re-check every scraped recipe")
    scrape_opts(r)
    r.set_defaults(func=cmd_refresh)

    w = sub.add_parser("rewrite", help="regenerate equipment + standard/unhinged voices")
    w.add_argument("--llm", action="store_true")
    w.add_argument("--model", default=os.environ.get("SAVR_REWRITE_MODEL", "claude-haiku-5-5"))
    w.add_argument("--only-missing", action="store_true", help="skip recipes already rewritten by Claude")
    w.add_argument("--limit", type=int, default=0)
    w.set_defaults(func=cmd_rewrite)

    pr = sub.add_parser("prune", help="drop recipes below a bar and duplicate dishes, without re-scraping")
    pr.add_argument("--min-rating", type=float, default=None)
    pr.add_argument("--min-reviews", type=int, default=0)
    pr.add_argument("--min-score", type=float, default=None)
    pr.add_argument("--drop-unrated", action="store_true")
    pr.add_argument("--dedupe", action="store_true", help="keep only the best-scoring recipe per dish")
    pr.add_argument("--dry-run", action="store_true", help="show what would be dropped")
    pr.set_defaults(func=cmd_prune)

    sub.add_parser("reprocess", help="re-apply cleanups (cuisine, hands-off time, difficulty) without re-scraping"
                   ).set_defaults(func=cmd_reprocess)

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
