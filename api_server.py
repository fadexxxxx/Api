
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# FINAL FULL VERSION —— api_master.py
# ZERO EXPLANATION. PURE CODE.

import os, json, secrets, hashlib
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# -------------------------------
# DB
# -------------------------------
def db():
    DATABASE_URL = os.environ.get("DATABASE_URL")
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")
    r = urlparse(DATABASE_URL)
    return psycopg2.connect(
        host=r.hostname,
        database=r.path[1:],
        user=r.username,
        password=r.password,
        port=r.port,
        sslmode="require"
    )

# -------------------------------
# UTIL
# -------------------------------
def now():
    return datetime.now(timezone.utc).isoformat()

def gen_id(prefix):
    return f"{prefix}_{secrets.token_hex(8)}"

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# -------------------------------
# INIT DB
# -------------------------------
def init_db():
    conn = db()
    cur = conn.cursor()

    # users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id VARCHAR PRIMARY KEY,
            username VARCHAR UNIQUE NOT NULL,
            pw_hash VARCHAR NOT NULL,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # user_data
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_data(
            user_id VARCHAR PRIMARY KEY,
            credits NUMERIC DEFAULT 1000,
            stats JSONB DEFAULT '[]'::jsonb,
            usage JSONB DEFAULT '[]'::jsonb,
            inbox JSONB DEFAULT '[]'::jsonb,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
    """)

    # admin
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins(
            admin_id VARCHAR PRIMARY KEY,
            pw_hash VARCHAR NOT NULL
        )
    """)

    # servers
    cur.execute("""
        CREATE TABLE IF NOT EXISTS servers(
            server_id VARCHAR PRIMARY KEY,
            server_name VARCHAR,
            status VARCHAR DEFAULT 'offline',
            last_seen TIMESTAMP,
            assigned_user VARCHAR,
            FOREIGN KEY(assigned_user) REFERENCES users(user_id) ON DELETE SET NULL
        )
    """)

    # tasks
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks(
            task_id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            message TEXT NOT NULL,
            total INT,
            count INT,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR DEFAULT 'pending',
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
    """)

    # shards
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shards(
            shard_id VARCHAR PRIMARY KEY,
            task_id VARCHAR NOT NULL,
            server_id VARCHAR,
            phones JSONB NOT NULL,
            status VARCHAR DEFAULT 'pending',
            result JSONB DEFAULT '{}'::jsonb,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
            FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE SET NULL
        )
    """)

    # reports
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports(
            report_id SERIAL PRIMARY KEY,
            shard_id VARCHAR,
            server_id VARCHAR,
            user_id VARCHAR,
            success INT,
            fail INT,
            sent INT,
            credits NUMERIC,
            detail JSONB,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()

# -------------------------------
# USER
# -------------------------------
@app.route("/api/register", methods=["POST"])
def register():
    d = request.json
    username = d["username"]
    pw = d["password"]
    uid = gen_id("u")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE username=%s", (username,))
    if cur.fetchone():
        return jsonify({"ok": False, "msg": "exists"})
    cur.execute("INSERT INTO users(user_id,username,pw_hash) VALUES(%s,%s,%s)",
                (uid, username, hash_pw(pw)))
    cur.execute("INSERT INTO user_data(user_id) VALUES(%s)", (uid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "user_id": uid})


@app.route("/api/login", methods=["POST"])
def login():
    d = request.json
    username = d["username"]
    pw = d["password"]
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE username=%s", (username,))
    u = cur.fetchone()
    if not u:
        return jsonify({"ok": False})
    if u["pw_hash"] != hash_pw(pw):
        return jsonify({"ok": False})
    return jsonify({"ok": True, "user_id": u["user_id"]})

# -------------------------------
# ADMIN
# -------------------------------
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    d = request.json
    aid = d["admin_id"]
    pw = d["password"]
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT pw_hash FROM admins WHERE admin_id=%s", (aid,))
    r = cur.fetchone()
    if not r: return jsonify({"ok": False})
    if r[0] != hash_pw(pw): return jsonify({"ok": False})
    return jsonify({"ok": True})

# -------------------------------
# SERVER (backend nodes)
# -------------------------------
@app.route("/api/server/register", methods=["POST"])
def server_register():
    d = request.json
    sid = d["server_id"]
    name = d.get("server_name","server")
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT server_id FROM servers WHERE server_id=%s",(sid,))
    if not cur.fetchone():
        cur.execute("INSERT INTO servers(server_id,server_name,status,last_seen) VALUES(%s,%s,'online',NOW())",
            (sid,name))
    else:
        cur.execute("UPDATE servers SET status='online', last_seen=NOW() WHERE server_id=%s",(sid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/server/heartbeat", methods=["POST"])
def server_hb():
    d = request.json
    sid = d["server_id"]
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE servers SET last_seen=NOW(), status='online' WHERE server_id=%s",(sid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# -------------------------------
# ASSIGN EXCLUSIVE SERVER
# -------------------------------
@app.route("/api/admin/assign", methods=["POST"])
def admin_assign():
    d = request.json
    server_id = d["server_id"]
    user_id = d["user_id"]
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE servers SET assigned_user=%s WHERE server_id=%s",(user_id,server_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# -------------------------------
# USER AVAILABLE SERVERS
# -------------------------------
@app.route("/api/user/<uid>/servers", methods=["GET"])
def user_servers(uid):
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    # get exclusive
    cur.execute("SELECT server_id FROM servers WHERE assigned_user=%s", (uid,))
    ex = [i["server_id"] for i in cur.fetchall()]

    # get shared = all servers minus all assigned
    cur.execute("SELECT server_id FROM servers WHERE assigned_user IS NULL")
    shared = [i["server_id"] for i in cur.fetchall()]

    conn.close()

    return jsonify({
        "ok": True,
        "shared": shared,
        "exclusive": ex,
        "all": shared + ex
    })

# -------------------------------
# CREATE TASK
# -------------------------------
@app.route("/api/task/create", methods=["POST"])
def create_task():
    d = request.json
    uid = d["user_id"]
    msg = d["message"]
    nums = d["numbers"]
    cnt = int(d.get("count",1))

    task_id = gen_id("task")

    conn = db()
    cur = conn.cursor()

    cur.execute("INSERT INTO tasks(task_id,user_id,message,total,count) VALUES(%s,%s,%s,%s,%s)",
                (task_id,uid,msg,len(nums),cnt))

    # split shards
    SHARD = 50
    for i in range(0,len(nums),SHARD):
        shard_id = gen_id("shard")
        group = nums[i:i+SHARD]
        cur.execute("INSERT INTO shards(shard_id,task_id,phones) VALUES(%s,%s,%s)",
                    (shard_id,task_id,json.dumps(group)))

    conn.commit()
    conn.close()

    return jsonify({"ok": True, "task_id": task_id})

# -------------------------------
# ASSIGN SHARDS TO SERVERS
# -------------------------------
@app.route("/api/task/assign", methods=["POST"])
def assign_task():
    d = request.json
    task_id = d["task_id"]

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # load task user
    cur.execute("SELECT user_id FROM tasks WHERE task_id=%s",(task_id,))
    r = cur.fetchone()
    uid = r["user_id"]

    # user available servers
    cur.execute("SELECT server_id FROM servers WHERE assigned_user=%s",(uid,))
    exclusive = [i["server_id"] for i in cur.fetchall()]
    cur.execute("SELECT server_id FROM servers WHERE assigned_user IS NULL")
    shared = [i["server_id"] for i in cur.fetchall()]

    servers = exclusive + shared
    if not servers:
        return jsonify({"ok": False, "msg": "no server"})

    # shards
    cur.execute("SELECT shard_id FROM shards WHERE task_id=%s",(task_id,))
    shards = [i["shard_id"] for i in cur.fetchall()]

    for idx, sid in enumerate(shards):
        server = servers[idx % len(servers)]
        cur.execute("UPDATE shards SET server_id=%s WHERE shard_id=%s",(server,sid))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})

# -------------------------------
# SERVER FETCH SHARDS
# -------------------------------
@app.route("/api/server/<sid>/shards", methods=["GET"])
def server_shards(sid):
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM shards WHERE server_id=%s AND status='pending'",(sid,))
    r = cur.fetchall()
    # mark in-progress
    for i in r:
        cur2 = conn.cursor()
        cur2.execute("UPDATE shards SET status='running' WHERE shard_id=%s",(i["shard_id"],))
        conn.commit()
    conn.close()
    return jsonify({"ok": True, "shards": r})

# -------------------------------
# SERVER REPORT
# -------------------------------
@app.route("/api/server/report", methods=["POST"])
def server_report():
    d = request.json
    shard_id = d["shard_id"]
    sid = d["server_id"]
    uid = d["user_id"]
    suc = d["success"]
    fail = d["fail"]
    sent = suc + fail
    credits = suc * 1.0

    conn = db()
    cur = conn.cursor()

    # insert
    cur.execute("""
        INSERT INTO reports(shard_id,server_id,user_id,success,fail,sent,credits,detail)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
    """,(shard_id,sid,uid,suc,fail,sent,credits,json.dumps(d)))

    # update shard
    cur.execute("UPDATE shards SET status='done', result=%s WHERE shard_id=%s",
                (json.dumps({"success":suc,"fail":fail}), shard_id))

    # deduct credits
    cur.execute("SELECT credits,usage FROM user_data WHERE user_id=%s",(uid,))
    r = cur.fetchone()
    c = float(r[0])
    log = r[1] or []
    new_c = max(0,c - credits)
    log.append({
        "sid": sid,
        "shard": shard_id,
        "success": suc,
        "fail": fail,
        "credits": credits,
        "ts": now()
    })
    cur.execute("UPDATE user_data SET credits=%s, usage=%s WHERE user_id=%s",(new_c,json.dumps(log),uid))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})

# -------------------------------
# INBOX
# -------------------------------
@app.route("/api/inbox/push", methods=["POST"])
def inbox_push():
    d = request.json
    uid = d["user_id"]
    phone = d["phone"]
    text = d["text"]
    ts = now()

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT inbox FROM user_data WHERE user_id=%s",(uid,))
    inbox = cur.fetchone()[0] or []

    inbox.append({
        "phone":phone,
        "text":text,
        "ts":ts
    })

    cur.execute("UPDATE user_data SET inbox=%s WHERE user_id=%s",(json.dumps(inbox),uid))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})

# -------------------------------
# HEALTH
# -------------------------------
@app.route("/api/health")
def health():
    return jsonify({"ok":True})

# -------------------------------
# MAIN
# -------------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
