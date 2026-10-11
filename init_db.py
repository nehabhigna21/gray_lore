"""
Initialise the Graylore database from the SQL files, using DATABASE_URL.

    python init_db.py              # (re)create everything: schema + triggers + seed
    python init_db.py --if-empty   # only if the schema isn't there yet (used on deploy)

Works anywhere psycopg2 can connect, no psql binary needed (Render, Railway, Neon, local).
"""
import os
import sys

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/graylore")
FILES = ("schema.sql", "triggers.sql", "seed_data.sql")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    if "--if-empty" in sys.argv:
        cur.execute("SELECT to_regclass('public.users')")
        if cur.fetchone()[0]:
            print("init_db: schema already present, nothing to do")
            return
    for name in FILES:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            cur.execute(f.read())
        print(f"init_db: loaded {name}")
    cur.execute("SELECT COUNT(*) FROM Content")
    print(f"init_db: done, {cur.fetchone()[0]} titles in catalog")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
