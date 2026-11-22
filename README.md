# savr

**Top-rated recipes, simplified, straight to a pickup order.**

savr is a Python (Flask) web app. Users browse a curated library of well-reviewed recipes, each cut down to short action steps. They can get picks based on their mood, plan a week, track their pantry, and send one combined shopping list to a grocery store for pickup. It runs in Docker on any port and installs on iPhone as a home-screen app.

## What users get

| Feature | What it does |
|---|---|
| **Browse** | Filter by meal, cuisine, total time, hands-on time, difficulty (1–5), diet/allergens, and ingredients to leave out. |
| **Simplified steps** | Each step is tagged Preheat / Cut / Mix / Cook / Bake / Rest / Serve, has a timer if it includes a time, and comes with an equipment list. |
| **Two writing styles** | **Standard** reads like a normal recipe. **Unhinged 🔥** gives the same steps, numbers and temperatures with a lot more swearing ("Crank the damn oven to 425°F"). One toggle switches it everywhere and the choice is remembered. |
| **Pick for me** | Type how you feel ("wiped out, cozy dinner under 45 min, no dairy") and get top picks, each with the reasons it was chosen. |
| **Meal planner** | Plans 3–7 nights that fit your request, uses different cuisines and proteins, and pairs recipes that share fresh ingredients so less goes to waste. You can swap any single night. One tap puts the whole week in the cart. |
| **Pantry** | Track what you have, with optional use-by dates. Shopping lists skip what you already have, "What you can make" puts expiring items first, and "Mark as bought" fills the pantry after a shopping trip. |
| **Household ratings** | Thumbs up/down per person, shared across a household. Thumbs-down recipes are never suggested again. Liked recipes, cuisines and proteins move similar recipes up in picks and plans. |
| **Cook mode** | Full screen, one big step at a time. Swipe or use the arrow keys, the screen stays on, steps can be read aloud (in either style), voice commands work ("next", "back", "repeat", "timer") where the browser supports them, and there's an ingredient drawer. |
| **Pickup orders** | One combined, scaled list grouped by aisle. **Kroger sign-in** matches products at your chosen store and fills the pickup cart (also Kroger-owned stores such as King Soopers, Ralphs, Fred Meyer and Smith's). **Walmart** (when its API is set up) matches each item to a Walmart product and opens one add-to-cart link for pickup or delivery; otherwise it gives Walmart search links. Other options: Instacart shopping-list link, Target search links, CSV, checklist, share sheet. |

**Households** use a shared code (e.g. `basil-otter-4821`) instead of accounts. A phone joins by entering the code, and the household's ratings, pantry and plan are stored on the server. Treat the code like a Wi-Fi password.

---

## Run it

```bash
cp .env.example .env              # set SECRET_KEY at minimum
docker compose up -d --build      # → http://localhost:8000  (HOST_PORT=8080 to change)
```
If you run the image yourself instead (e.g. Docker Desktop's ▶ Run), publish the port, e.g. `docker run -p 8000:8000 -e SECRET_KEY=… -v savr_savr-data:/data savr:latest`. Without `-p` the container runs but your browser can't reach it, and the installed app shows its offline copy (with a red "Offline" bar).

The recipe library ships inside the image. Household data is stored in SQLite on the `savr-data` volume and survives library updates.

Locally, without Docker:
```bash
pip install -r requirements-dev.txt
python wsgi.py                    # :8000 — reads .env automatically
pytest -q
```

### Configuration
| Variable | Needed for |
|---|---|
| `SECRET_KEY` | Required. Signs the Kroger sign-in session. |

Settings live in `.env` (git- and docker-ignored). Docker Compose reads it, and so does `python wsgi.py`; real environment variables override it.
| `COOKIE_SECURE=1` | When you serve over HTTPS. |
| `KROGER_CLIENT_ID`, `KROGER_CLIENT_SECRET`, `KROGER_REDIRECT_URI` | Kroger pickup cart. Create an app at developer.kroger.com with scopes `product.compact` and `cart.basic:write`. The redirect URI must be `https://<your host>/kroger/callback`. Kroger buttons stay hidden until these are set. |
| `WALMART_CONSUMER_ID`, `WALMART_KEY_VERSION`, `WALMART_PRIVATE_KEY` (or `WALMART_PRIVATE_KEY_PATH`), `WALMART_PUBLISHER_ID` | Walmart cart link (Walmart I/O). Upload your **public** key at walmart.io, then set the consumer ID and key version it shows. Give savr the **private** key: for Docker, put it in `.env` on one line with `WALMART_PRIVATE_KEY=$(base64 -i WM_IO_private_key.pem | tr -d '\n')`; without Docker, `WALMART_PRIVATE_KEY_PATH=/path/to/WM_IO_private_key.pem` also works. Use the unencrypted key (`BEGIN PRIVATE KEY`) or also set `WALMART_PRIVATE_KEY_PASSWORD`. The publisher ID is optional (affiliate credit). The app runs a test search (rechecked every 15 minutes, or 2 minutes after a failure) and uses Walmart search links when the API isn't working. Key files are git-ignored; keep them out of the repo anyway. |
| `INSTACART_API_KEY`, `INSTACART_ENV` | Instacart shopping-list links (apply for the Instacart Developer Platform). Without a key, the app shows the request body instead. |
| `ANTHROPIC_API_KEY`, `SAVR_REWRITE_MODEL` | **Developer CLI only.** Claude rewrite of steps and voices. Default model `claude-haiku-5-5`. |
| `MIN_RATING` / `MIN_REVIEWS` | Defaults for the scraper (4.0 / 5). |
| `DATABASE_PATH`, `LIBRARY_PATH`, `LIBRARY_SYNC` | Storage locations. Leave these alone normally. |

---

## Updating recipes (developer only)

Users never scrape. The library is a flat file, `app/data/library.json`, which you update with the CLI. When the app starts, it loads any changes into its database (`python -m app.cli sync` does it immediately). Recipe IDs stay the same, so household ratings and plans keep working after an update.

```bash
python -m app.cli scrape https://site.com/recipes/weeknight/ --max 25   # recipe pages, listing pages, sitemaps
python -m app.cli scrape --from-file sources.txt --min-rating 4.5
python -m app.cli scrape-sites     # many sites at once from recipe_sites.txt (see below)
python -m app.cli refresh          # re-check ratings; drop recipes that fell below the bar
python -m app.cli rewrite --llm    # Claude: tighter steps + equipment + both voices
python -m app.cli remove "Recipe Name"
python -m app.cli samples          # rebuild the 40 bundled original recipes
python -m app.cli prune --min-score 4.2 --dedupe --dry-run   # raise the bar / drop duplicate dishes, no re-scrape
python -m app.cli stats
```

### Scraping many sites at once

`recipe_sites.txt` (repo root) lists 46 popular recipe sites: Allrecipes, Simply Recipes, Serious Eats, Food Network, Epicurious, Bon Appétit, Taste of Home, BBC Good Food, King Arthur, Budget Bytes, RecipeTin Eats, Sally's Baking Addiction and more. `scrape-sites` reads it (or any file you pass) and gives each site its own budget, so one big site can't use up the whole run. It takes turns between sites one page at a time, which spreads requests out so the per-site delay rarely slows the run.

```bash
python -m app.cli scrape-sites --list                                     # what would run
python -m app.cli scrape-sites                                            # 10 recipes / 100 pages per site
python -m app.cli scrape-sites --per-site 25 --max-pages-per-site 80 --max-total 300
python -m app.cli scrape-sites --only allrecipes.com,seriouseats.com      # or --skip food.com
python -m app.cli scrape-sites my_sites.txt --min-rating 4.5
python -m app.cli scrape-sites --min-score 4.2                            # review-weighted bar (see below)
python -m app.cli scrape-sites --allow-unrated-from kingarthurbaking.com  # sites that publish no ratings
```

File format: one start URL per line (homepage, listing page, sitemap `.xml`, or a recipe). A bare domain like `allrecipes.com` means its homepage, and `#` starts a comment. Lines for the same site share that site's budget. The run prints each result as it goes, saves the library every 10 recipes, and ends with a per-site summary: added / updated / replaced / duplicate, `known` (already in the library, skipped without fetching), `low_rating`, `unrated` (the site publishes no rating; allow it per site with `--allow-unrated-from`), `incomplete` (fewer than `--min-ingredients` ingredients, no instructions, or no image unless `--allow-no-image`), `no_recipe`, `blocked` (robots.txt), `error_<HTTP code>`, and `gave_up` when a site refused 6 requests in a row.

**Getting more recipes per run.** Pages are fetched in priority order: start URLs, recipe sitemaps, single-recipe pages, other pages, then listing pages, so a site's budget goes to recipes rather than category pages. Sitemaps listed in the site's robots.txt are used automatically (`--no-sitemaps` turns that off) and tag/category/author sitemaps are skipped. Recipes already in the library are skipped and don't use the page budget (`--refresh-known` re-fetches them; `refresh` re-checks ratings). On a 429 or 503 the scraper waits (honouring Retry-After), retries twice and slows down for that site.

**Quality.** Besides `--min-rating` and `--min-reviews`, `--min-score` sets a bar on the review-weighted score, which pulls a rating toward 4.0 until it has about 20 reviews (4.9 from 6 reviews scores 4.21; 4.6 from 19,000 scores 4.60). "Top rated" and the picks use the same score. When a scraped dish matches one already in the library (same name ignoring words like "best", "easy", "recipe"), only the higher-scoring one is kept. `prune` applies a new bar or de-duplicates the existing library without re-scraping; the bundled samples are never pruned.

To refresh on a schedule, put your sources in `sources.txt` (see `sources.txt.example`) and run `scripts/update-library.sh` from cron, e.g. weekly. Then commit `library.json` and redeploy.

To run the CLI through Docker and write the result back into your repo:
```bash
docker compose run --rm --user "$(id -u)" -v "$PWD:/repo" -w /repo savr python -m app.cli scrape --from-file sources.txt
```

**How the scraper works.** It reads the schema.org recipe data that most recipe sites publish for Google, so the long stories never come along. It respects robots.txt, waits between requests (1.5 s per site by default), keeps only recipes with at least `MIN_RATING` stars and `MIN_REVIEWS` ratings (10- and 100-point scales are converted to 5), and credits and links the original on every recipe. Check each site's terms of service.

**How the rewrite works.** It runs during the update, never while users wait.
- **Rules (always available, offline):** detects equipment by keyword, rewrites the opening verb ("Chop the hell out of the onion"), swaps stock phrases ("until golden" becomes "until it's golden as hell"), and adds an action-specific opener or closer. Every number survives (tests enforce this across the whole library).
- **Claude (`--llm`):** merges and reorders steps, lists equipment, and writes both styles. The result is checked before it's kept: every time, temperature and quantity must appear in both styles, and actions must be valid. If anything fails, that recipe falls back to the rule-based version and the CLI prints why. Claude results are kept across `refresh` unless the source instructions change.

> The bundled library has 40 **original** recipes written for savr, with sample ratings. I couldn't scrape real sites from the build environment because its network blocks them. Run `scrape` on your machine to add real ones.

---

## Architecture

```
app/
  scraper.py      polite fetcher, schema.org parsing, rating filter, crawling (CLI only)
  pipeline.py     raw recipe → clean record (steps, times, difficulty, tags, allergens)
  simplifier.py   step splitting, chatter removal, action tags, time + difficulty estimates
  rewrite.py      equipment, standard/unhinged voices (rules + Claude with validation)
  tagger.py       meal type, cuisine, style, allergen/diet detection
  library.py      library.json read/write + hash-based sync into SQLite
  cli.py          developer commands
  recommender.py  mood parsing, scoring, household taste profile, pantry signals
  planner.py      weekly plan (fit + shared perishables − repetition)
  pantry.py       pantry ↔ ingredient matching, "what can I make"
  households.py   codes, ratings, pantry, plans, Kroger tokens
  kroger.py       OAuth, store lookup, product → UPC matching, cart add (PICKUP)
  grocery.py      combine/scale/convert lists, aisles, Instacart/CSV/text/links
  api.py, web.py  JSON API + server-rendered pages
```

### iOS
- **Now:** in Safari, Share → **Add to Home Screen** gives a full-screen app with a bottom tab bar. Recipes you've opened work offline, and cook mode keeps the screen on.
- **Native later:** every feature is in the JSON API. Send the household code as an `X-Household` header. Endpoints: `/api/recipes`, `/api/recipes/{id}`, `/api/categories`, `/api/recommend`, `/api/plan` (+ `/swap`), `/api/pantry` (+ `/suggest`, `/bought`), `/api/ratings`, `/api/grocery`, `/api/kroger/*`, `/api/household/*`. Wrapping the PWA with Capacitor is the fastest path to the App Store.

### Notes and limits
- Allergen and diet tags come from ingredient names only. The app says this on every recipe.
- Kroger's public API can add items to a cart but can't read the cart, so the user reviews it and checks out in the Kroger app.
- Voice commands depend on the browser's speech recognition (Chrome and Safari 14.5+). The button hides itself where it isn't supported.
