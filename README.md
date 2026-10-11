# Graylore — OTT Platform DBMS Project

A working OTT (video-on-demand) platform backend wired to five UI pages, implementing the
locked 17-feature scope with real **PostgreSQL** queries, triggers, a stored function, and
the Graylore glassmorphism UI.

## Running it

Requirements: Python 3.10+ and PostgreSQL 13+ (Postgres.app, Homebrew `postgresql@16`, or
the EDB installer all work).

```bash
# 1. Database — create it and load schema, triggers and seed data, in that order
createdb graylore
psql graylore -f schema.sql
psql graylore -f triggers.sql
psql graylore -f seed_data.sql

# 2. Python
python3 -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Point the app at your database (skip if Postgres runs locally with your OS user and no password)
export DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/graylore

# 4. Run
python app.py            # → http://127.0.0.1:5050
```

**No Postgres installed?** `pip install -r requirements-dev.txt` then `python run_local.py` starts an
embedded PostgreSQL, loads the three SQL files, and runs the app. Nothing else to set up.

Demo accounts (password for all: `password123`):

| Email | Role | Plan | Profiles |
|---|---|---|---|
| `aditi@example.com` | viewer | Standard (2 devices) | Aditi, Kiddo (Kids) |
| `rohan@example.com` | viewer | Premium (4 devices) | Rohan |
| `admin@graylore.com` | admin | Basic (1 device) | Admin |

