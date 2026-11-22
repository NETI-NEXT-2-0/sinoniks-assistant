import os

import psycopg
from psycopg import sql

url = os.environ["PARTNER_TEST_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
parsed = psycopg.conninfo.conninfo_to_dict(url)
if not parsed["dbname"].endswith("_test"):
    raise SystemExit("Disposable test database required")
with psycopg.connect(os.environ["CI_DATABASE_ADMIN_URL"], autocommit=True) as connection:
    connection.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
            sql.Identifier(parsed["user"]), sql.Literal(parsed["password"])
        )
    )
    connection.execute(
        sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
            sql.Identifier(parsed["dbname"]), sql.Identifier(parsed["user"])
        )
    )
with psycopg.connect(url, autocommit=True) as connection:
    connection.execute("CREATE SCHEMA langgraph")
