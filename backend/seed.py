import os
import time
from datetime import date, timedelta

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
    # 留样冷柜登记：不通过批次强制入柜，记录柜位、预计取出日与实际取出时间
    cur.execute("ALTER TABLE cuppings ADD COLUMN IF NOT EXISTS freezer_slot text")
    cur.execute("ALTER TABLE cuppings ADD COLUMN IF NOT EXISTS expected_remove_date date")
    cur.execute("ALTER TABLE cuppings ADD COLUMN IF NOT EXISTS removed_at timestamp with time zone")
    cur.execute(
        """CREATE TABLE IF NOT EXISTS retention_memos (
            id serial PRIMARY KEY,
            cupping_id integer NOT NULL REFERENCES cuppings(id),
            lot text NOT NULL,
            body text NOT NULL,
            created_at date NOT NULL DEFAULT CURRENT_DATE
        )"""
    )
    cur.execute("SELECT COUNT(*) FROM cuppings")
    if cur.fetchone()[0] == 0:
        # (批次, 香气, 滋味, 汤色, 柜位, 预计取出日)——不通过的夏茶-C 直接登记入柜
        seed_rows = (
            ("春茶-A", 8, 8, 7, None, None),
            ("夏茶-C", 5, 4, 6, "A-07", date.today() + timedelta(days=7)),
        )
        for lot, aroma, taste, liquor, slot, remove_date in seed_rows:
            verdict, note, score = weigh(aroma, taste, liquor)
            cur.execute(
                """INSERT INTO cuppings
                       (lot, aroma, taste, liquor, score, verdict, note, created_by,
                        freezer_slot, expected_remove_date)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lot, aroma, taste, liquor, score, verdict, note, "taster", slot, remove_date),
            )
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
