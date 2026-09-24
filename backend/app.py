import os
from datetime import date
from functools import wraps

import psycopg2
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


def writer_required(fn):
    """登记、点取出等写操作仅限审评员；观察员一律 403。"""
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "writer":
            return ("观察员仅可查看冷柜与超期栏，不能登记或取出", 403)
        return fn(*args, **kwargs)

    return wrap


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=session.get("role") == "writer")


@app.post("/cuppings")
@writer_required
def create():
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)

    # 不通过必须强制登记留样冷柜：柜位编号与预计取出日缺一项即拒交
    freezer_slot = None
    expected_remove_date = None
    if verdict == "不通过":
        freezer_slot = request.form.get("freezer_slot", "").strip()
        raw_date = request.form.get("expected_remove_date", "").strip()
        missing = []
        if not freezer_slot:
            missing.append("柜位编号")
        if not raw_date:
            missing.append("预计取出日")
        if missing:
            return ("不通过批次必须登记留样冷柜，缺少：" + "、".join(missing), 400)
        try:
            expected_remove_date = date.fromisoformat(raw_date)
        except ValueError:
            return ("预计取出日格式无效，应为 YYYY-MM-DD", 400)

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO cuppings
                   (lot, aroma, taste, liquor, score, verdict, note, created_by,
                    freezer_slot, expected_remove_date)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"],
             freezer_slot, expected_remove_date),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


def _sync_overdue_memos(cur, today):
    """到期仍未点取出的批次，每批记一条超期备忘（取出前不重复记）。"""
    cur.execute(
        """SELECT id, lot, expected_remove_date FROM cuppings
           WHERE verdict = '不通过' AND freezer_slot IS NOT NULL
             AND removed_at IS NULL AND expected_remove_date < %s
           ORDER BY expected_remove_date""",
        (today,),
    )
    overdue = cur.fetchall()
    for row in overdue:
        cur.execute(
            """INSERT INTO retention_memos (cupping_id, lot, body, created_at)
               SELECT %s, %s, %s, %s
               WHERE NOT EXISTS (
                   SELECT 1 FROM retention_memos WHERE cupping_id = %s
               )""",
            (
                row["id"],
                row["lot"],
                "批次 %s 应于 %s 取出，至 %s 仍未点取出，已超期"
                % (row["lot"], row["expected_remove_date"], today),
                today,
                row["id"],
            ),
        )
    return overdue


@app.get("/freezer")
@login_required
def freezer():
    today = date.today()
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        overdue = _sync_overdue_memos(cur, today)
        cur.execute(
            """SELECT * FROM cuppings
               WHERE verdict = '不通过' AND freezer_slot IS NOT NULL AND removed_at IS NULL
               ORDER BY expected_remove_date"""
        )
        stored = cur.fetchall()
        cur.execute(
            """SELECT * FROM retention_memos
               WHERE cupping_id IN (
                   SELECT id FROM cuppings WHERE removed_at IS NULL
               )
               ORDER BY created_at DESC, id DESC"""
        )
        memos = cur.fetchall()
        conn.commit()
    in_cabinet = [r for r in stored if r["expected_remove_date"] >= today]
    overdue_lots = {r["id"] for r in overdue}
    overdue_rows = [r for r in stored if r["id"] in overdue_lots]
    return render_template(
        "freezer.html",
        in_cabinet=in_cabinet,
        overdue_rows=overdue_rows,
        memos=memos,
        today=today,
        can_write=session.get("role") == "writer",
    )


@app.post("/cuppings/<int:cupping_id>/remove")
@writer_required
def remove_sample(cupping_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE cuppings SET removed_at = CURRENT_TIMESTAMP
               WHERE id = %s AND freezer_slot IS NOT NULL AND removed_at IS NULL""",
            (cupping_id,),
        )
        conn.commit()
    return redirect(url_for("freezer"))


@app.post("/cuppings/<int:cupping_id>/reschedule")
@writer_required
def reschedule(cupping_id):
    raw_date = request.form.get("expected_remove_date", "").strip()
    if not raw_date:
        return ("预计取出日不能为空", 400)
    try:
        new_date = date.fromisoformat(raw_date)
    except ValueError:
        return ("预计取出日格式无效，应为 YYYY-MM-DD", 400)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE cuppings SET expected_remove_date = %s
               WHERE id = %s AND freezer_slot IS NOT NULL AND removed_at IS NULL""",
            (new_date, cupping_id),
        )
        conn.commit()
    return redirect(url_for("freezer"))
