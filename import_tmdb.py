"""
Import movies from TMDB into the Graylore Content table.

Usage:
    set TMDB_API_KEY=your_key            (Windows)   /   export TMDB_API_KEY=your_key (Linux/Mac)

    python import_tmdb.py --popular 20              # 20 currently-popular movies
    python import_tmdb.py --search "Inception"      # best match for a title
    python import_tmdb.py --id 27205                # a specific TMDB movie id
    python import_tmdb.py --popular 20 --region US  # license for a different region

Each imported title gets: poster, backdrop, overview, trailer, genre, runtime,
age rating, an active license for --region, and a playable sample stream.
Re-running is safe: rows are matched on tmdb_id and refreshed in place.
"""
import argparse
import sys
import psycopg2

from app import DATABASE_URL, DEFAULT_REGION
import tmdb


def main():
    ap = argparse.ArgumentParser(description="Import TMDB movies into Graylore")
    ap.add_argument("--popular", type=int, metavar="N", help="import N popular movies")
    ap.add_argument("--search", metavar="TITLE", help="import the top search hit for TITLE")
    ap.add_argument("--id", type=int, metavar="TMDB_ID", help="import one movie by TMDB id")
    ap.add_argument("--region", default=DEFAULT_REGION, help=f"license region (default {DEFAULT_REGION})")
    args = ap.parse_args()

    ids = []
    try:
        if args.popular:
            ids += tmdb.popular_movie_ids(args.popular, region=args.region)
        if args.search:
            hits = tmdb.search_movies(args.search, limit=1)
            if not hits:
                print(f"No TMDB results for '{args.search}'")
            else:
                print(f"Matched: {hits[0]['title']} ({hits[0]['year']})")
                ids.append(hits[0]["tmdb_id"])
        if args.id:
            ids.append(args.id)
    except tmdb.TMDBError as e:
        sys.exit(f"TMDB error: {e}")

    if not ids:
        ap.print_help()
        sys.exit(1)

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    done = 0
    for tmdb_id in dict.fromkeys(ids):      # de-dupe, keep order
        try:
            movie = tmdb.fetch_movie(tmdb_id)
        except Exception as e:              # one bad id shouldn't abort the batch
            print(f"  skip {tmdb_id}: {e}")
            continue
        content_id = tmdb.upsert_content(cur, movie, region=args.region)
        conn.commit()
        done += 1
        print(f"  [{content_id:>3}] {movie['title']} ({(movie['release_date'] or '')[:4]}) "
              f"· {movie['genre']} · {movie['duration_min']} min · {movie['age_rating']}")
    cur.close()
    conn.close()
    print(f"Imported/updated {done} title(s), licensed for {args.region}.")


if __name__ == "__main__":
    main()
