"""
Thin TMDB (The Movie Database) client used by import_tmdb.py and the admin panel.

TMDB supplies metadata only — titles, posters, overviews, trailers. It never
supplies full films, so every imported title is assigned a playable sample
stream (public-domain films) via SAMPLE_STREAMS until a real stream is set.

Set the API key with the TMDB_API_KEY environment variable (v3 key from
https://www.themoviedb.org/settings/api).
"""
import os
import itertools
import requests

API_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p"
POSTER_SIZE = "w342"
BACKDROP_SIZE = "w1280"

# Public-domain / Creative Commons films hosted on the Internet Archive — safe to stream in a demo.
# (Google's old gtv-videos-bucket sample URLs now return 403, so don't use those.)
SAMPLE_STREAMS = [
    "https://archive.org/download/BigBuckBunny_124/Content/big_buck_bunny_720p_surround.mp4",
    "https://archive.org/download/ElephantsDream/ed_1024_512kb.mp4",
    "https://archive.org/download/Sintel/sintel-2048-surround.mp4",
    "https://archive.org/download/his_girl_friday/his_girl_friday_512kb.mp4",
]
_stream_cycle = itertools.cycle(SAMPLE_STREAMS)

# TMDB genre ids -> the single genre string our Content.genre column stores
GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Sci-Fi", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
}

LANGUAGES = {
    "en": "English", "hi": "Hindi", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam",
    "kn": "Kannada", "ko": "Korean", "ja": "Japanese", "fr": "French", "es": "Spanish",
    "de": "German", "it": "Italian", "zh": "Chinese", "pt": "Portuguese",
}

# Map certifications to the age_rating buckets the app already uses (U / 13+ / 16+ / 18+)
CERT_MAP = {
    "U": "U", "UA": "13+", "UA 13+": "13+", "UA 16+": "16+", "A": "18+",     # India
    "G": "U", "PG": "U", "PG-13": "13+", "R": "16+", "NC-17": "18+",          # US
}


class TMDBError(Exception):
    pass


def _api_key():
    key = os.environ.get("TMDB_API_KEY")
    if not key:
        raise TMDBError("TMDB_API_KEY is not set. Get a free v3 key at themoviedb.org/settings/api")
    return key


def _get(path, **params):
    params["api_key"] = _api_key()
    r = requests.get(f"{API_BASE}{path}", params=params, timeout=15)
    if r.status_code == 401:
        raise TMDBError("TMDB rejected the API key (401).")
    r.raise_for_status()
    return r.json()


def search_movies(query, limit=5):
    """Return lightweight search hits: [{tmdb_id, title, year, overview}]"""
    data = _get("/search/movie", query=query, include_adult="false")
    hits = []
    for m in data.get("results", [])[:limit]:
        hits.append({
            "tmdb_id": m["id"],
            "title": m.get("title"),
            "year": (m.get("release_date") or "")[:4],
            "overview": m.get("overview", ""),
        })
    return hits


def popular_movie_ids(count=20, region="IN"):
    ids, page = [], 1
    while len(ids) < count:
        data = _get("/movie/popular", page=page, region=region)
        results = data.get("results", [])
        if not results:
            break
        ids.extend(m["id"] for m in results)
        page += 1
    return ids[:count]


def _age_rating(details, preferred=("IN", "US")):
    """Pick a certification from release_dates, preferring Indian then US boards."""
    by_country = {}
    for entry in details.get("release_dates", {}).get("results", []):
        for rd in entry.get("release_dates", []):
            cert = (rd.get("certification") or "").strip()
            if cert:
                by_country.setdefault(entry["iso_3166_1"], cert)
                break
    for cc in preferred:
        if cc in by_country and by_country[cc] in CERT_MAP:
            return CERT_MAP[by_country[cc]]
    return "13+"


def _trailer_key(details):
    vids = details.get("videos", {}).get("results", [])
    for v in vids:
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            return v["key"]
    for v in vids:
        if v.get("site") == "YouTube":
            return v["key"]
    return None


def fetch_movie(tmdb_id):
    """
    Fetch one movie and shape it as a Content row dict:
    title, genre, language, age_rating, release_date, duration_min,
    tmdb_id, overview, poster_url, backdrop_url, trailer_key, video_url
    """
    d = _get(f"/movie/{tmdb_id}", append_to_response="videos,release_dates")
    genres = d.get("genres") or []
    genre = GENRES.get(genres[0]["id"], genres[0]["name"]) if genres else "Drama"
    return {
        "title": d["title"][:150],
        "genre": genre[:50],
        "language": LANGUAGES.get(d.get("original_language"), d.get("original_language", "en"))[:30],
        "age_rating": _age_rating(d),
        "release_date": d.get("release_date") or None,
        "duration_min": d.get("runtime") or 100,
        "tmdb_id": d["id"],
        "overview": d.get("overview") or "",
        "poster_url": f"{IMG_BASE}/{POSTER_SIZE}{d['poster_path']}" if d.get("poster_path") else None,
        "backdrop_url": f"{IMG_BASE}/{BACKDROP_SIZE}{d['backdrop_path']}" if d.get("backdrop_path") else None,
        "trailer_key": _trailer_key(d),
        "video_url": next(_stream_cycle),
    }


CONTENT_COLUMNS = [
    "title", "genre", "language", "age_rating", "release_date", "duration_min",
    "tmdb_id", "overview", "poster_url", "backdrop_url", "trailer_key", "video_url",
]

UPSERT_SQL = f"""
    INSERT INTO Content ({", ".join(CONTENT_COLUMNS)})
    VALUES ({", ".join(["%s"] * len(CONTENT_COLUMNS))})
    ON CONFLICT (tmdb_id) DO UPDATE SET
        title = EXCLUDED.title, genre = EXCLUDED.genre, language = EXCLUDED.language,
        age_rating = EXCLUDED.age_rating, release_date = EXCLUDED.release_date,
        duration_min = EXCLUDED.duration_min, overview = EXCLUDED.overview,
        poster_url = EXCLUDED.poster_url, backdrop_url = EXCLUDED.backdrop_url,
        trailer_key = EXCLUDED.trailer_key,
        video_url = COALESCE(Content.video_url, EXCLUDED.video_url)
    RETURNING content_id
"""


def upsert_content(cur, movie, region="IN", license_years=2):
    """
    Insert (or refresh) a Content row for a fetched movie and make sure it has
    an active license in `region`. Returns content_id.
    """
    cur.execute(UPSERT_SQL, tuple(movie[c] for c in CONTENT_COLUMNS))
    row = cur.fetchone()
    # Works with both plain tuple cursors (import_tmdb.py) and dict cursors (app.py)
    content_id = row["content_id"] if isinstance(row, dict) else row[0]
    cur.execute(
        "SELECT 1 FROM Licenses WHERE content_id = %s AND region = %s "
        "AND CURRENT_DATE BETWEEN license_start AND license_end",
        (content_id, region),
    )
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO Licenses (content_id, region, license_start, license_end) "
            "VALUES (%s, %s, CURRENT_DATE, CURRENT_DATE + (%s * INTERVAL '1 year'))",
            (content_id, region, license_years),
        )
    return content_id