Optional environment variables: `DATABASE_URL` (default `postgresql://localhost/graylore`),
`GRAYLORE_REGION` (viewer's region, default `IN`), `SECRET_KEY`, `TMDB_API_KEY`, `PORT`.

## Deploying (Render, free tier)

The repo carries a `render.yaml` blueprint that creates a web service and a PostgreSQL database
and wires `DATABASE_URL` between them. On first boot `init_db.py --if-empty` loads the schema,
triggers and seed data, so the live site starts with the full catalog and demo accounts.

1. Push this repo to GitHub.
2. In [Render](https://dashboard.render.com) click **New +** → **Blueprint**, pick the repo, click **Apply**.
3. Wait for the first deploy (3 to 5 minutes), then open the service URL and sign in with a demo account.

Notes: Render's free database is deleted 30 days after creation, so for anything longer than a
demo window either upgrade it or point `DATABASE_URL` at a free [Neon](https://neon.tech) database
and run `python init_db.py` once against it. The free web service sleeps after 15 minutes idle;
the first request afterwards takes about 30 seconds. The films stream from the Internet Archive,
not from the server, so video playback costs the host nothing.

`Procfile` is included too, so the same app runs unchanged on Railway or Heroku-style hosts:
set `DATABASE_URL` and `SECRET_KEY`, and the start command initialises the database itself.

## Deploying (Vercel + Neon, free tier)

Vercel runs the app as a serverless function (`api/index.py` + `vercel.json` are the adapter) and
has no built-in Postgres, so the database comes from Neon, which Vercel can create for you.

1. Push this repo to GitHub.
2. In [Vercel](https://vercel.com/new) import the repo and click **Deploy** (the defaults are right).
3. Open the project → **Storage** → **Create Database** → **Neon** → accept the free plan. Vercel
   adds `DATABASE_URL` to the project automatically.
4. **Settings → Environment Variables**: add `SECRET_KEY` (any long random string). Then
   **Deployments → Redeploy** so the new variables apply.
5. Open the site. The first request finds the empty database and loads the schema, triggers and
   seed itself (`AUTO_INIT_DB=1` is set by `api/index.py`), which takes a few seconds once.

Trade-offs versus Render: no 15-minute sleep and Neon's free database never expires, but each
request is a short-lived function, so the first hit after a quiet spell takes about a second
to warm up and the Neon database pauses after 5 idle minutes (resumes in under a second).

## Feature map

| Feature | Route | DB logic |
|---|---|---|
| Registration / Login | `/register`, `/login` | Werkzeug password hashing; new users auto-get a Basic subscription and a device session |
| Multiple profiles incl. Kids | `/profiles`, `/select_profile/<id>` | Insert/list `Profiles`; ownership check on select; **Kids profiles only see and play U-rated titles** |
| Content catalog, region licensing | `/home` | `Content` ⋈ `Licenses` filtered by region and `CURRENT_DATE BETWEEN license_start AND license_end` |
| Recommended for You | `/home` | Co-watch correlation: three-way self-join on `WatchHistory` (my titles → other viewers of them → what they also watched that I haven't), falls back to top-rated unwatched titles |
| Trending Now | `/home` | `SUM(watch_minutes)` across all profiles, grouped by title |
| Continue Watching | `/home` | `DISTINCT ON (content_id)` latest `WatchHistory` row per title, in-progress only, with a progress bar |
| **Lore, the recommendation bot** | `/bot/pick` (JSON) | Animated guide on Home. Greets the profile by name with what they left unfinished (or what similar viewers watched next), then picks by mood: `Moods` ⋈ `ContentMoods` ⋈ `Content` ⋈ `Licenses`, unwatched titles first, weighted by average rating, Kids-safe. Every title carries a hand-written `pitch` describing how it feels to watch |
| **Search** | `/search` (JSON) | Netflix-style expanding search box in the header; `ILIKE` across title, genre, overview and language, or an exact release year, with the same license and Kids filters as the catalog. Results replace the rows as you type; press `/` to focus, `Esc` to clear |
| Average ratings | `/home` | `AVG(rating)` per title joined onto every row; shown on cards and in the detail modal |
| **VOD playback** | `/play/<id>` | HTML5 `<video>` streams `Content.video_url`; license + Kids checks before playback; opens a `WatchHistory` row |
| **Resume + progress tracking** | `/progress` (JSON) | Player posts `position_sec` / `watched_sec` every 10 s, on pause and on page close; next play resumes from the saved position |
| **TMDB metadata import** | `import_tmdb.py`, `/admin/import_tmdb` | Upserts posters, backdrops, overview, trailer, genre, runtime, age rating into `Content` with `ON CONFLICT (tmdb_id)`, plus a regional `License` |
| **Device limit enforcement** | `trg_device_limit` (trigger) | Fires on `Sessions` insert: if the user is at their plan's device cap, the oldest active device is deactivated. The app checks `is_active` on every request and signs a kicked device out |
| Rate / Review | `/rate`, `/rate/delete` | `INSERT … ON CONFLICT (profile_id, content_id) DO UPDATE`; edit and delete from My Space |
| **My List (watchlist)** | `/watchlist/toggle` (JSON) | `Watchlist` table, unique per profile+title; "+ My List" button in the title popup |
| **My Space page** | `/me` | Per-profile page: My List with play/remove (flags titles not licensed in the viewer's region), My Reviews with inline edit/delete, last 20 viewing sessions with resume, and watch stats |
| Subscription / Billing | `/billing` | New `Subscriptions` row + `Payments` row; modal preselects the current plan |
| **Single active subscription** | `trg_single_active_sub` (trigger) | Fires on `Subscriptions` insert: cancels the previous active plan and stamps its `end_date` |
| Admin dashboard | `/admin` | Live stats, content table with all licenses, users with plan / devices / lifetime spend |
| **Royalty payout report** | `/admin` | `SUM(watch_minutes) × rate_per_min` computed live |
| Payout snapshots | `/admin/snapshot_payouts` | Calls the PL/pgSQL function `refresh_payouts(period, rate)` which upserts the denormalised `Payouts` reporting table |
| Add content (admin) | `/admin/add_content` | Insert into `Content` + `Licenses` |

### Trying the triggers

- **Device limit:** sign in as `admin@graylore.com` (Basic, 1 device) in a normal window, then
  again in a private/incognito window. The first window is signed out on its next click with a
  "device limit reached" message. Each browser profile counts as one device.
- **Single subscription:** change plan from the avatar menu on Home, then check
  `SELECT * FROM Subscriptions WHERE user_id = 1;` — the old row is `cancelled` with today's `end_date`.
- **Region licensing:** "Carnival of Souls", "The Phantom of the Opera", "Kansas City Confidential" and
  "Royal Wedding" are licensed for US only, so they never appear for an `IN` viewer; "The Killer Shrews"
  has an expired licence and "The Little Shop of Horrors" expires at the end of October. Run with
  `GRAYLORE_REGION=US python app.py` to see the US catalog.
- **Kids profile:** pick "Kiddo" on Aditi's account. Only U-rated titles appear, and `/play/2`
  (Night of the Living Dead, 16+) is blocked with a message.
- **Resume:** Aditi left Nosferatu at 40 minutes and Rohan left Night of the Living Dead at 35;
  both show in Continue Watching and the player resumes from that point.

## Files

- `app.py` — Flask application, all routes (psycopg2, `DATABASE_URL`)
- `tmdb.py` — TMDB API client + `Content` upsert helper
- `import_tmdb.py` — CLI importer (`--popular N`, `--search TITLE`, `--id TMDB_ID`, `--region XX`)
- `schema.sql` — 13-table schema (incl. `Moods` and the `ContentMoods` M:N) with foreign keys, checks and indexes
- `triggers.sql` — the two triggers and the `refresh_payouts()` function (PL/pgSQL)
- `seed_data.sql` — 12 users, 22 profiles, 68 real public-domain films with working streams, 125 licenses, ~375 watch rows, ~100 ratings, monthly payments
- `templates/` — the 6 Graylore pages (auth, profile, home, my space, player, admin), Jinja-driven
- `run_local.py` — optional embedded-Postgres runner for machines without a database install
- `requirements.txt` / `requirements-dev.txt` — Python dependencies

## Where the video comes from

Every title carries its own `video_url`, which the HTML5 player streams directly with range
requests, so seeking and resume work. The seeded catalog is 68 real public-domain and Creative
Commons films from 1902 to 2012 (His Girl Friday, Night of the Living Dead, Nosferatu, Metropolis,
The General, Charade, The Lady Vanishes, Battleship Potemkin, House on Haunted Hill, Big Buck
Bunny, Sintel, and more) across 13 genres and 5 languages, streamed straight from the Internet
Archive, with posters from Wikipedia. No API key or download is needed for playback.

The seed also carries 12 users, 22 profiles (5 of them Kids), subscriptions with monthly payment
history and a few plan changes, about 375 watch-history rows over six months, and about 100
ratings, so Trending, Recommended, the royalty report and the admin panels have real aggregates
to show. The seed file is generated deterministically, so every fresh load is identical.

TMDB provides metadata only (posters, synopsis, trailers), never the films, so imported titles are
given a sample stream until you set a real one. Add your own MP4 or HLS URLs via the admin
"Add Title" form.

To import real titles with posters, get a free v3 key at themoviedb.org/settings/api, then:

```bash
export TMDB_API_KEY=your_key
python import_tmdb.py --popular 20
```
