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
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "writer":
            return ("仅审评员可登记留样", 403)
        return fn(*args, **kwargs)

    return wrap


def parse_date(value):
    return date.fromisoformat(value)


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
    try:
        aroma = float(request.form["aroma"])
        taste = float(request.form["taste"])
        liquor = float(request.form["liquor"])
    except (KeyError, ValueError):
        return ("香气、滋味、汤色必须是数字", 400)
    lot = request.form.get("lot", "").strip()
    if not lot:
        return ("批次不能为空", 400)
    verdict, note, score = weigh(aroma, taste, liquor)

    freezer_slot = request.form.get("freezer_slot", "").strip()
    take_out_raw = request.form.get("take_out_date", "").strip()
    take_out_date = None
    if verdict == "不通过":
        missing = []
        if not freezer_slot:
            missing.append("柜位编号")
        if not take_out_raw:
            missing.append("预计取出日")
        if missing:
            return ("不通过批次必须登记留样：" + "、".join(missing) + "缺一不可", 400)
        try:
            take_out_date = parse_date(take_out_raw)
        except ValueError:
            return ("预计取出日格式无效，应为 YYYY-MM-DD", 400)

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO cuppings
                   (lot, aroma, taste, liquor, score, verdict, note, created_by,
                    freezer_slot, take_out_date)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"],
             freezer_slot or None, take_out_date),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


def sync_overdue_memos(cur):
    """到期仍未取出的批次，每个补记一条超期备忘（幂等）。"""
    cur.execute(
        """INSERT INTO retention_memos (cupping_id, kind, content, created_by)
           SELECT id, 'overdue',
                  '批次 ' || lot || ' 留样已超期（预计取出日 '
                      || to_char(take_out_date, 'YYYY-MM-DD') || '），仍未点取出',
                  %s
             FROM cuppings
            WHERE verdict = '不通过'
              AND taken_out_at IS NULL
              AND take_out_date IS NOT NULL
              AND take_out_date < CURRENT_DATE
           ON CONFLICT (cupping_id, kind) DO NOTHING""",
        ("system",),
    )


@app.get("/freezer")
@login_required
def freezer():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        sync_overdue_memos(cur)
        conn.commit()
        cur.execute(
            """SELECT * FROM cuppings
                WHERE verdict = '不通过' AND freezer_slot IS NOT NULL
                  AND taken_out_at IS NULL AND take_out_date >= CURRENT_DATE
                ORDER BY take_out_date, id"""
        )
        in_freezer = cur.fetchall()
        cur.execute(
            """SELECT * FROM cuppings
                WHERE verdict = '不通过' AND freezer_slot IS NOT NULL
                  AND taken_out_at IS NULL AND take_out_date < CURRENT_DATE
                ORDER BY take_out_date, id"""
        )
        overdue = cur.fetchall()
        cur.execute(
            """SELECT * FROM cuppings
                WHERE verdict = '不通过' AND freezer_slot IS NOT NULL
                  AND taken_out_at IS NOT NULL
                ORDER BY taken_out_at DESC"""
        )
        taken_out = cur.fetchall()
        cur.execute(
            """SELECT m.*, c.lot FROM retention_memos m
                 JOIN cuppings c ON c.id = m.cupping_id
                ORDER BY m.id DESC"""
        )
        memos = cur.fetchall()
    return render_template(
        "freezer.html",
        in_freezer=in_freezer,
        overdue=overdue,
        taken_out=taken_out,
        memos=memos,
        can_write=session.get("role") == "writer",
        today=date.today(),
    )


@app.post("/cuppings/<int:cupping_id>/retrieve")
@writer_required
def retrieve(cupping_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cuppings SET taken_out_at = now() WHERE id = %s AND taken_out_at IS NULL",
            (cupping_id,),
        )
        conn.commit()
    return redirect(url_for("freezer"))


@app.post("/cuppings/<int:cupping_id>/take-out-date")
@writer_required
def update_take_out_date(cupping_id):
    new_date = request.form.get("take_out_date", "").strip()
    if not new_date:
        return ("预计取出日不能为空", 400)
    try:
        parse_date(new_date)
    except ValueError:
        return ("预计取出日格式无效，应为 YYYY-MM-DD", 400)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE cuppings SET take_out_date = %s
                WHERE id = %s AND verdict = '不通过' AND taken_out_at IS NULL""",
            (new_date, cupping_id),
        )
        conn.commit()
    return redirect(url_for("freezer"))
