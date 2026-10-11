"""
Zero-install local runner: starts an embedded PostgreSQL (via the `pgserver`
package, no system install needed), loads schema + triggers + seed data into a
fresh `graylore` database, and runs the Flask app on http://127.0.0.1:5050.

    pip install -r requirements.txt -r requirements-dev.txt
    python run_local.py              # fresh DB every start
    python run_local.py --keep       # keep existing data between restarts

For a real Postgres install, skip this file and follow the README instead.
"""
import os
import subprocess
import sys

import pgserver

HERE = os.path.dirname(os.path.abspath(__file__))
PGDATA = os.path.join(HERE, ".pgdata")
KEEP = "--keep" in sys.argv

server = pgserver.get_server(PGDATA)
base_uri = server.get_uri()                      # postgresql://postgres:@/postgres?host=...
db_uri = base_uri.replace("/postgres?", "/graylore?", 1)
psql = os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "bin", "psql")

exists = "1" in server.psql("SELECT 1 FROM pg_database WHERE datname = 'graylore';")
if not KEEP or not exists:
    server.psql("DROP DATABASE IF EXISTS graylore;")
    server.psql("CREATE DATABASE graylore;")
    for f in ("schema.sql", "triggers.sql", "seed_data.sql"):
        r = subprocess.run([psql, "-q", "-v", "ON_ERROR_STOP=1", db_uri, "-f", os.path.join(HERE, f)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"{f} failed:\n{r.stderr}")
        print(f"loaded {f}")

os.environ["DATABASE_URL"] = db_uri
print(f"DATABASE_URL={db_uri}")
print(f"psql shell: {psql} '{db_uri}'")

sys.path.insert(0, HERE)
import app  # noqa: E402

app.app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5050)), debug=True, use_reloader=False)
