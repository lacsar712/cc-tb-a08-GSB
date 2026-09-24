import os
import time

import psycopg2

from rules import weigh


def connect():
    last = None
    for _ in range(30):
        try:
            return psycopg2.connect(os.environ["DATABASE_URL"])
        except psycopg2.OperationalError as exc:
            last = exc
            time.sleep(1)
    raise last


def main():
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS cuppings (
            id serial PRIMARY KEY,
            lot text NOT NULL,
            aroma double precision NOT NULL,
            taste double precision NOT NULL,
            liquor double precision NOT NULL,
            score double precision NOT NULL,
            verdict text NOT NULL,
            note text NOT NULL,
            created_by text NOT NULL
        )"""
    )
    # 不通过批次的留样冷柜登记信息
    cur.execute("ALTER TABLE cuppings ADD COLUMN IF NOT EXISTS freezer_slot text")
    cur.execute("ALTER TABLE cuppings ADD COLUMN IF NOT EXISTS take_out_date date")
    cur.execute("ALTER TABLE cuppings ADD COLUMN IF NOT EXISTS taken_out_at timestamp")
    # 超期备忘：每个批次每类只记一条
    cur.execute(
        """CREATE TABLE IF NOT EXISTS retention_memos (
            id serial PRIMARY KEY,
            cupping_id integer NOT NULL REFERENCES cuppings(id),
            kind text NOT NULL,
            content text NOT NULL,
            created_by text NOT NULL,
            created_at timestamp NOT NULL DEFAULT now(),
            UNIQUE (cupping_id, kind)
        )"""
    )
    cur.execute("SELECT COUNT(*) FROM cuppings")
    if cur.fetchone()[0] == 0:
        for lot, aroma, taste, liquor in (("春茶-A", 8, 8, 7), ("夏茶-C", 5, 4, 6)):
            verdict, note, score = weigh(aroma, taste, liquor)
            cur.execute(
                """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lot, aroma, taste, liquor, score, verdict, note, "taster"),
            )
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
