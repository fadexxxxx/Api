#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoSender API Server"""

# region API


# region [IMPORTS]
import os
import json
import time
import secrets
import hashlib
import sys
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from flask_sock import Sock

import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse
# endregion


# region [APP INIT]
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', stream=sys.stdout)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

sock = Sock(app)

_DB_READY = False
_DB_INIT_LOCK = threading.Lock()
_frontend_clients = {}  # sid -> {"ws": ws, "user_id": str, "subscribed_tasks": set, "connected_at": time}
_task_subscribers = {}  # task_id -> set(sid)
_worker_clients = {}  # server_id -> {"ws": ws, "meta": {}, "ready": False, "connected_at": time}
_worker_lock = threading.Lock()
_frontend_lock = threading.Lock()
# endregion


# region [DB & UTILS]
# 数据库初始化已移至应用启动时（见文件末尾）
# 不要在每次请求时初始化数据库！


def _require_env(name: str) -> str:
    """获取必需环境变量"""
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def db():
    """获取数据库连接"""
    database_url = _require_env("DATABASE_URL")
    try:
        conn = psycopg2.connect(database_url, connect_timeout=10)
        return conn
    except Exception as e:
        print(f"❌ PostgreSQL 连接失败: {e}")
        import traceback
        traceback.print_exc()
        raise RuntimeError(f"PostgreSQL 连接失败: {e}") from e


def now_iso() -> str:
    """获取当前UTC时间ISO格式"""
    return datetime.now(timezone.utc).isoformat()


def gen_id(prefix: str) -> str:
    """生成带前缀的4位短ID（人类可读）"""
    # 使用数字和大写字母，排除容易混淆的字符（0,O,1,I,L）
    chars = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    short_id = ''.join(secrets.choice(chars) for _ in range(4))
    return f"{prefix}_{short_id}"


def hash_pw(pw: str) -> str:
    """密码哈希"""
    return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    """Token哈希"""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _json() -> Dict[str, Any]:
    """获取请求JSON"""
    return request.get_json(silent=True) or {}


def _bearer_token() -> Optional[str]:
    """获取Bearer Token"""
    auth = request.headers.get("Authorization", "")
    if not auth:
        return None
    parts = auth.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _get_setting(cur, key: str) -> Optional[str]:
    """获取设置项"""
    cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
    row = cur.fetchone()
    if not row:
        return None
    return row.get("value") if isinstance(row, dict) else row[0]


def _set_setting(cur, key: str, value: str) -> None:
    """设置设置项"""
    cur.execute("INSERT INTO settings(key, value) VALUES(%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))


def _verify_user_token(conn, user_id: str, token: str) -> bool:
    """验证用户Token"""
    if not user_id or not token:
        return False
    th = hash_token(token)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM user_tokens WHERE user_id=%s AND token_hash=%s", (user_id, th))
    ok = cur.fetchone() is not None
    if ok:
        cur.execute("UPDATE user_tokens SET last_used=NOW() WHERE user_id=%s AND token_hash=%s", (user_id, th))
        conn.commit()
    return ok


def _maybe_authed_user(conn) -> Optional[str]:
    """尝试从Token获取用户ID"""
    token = _bearer_token()
    if not token:
        return None
    th = hash_token(token)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT user_id FROM user_tokens WHERE token_hash=%s ORDER BY created DESC LIMIT 1", (th,))
    row = cur.fetchone()
    return row["user_id"] if row else None
# endregion


# region [DB INIT]
def init_db() -> None:
    """初始化数据库表"""
    print("🔄 正在连接数据库...")
    conn = db()
    try:
        cur = conn.cursor()
        print("🔄 正在创建表...")

        # 先删除可能存在的重复表
        cur.execute("DROP TABLE IF EXISTS users CASCADE")
        cur.execute("DROP TABLE IF EXISTS user_data CASCADE")
        cur.execute("DROP TABLE IF EXISTS user_tokens CASCADE")
        cur.execute("DROP TABLE IF EXISTS admins CASCADE")
        cur.execute("DROP TABLE IF EXISTS admin_tokens CASCADE")
        cur.execute("DROP TABLE IF EXISTS settings CASCADE")
        cur.execute("DROP TABLE IF EXISTS servers CASCADE")
        cur.execute("DROP TABLE IF EXISTS tasks CASCADE")
        cur.execute("DROP TABLE IF EXISTS shards CASCADE")
        cur.execute("DROP TABLE IF EXISTS reports CASCADE")
        cur.execute("DROP TABLE IF EXISTS conversations CASCADE")
        cur.execute("DROP TABLE IF EXISTS sent_records CASCADE")
        cur.execute("DROP TABLE IF EXISTS id_library CASCADE")

        cur.execute("""CREATE TABLE IF NOT EXISTS users(user_id VARCHAR PRIMARY KEY, username VARCHAR UNIQUE NOT NULL, pw_hash VARCHAR NOT NULL, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS user_data(user_id VARCHAR PRIMARY KEY, credits NUMERIC DEFAULT 1000, stats JSONB DEFAULT '[]'::jsonb, usage JSONB DEFAULT '[]'::jsonb, inbox JSONB DEFAULT '[]'::jsonb, FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS user_tokens(token_hash VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_used TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS admins(admin_id VARCHAR PRIMARY KEY, pw_hash VARCHAR NOT NULL, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS admin_tokens(token_hash VARCHAR PRIMARY KEY, admin_id VARCHAR NOT NULL, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_used TIMESTAMP, FOREIGN KEY(admin_id) REFERENCES admins(admin_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS settings(key VARCHAR PRIMARY KEY, value TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS servers(server_id VARCHAR PRIMARY KEY, server_name VARCHAR, server_url TEXT, port INT, clients_count INT DEFAULT 0, status VARCHAR DEFAULT 'disconnected', last_seen TIMESTAMP, registered_at TIMESTAMP, registry_id VARCHAR, meta JSONB DEFAULT '{}'::jsonb, assigned_user VARCHAR, FOREIGN KEY(assigned_user) REFERENCES users(user_id) ON DELETE SET NULL)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS tasks(task_id VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, message TEXT NOT NULL, total INT, count INT, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status VARCHAR DEFAULT 'pending', FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS shards(shard_id VARCHAR PRIMARY KEY, task_id VARCHAR NOT NULL, server_id VARCHAR, phones JSONB NOT NULL, status VARCHAR DEFAULT 'pending', attempts INT DEFAULT 0, locked_at TIMESTAMP, updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, result JSONB DEFAULT '{}'::jsonb, FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE, FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE SET NULL)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS reports(report_id SERIAL PRIMARY KEY, shard_id VARCHAR, server_id VARCHAR, user_id VARCHAR, success INT, fail INT, sent INT, credits NUMERIC, detail JSONB, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS conversations(user_id VARCHAR NOT NULL, chat_id VARCHAR NOT NULL, meta JSONB DEFAULT '{}'::jsonb, messages JSONB DEFAULT '[]'::jsonb, updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, chat_id), FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS sent_records(id SERIAL PRIMARY KEY, user_id VARCHAR NOT NULL, phone_number VARCHAR, task_id VARCHAR, detail JSONB DEFAULT '{}'::jsonb, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS id_library(apple_id VARCHAR PRIMARY KEY, password VARCHAR NOT NULL, status VARCHAR DEFAULT 'normal', usage_status VARCHAR DEFAULT 'new', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

        print("✅ 用户相关表创建完成")
        
        # 测试插入一个测试用户
        try:
            test_uid = "test_user_001"
            test_username = "testuser"
            test_pw = "test123"
            
            cur.execute("INSERT INTO users(user_id, username, pw_hash) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", 
                       (test_uid, test_username, hash_pw(test_pw)))
            cur.execute("INSERT INTO user_data(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (test_uid,))
            
            print(f"✅ 测试用户已创建: {test_username}/{test_pw}")
        except Exception as e:
            print(f"⚠️ 创建测试用户失败: {e}")

        default_pw = os.environ.get("SERVER_MANAGER_PASSWORD", "admin123")
        if not _get_setting(cur, "server_manager_pw_hash"):
            _set_setting(cur, "server_manager_pw_hash", hash_pw(default_pw))
            print(f"✅ 默认管理员密码已设置")

        conn.commit()
        print("✅ 数据库初始化完全成功")
    except Exception as e:
        print(f"❌ 数据库初始化错误: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        conn.close()



# endregion


# region [REDIS UTILS]
import redis

class RedisManager:
    """简易Redis管理器，没有Redis时用内存模拟"""
    
    def __init__(self):
        self.redis_url = os.environ.get("REDIS_URL")
        self.use_redis = bool(self.redis_url)
        
        if self.use_redis:
            try:
                self.client = redis.from_url(self.redis_url, decode_responses=True)
                self.client.ping()
                print("✅ Redis连接成功")
            except:
                print("❌ Redis连接失败，使用内存模式")
                self.use_redis = False
                self.client = None
        else:
            print("⚠️ 未设置REDIS_URL，使用内存模式")
            self.client = None
        
        # 内存后备存储
        self.memory_data = {
            "online_workers": set(),
            "worker_load": {}
        }
    
    def worker_online(self, server_id: str, info: dict):
        """标记Worker在线"""
        if self.use_redis:
            # 存储到Redis，30秒过期
            self.client.hset(f"worker:{server_id}", mapping=info)
            self.client.expire(f"worker:{server_id}", 30)
            self.client.sadd("online_workers", server_id)
        else:
            # 存储到内存
            self.memory_data["online_workers"].add(server_id)
            self.memory_data["worker_load"][server_id] = info.get("load", 0)
    
    def worker_offline(self, server_id: str):
        """标记Worker离线"""
        if self.use_redis:
            self.client.srem("online_workers", server_id)
            self.client.delete(f"worker:{server_id}")
        else:
            self.memory_data["online_workers"].discard(server_id)
            self.memory_data["worker_load"].pop(server_id, None)
    
    def update_heartbeat(self, server_id: str):
        """更新心跳"""
        if self.use_redis:
            if self.client.exists(f"worker:{server_id}"):
                self.client.expire(f"worker:{server_id}", 30)  # 续期
        # 内存模式无需额外操作
    
    def get_online_workers(self):
        """获取在线Worker列表"""
        if self.use_redis:
            return list(self.client.smembers("online_workers"))
        else:
            return list(self.memory_data["online_workers"])
    
    def get_worker_load(self, server_id: str):
        """获取Worker负载"""
        if self.use_redis:
            load = self.client.hget(f"worker:{server_id}", "load")
            return int(load) if load else 0
        else:
            return self.memory_data["worker_load"].get(server_id, 0)
    
    def set_worker_load(self, server_id: str, load: int):
        """设置Worker负载"""
        if self.use_redis:
            self.client.hset(f"worker:{server_id}", "load", load)
        else:
            self.memory_data["worker_load"][server_id] = load

# 创建全局实例
redis_manager = RedisManager()
# endregion


# region [HEALTH]   
@app.route("/")
def root():
    """根路由"""
    print("✅ 根路由被访问")
    return jsonify({"ok": True, "name": "AutoSender API", "status": "running", "message": "API is running. Use /api endpoints.", "timestamp": now_iso()})

@app.route("/api")
def api_root():
    """API根路由"""
    print("✅ API根路由被访问")
    return jsonify({"ok": True, "name": "AutoSender API", "status": "running", "timestamp": now_iso()})

def _ensure_db_initialized():
    """确保数据库已初始化（线程安全）"""
    global _DB_READY
    if not _DB_READY:
        with _DB_INIT_LOCK:
            if not _DB_READY:  # Double-check locking
                try:
                    print("🔄 首次请求 - 初始化数据库...")
                    init_db()
                    _DB_READY = True
                    print("✅ 数据库初始化成功")
                except Exception as e:
                    print(f"❌ 数据库初始化失败: {e}")
                    import traceback
                    traceback.print_exc()
                    raise

@app.route("/api/health")
def health():
    """健康检查"""
    print("✅ 健康检查被访问")
    try:
        # 确保数据库已初始化
        _ensure_db_initialized()
        # 测试数据库连接
        conn = db()
        conn.close()
        db_status = "connected"
    except Exception as e:
        print(f"❌ 数据库连接失败: {e}")
        db_status = f"error: {str(e)}"
    
    return jsonify({
        "ok": True, 
        "status": "healthy", 
        "database": db_status,
        "timestamp": now_iso()
    })

@app.route("/api/debug/db-status", methods=["GET"])
def debug_db_status():
    """数据库状态诊断"""
    try:
        conn = db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 检查所有表是否存在
        cur.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tables = [row["table_name"] for row in cur.fetchall()]
        
        # 检查各表行数
        table_counts = {}
        for table in tables:
            try:
                cur.execute(f"SELECT COUNT(*) as cnt FROM {table}")
                count = cur.fetchone()["cnt"]
                table_counts[table] = count
            except:
                table_counts[table] = "error"
        
        # 检查admins表
        cur.execute("SELECT admin_id, created FROM admins")
        admins = cur.fetchall()
        
        # 检查users表
        cur.execute("SELECT user_id, username, created FROM users")
        users = cur.fetchall()
        
        conn.close()
        
        return jsonify({
            "ok": True,
            "tables": tables,
            "table_counts": table_counts,
            "admins": admins,
            "users": users,
            "message": f"数据库连接正常，共{len(tables)}个表"
        })
        
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "message": "数据库连接失败"
        }), 500

@app.route("/api/debug/redis", methods=["GET"])
def debug_redis():
    """查看Redis状态"""
    online = redis_manager.get_online_workers()
    workers = []
    
    for worker_id in online:
        load = redis_manager.get_worker_load(worker_id)
        workers.append({
            "server_id": worker_id,
            "load": load,
            "online": True
        })
    
    return jsonify({
        "ok": True,
        "use_redis": redis_manager.use_redis,
        "online_workers": len(online),
        "workers": workers
    })









# endregion


# region [USER AUTH]
def _issue_user_token(conn, user_id: str) -> str:
    """签发用户Token"""
    token = secrets.token_urlsafe(24)
    th = hash_token(token)
    cur = conn.cursor()
    cur.execute("INSERT INTO user_tokens(token_hash, user_id, last_used) VALUES(%s,%s,NOW()) ON CONFLICT DO NOTHING", (th, user_id))
    conn.commit()
    return token


@app.route("/api/register", methods=["POST", "OPTIONS"])
def register():
    """用户注册/服务器注册"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()

    if ("username" not in d) and ("url" in d) and ("name" in d or "server_name" in d):
        name = (d.get("name") or d.get("server_name") or "server").strip()
        url = (d.get("url") or "").strip()
        port = d.get("port")
        clients_count = int(d.get("clients_count") or d.get("clients") or 0)
        status = (d.get("status") or "online").strip().lower()

        conn = db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        registry_id = gen_id("reg")
        server_id = d.get("server_id") or gen_id("server")

        cur.execute("""INSERT INTO servers(server_id, server_name, server_url, port, clients_count, status, last_seen, registered_at, registry_id, meta) VALUES(%s,%s,%s,%s,%s,%s,NOW(),NOW(),%s,%s) ON CONFLICT (server_id) DO UPDATE SET server_name=EXCLUDED.server_name, server_url=EXCLUDED.server_url, port=EXCLUDED.port, clients_count=EXCLUDED.clients_count, status=EXCLUDED.status, last_seen=NOW()""", (server_id, name, url, port, clients_count, _normalize_server_status(status, clients_count), registry_id, json.dumps(d)))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "success": True, "id": registry_id, "server_id": server_id})

    username = (d.get("username") or "").strip()
    pw = (d.get("password") or "").strip()

    if not username:
        return jsonify({"ok": False, "success": False, "message": "用户名不能为空"}), 400
    if not pw:
        return jsonify({"ok": False, "success": False, "message": "密码不能为空"}), 400
    if len(pw) < 4:
        return jsonify({"ok": False, "success": False, "message": "密码至少需要4位"}), 400

    uid = gen_id("u")
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
    if cur.fetchone():
        conn.close()
        return jsonify({"ok": False, "success": False, "message": "用户名已存在"}), 409

    try:
        cur.execute("INSERT INTO users(user_id,username,pw_hash) VALUES(%s,%s,%s)", (uid, username, hash_pw(pw)))
        cur.execute("INSERT INTO user_data(user_id) VALUES(%s)", (uid,))
        conn.commit()
        token = _issue_user_token(conn, uid)
        conn.close()
        return jsonify({"ok": True, "success": True, "user_id": uid, "token": token, "message": "注册成功"})
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "success": False, "message": f"注册失败: {str(e)}"}), 500


@app.route("/api/login", methods=["POST", "OPTIONS"])
def login():
    """用户登录"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    username = (d.get("username") or "").strip()
    pw = (d.get("password") or "").strip()
    
    print(f"🔐 用户登录请求: {username}")

    if not username or not pw:
        print(f"❌ 登录失败: 用户名或密码为空")
        return jsonify({"ok": False, "success": False, "message": "用户名和密码不能为空"}), 400

    try:
        conn = db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        print(f"🔄 查询用户: {username}")
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
    except Exception as e:
        print(f"❌ 数据库查询失败: {e}")
        return jsonify({"ok": False, "success": False, "message": "数据库错误"}), 500
    u = cur.fetchone()

    if not u or u.get("pw_hash") != hash_pw(pw):
        conn.close()
        return jsonify({"ok": False, "success": False, "message": "用户名或密码错误"}), 401

    token = _issue_user_token(conn, u["user_id"])
    uid = u["user_id"]
    
    print(f"🔄 加载用户数据...")
    cur.execute("SELECT credits, usage FROM user_data WHERE user_id=%s", (uid,))
    user_data = cur.fetchone()
    credits = float(user_data["credits"]) if user_data and user_data.get("credits") is not None else 1000.0
    usage = user_data.get("usage") if user_data else []
    
    print(f"🔄 加载对话记录...")
    cur.execute("SELECT chat_id, meta, messages, updated FROM conversations WHERE user_id=%s ORDER BY updated DESC LIMIT 100", (uid,))
    conversations_rows = cur.fetchall()
    conversations = [{"chat_id": conv.get("chat_id"), "meta": conv.get("meta") or {}, "messages": conv.get("messages") or [], "updated": conv.get("updated").isoformat() if conv.get("updated") else None} for conv in conversations_rows]
    
    print(f"🔄 加载发送记录...")
    cur.execute("SELECT phone_number, task_id, detail, ts FROM sent_records WHERE user_id=%s ORDER BY ts DESC LIMIT 50", (uid,))
    sent_records_rows = cur.fetchall()
    access_records = [{"phone_number": rec.get("phone_number"), "task_id": rec.get("task_id"), "detail": rec.get("detail") or {}, "ts": rec.get("ts").isoformat() if rec.get("ts") else None} for rec in sent_records_rows]
    
    print(f"🔄 加载任务历史...")
    cur.execute("SELECT task_id, message, total, count, status, created, updated FROM tasks WHERE user_id=%s ORDER BY created DESC LIMIT 50", (uid,))
    tasks_rows = cur.fetchall()
    history_tasks = []
    for task in tasks_rows:
        task_id = task.get("task_id")
        cur.execute("SELECT COALESCE(SUM(success),0) AS success, COALESCE(SUM(fail),0) AS fail, COALESCE(SUM(sent),0) AS sent FROM reports WHERE shard_id IN (SELECT shard_id FROM shards WHERE task_id=%s)", (task_id,))
        stats = cur.fetchone() or {}
        history_tasks.append({"task_id": task_id, "message": task.get("message"), "total": task.get("total"), "count": task.get("count"), "status": task.get("status"), "created": task.get("created").isoformat() if task.get("created") else None, "updated": task.get("updated").isoformat() if task.get("updated") else None, "result": {"success": int(stats.get("success", 0)), "fail": int(stats.get("fail", 0)), "sent": int(stats.get("sent", 0))}})
    
    conn.close()
    
    print(f"✅ 用户 {username} 登录成功")
    return jsonify({
        "ok": True, "success": True, "user_id": uid, "token": token, "message": "登录成功",
        "balance": credits, "usage_records": usage or [], "access_records": access_records,
        "inbox_conversations": conversations, "history_tasks": history_tasks,
        "data": {"credits": credits, "usage": usage or [], "conversations": conversations, "sent_records": access_records}
    })


@app.route("/api/verify", methods=["POST", "OPTIONS"])
def verify_user():
    """验证用户Token"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    user_id = d.get("user_id")
    token = d.get("token")
    
    print(f"🔐 验证用户: {user_id}")

    try:
        conn = db()
        ok = _verify_user_token(conn, user_id, token)
        conn.close()
    except Exception as e:
        print(f"❌ 验证失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

    if ok:
        return jsonify({"ok": True, "success": True})
    return jsonify({"ok": False, "success": False, "message": "invalid_token"}), 401
# endregion


# region [ADMIN AUTH]
def _issue_admin_token(conn, admin_id: str) -> str:
    """签发管理员Token"""
    token = secrets.token_urlsafe(24)
    th = hash_token(token)
    cur = conn.cursor()
    cur.execute("INSERT INTO admin_tokens(token_hash, admin_id, last_used) VALUES(%s,%s,NOW()) ON CONFLICT DO NOTHING", (th, admin_id))
    conn.commit()
    return token


def _verify_admin_token(conn, token: str) -> Optional[str]:
    """验证管理员Token"""
    if not token:
        return None
    th = hash_token(token)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT admin_id FROM admin_tokens WHERE token_hash=%s ORDER BY created DESC LIMIT 1", (th,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE admin_tokens SET last_used=NOW() WHERE token_hash=%s", (th,))
        conn.commit()
        return row["admin_id"]
    return None


@app.route("/api/admin/login", methods=["POST", "OPTIONS"])
def admin_login():
    """管理员登录"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    aid = (d.get("admin_id") or "").strip()
    pw = (d.get("password") or "").strip()

    if not aid or not pw:
        return jsonify({"ok": False, "success": False, "message": "管理员ID和密码不能为空"}), 400

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT pw_hash FROM admins WHERE admin_id=%s", (aid,))
    r = cur.fetchone()

    if not r:
        conn.close()
        return jsonify({"ok": False, "success": False, "message": "管理员ID不存在"}), 401

    if r[0] != hash_pw(pw):
        conn.close()
        return jsonify({"ok": False, "success": False, "message": "密码错误"}), 401

    token = _issue_admin_token(conn, aid)
    conn.close()
    return jsonify({"ok": True, "success": True, "admin_id": aid, "token": token, "message": "登录成功"})


@app.route("/api/admin/account", methods=["POST", "GET", "OPTIONS"])
def admin_account_collection():
    """管理员账号管理"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "GET":
        cur.execute("SELECT admin_id, created FROM admins ORDER BY created DESC")
        rows = cur.fetchall()
        conn.close()
        return jsonify({"success": True, "admins": rows})

    d = _json()
    admin_id = (d.get("admin_id") or "").strip()
    password = (d.get("password") or "").strip()
    if not admin_id or not password:
        conn.close()
        return jsonify({"success": False, "message": "缺少 admin_id 或 password"}), 400

    # 修复：检查是否有管理员存在，如果没有任何管理员，允许创建第一个管理员
    cur.execute("SELECT COUNT(*) as cnt FROM admins")
    admin_count = cur.fetchone()
    has_admins = admin_count and admin_count.get("cnt", 0) > 0
    
    # 只有当已有管理员时才需要认证
    if has_admins:
        token = _bearer_token()
        if not token:
            conn.close()
            return jsonify({"success": False, "message": "需要认证才能管理管理员账号"}), 403
        
        caller_admin_id = _verify_admin_token(conn, token)
        if not caller_admin_id:
            conn.close()
            return jsonify({"success": False, "message": "需要管理员身份才能创建新管理员"}), 403

    try:
        # 检查是否已存在
        cur.execute("SELECT 1 FROM admins WHERE admin_id=%s", (admin_id,))
        exists = cur.fetchone() is not None
        
        # 插入或更新
        cur.execute("INSERT INTO admins(admin_id, pw_hash) VALUES(%s,%s) ON CONFLICT (admin_id) DO UPDATE SET pw_hash=EXCLUDED.pw_hash", 
                   (admin_id, hash_pw(password)))
        conn.commit()
        
        # 获取创建的管理员信息
        cur.execute("SELECT admin_id, created FROM admins WHERE admin_id=%s", (admin_id,))
        new_admin = cur.fetchone()
        conn.close()
        
        return jsonify({
            "success": True, 
            "admin": new_admin, 
            "message": "管理员账号已更新" if exists else "管理员账号已创建"
        })
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/api/admin/account/<admin_id>", methods=["GET", "PUT", "DELETE", "OPTIONS"])
def admin_account_item(admin_id: str):
    """管理员账号详情"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "GET":
        cur.execute("SELECT admin_id, created FROM admins WHERE admin_id=%s", (admin_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "message": "not_found"}), 404
        return jsonify({"success": True, "admin": row})

    if request.method == "PUT":
        d = _json()
        password = (d.get("password") or "").strip()
        if not password:
            conn.close()
            return jsonify({"success": False, "message": "missing_password"}), 400
        cur.execute("UPDATE admins SET pw_hash=%s WHERE admin_id=%s", (hash_pw(password), admin_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True})

    cur.execute("DELETE FROM admins WHERE admin_id=%s", (admin_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})
# endregion


# region [ADMIN USER MGMT]
@app.route("/api/admin/users", methods=["POST", "GET", "OPTIONS"])
def admin_users_collection():
    """管理员用户管理"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    token = _bearer_token()
    caller_admin_id = _verify_admin_token(conn, token)
    if not caller_admin_id:
        conn.close()
        return jsonify({"success": False, "message": "需要管理员身份"}), 403

    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "GET":
        cur.execute("SELECT u.user_id, u.username, u.created, d.credits FROM users u LEFT JOIN user_data d ON u.user_id = d.user_id ORDER BY u.created DESC")
        rows = cur.fetchall()
        conn.close()
        return jsonify({"success": True, "users": rows})

    d = _json()
    username = (d.get("username") or "").strip()
    password = (d.get("password") or "").strip()
    initial_credits = float(d.get("credits", 1000))

    if not username:
        conn.close()
        return jsonify({"success": False, "message": "用户名不能为空"}), 400
    if not password:
        conn.close()
        return jsonify({"success": False, "message": "密码不能为空"}), 400

    cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
    if cur.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "用户名已存在"}), 409

    uid = gen_id("u")
    try:
        cur2 = conn.cursor()
        cur2.execute("INSERT INTO users(user_id, username, pw_hash) VALUES(%s,%s,%s)", (uid, username, hash_pw(password)))
        cur2.execute("INSERT INTO user_data(user_id, credits) VALUES(%s,%s)", (uid, initial_credits))
        conn.commit()
        cur.execute("SELECT u.user_id, u.username, u.created, d.credits FROM users u LEFT JOIN user_data d ON u.user_id = d.user_id WHERE u.user_id=%s", (uid,))
        new_user = cur.fetchone()
        conn.close()
        return jsonify({"success": True, "user": new_user, "message": "用户创建成功"})
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "message": f"创建失败: {str(e)}"}), 500


@app.route("/api/admin/users/<user_id>", methods=["GET", "DELETE", "OPTIONS"])
def admin_user_item(user_id: str):
    """管理员用户详情"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    token = _bearer_token()
    caller_admin_id = _verify_admin_token(conn, token)
    if not caller_admin_id:
        conn.close()
        return jsonify({"success": False, "message": "需要管理员身份"}), 403

    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "GET":
        cur.execute("SELECT u.user_id, u.username, u.created, d.credits FROM users u LEFT JOIN user_data d ON u.user_id = d.user_id WHERE u.user_id=%s", (user_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "message": "用户不存在"}), 404
        return jsonify({"success": True, "user": row})

    cur2 = conn.cursor()
    cur2.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "用户已删除"})


@app.route("/api/admin/users/<user_id>/recharge", methods=["POST", "OPTIONS"])
def admin_user_recharge(user_id: str):
    """管理员用户充值（支持user_id或username）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    token = _bearer_token()
    caller_admin_id = _verify_admin_token(conn, token)
    if not caller_admin_id:
        conn.close()
        return jsonify({"success": False, "message": "需要管理员身份"}), 403

    d = _json()
    amount = d.get("amount")
    if amount is None:
        conn.close()
        return jsonify({"success": False, "message": "缺少充值金额"}), 400
    
    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"success": False, "message": "金额格式错误"}), 400
    
    if amount_f <= 0:
        conn.close()
        return jsonify({"success": False, "message": "充值金额必须大于0"}), 400

    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 解析用户标识（支持user_id或username）
    real_user_id, username = _resolve_user_id(cur, user_id)
    if not real_user_id:
        conn.close()
        return jsonify({"success": False, "message": "用户不存在"}), 404
    
    cur.execute("SELECT credits, usage FROM user_data WHERE user_id=%s", (real_user_id,))
    row = cur.fetchone()
    
    if not row:
        conn.close()
        return jsonify({"success": False, "message": "用户数据不存在"}), 404

    old_credits = float(row.get("credits", 0))
    new_credits = old_credits + amount_f
    usage = row.get("usage") or []
    usage.append({"action": "recharge", "amount": amount_f, "ts": now_iso(), "admin_id": caller_admin_id, "old_credits": old_credits, "new_credits": new_credits})

    cur2 = conn.cursor()
    cur2.execute("UPDATE user_data SET credits=%s, usage=%s WHERE user_id=%s", (new_credits, json.dumps(usage), real_user_id))
    conn.commit()
    conn.close()

    try:
        broadcast_user_update(real_user_id, 'balance_update', {'credits': new_credits, 'balance': new_credits, 'recharged': amount_f, 'old_credits': old_credits})
    except Exception as e:
        logger.warning(f"推送余额更新失败: {e}")

    return jsonify({"success": True, "user_id": real_user_id, "username": username, "old_credits": old_credits, "amount": amount_f, "credits": new_credits, "new_credits": new_credits, "message": f"充值成功，当前余额: {new_credits}"})
# endregion


# region [SERVER MANAGER]
@app.route("/api/server-manager/verify", methods=["POST", "OPTIONS"])
def server_manager_verify():
    """服务器管理密码验证"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    password = d.get("password", "")

    conn = db()
    cur = conn.cursor()
    pw_hash = _get_setting(cur, "server_manager_pw_hash") or hash_pw("admin123")
    ok = (hash_pw(password) == pw_hash)
    conn.close()

    if ok:
        return jsonify({"success": True, "message": "验证成功"})
    return jsonify({"success": False, "message": "密码错误"}), 401


@app.route("/api/server-manager/password", methods=["PUT", "OPTIONS"])
def server_manager_password_update():
    """服务器管理密码更新"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    old_pw = d.get("oldPassword") or d.get("old_password") or ""
    new_pw = d.get("password") or ""

    if not old_pw or not new_pw:
        return jsonify({"success": False, "message": "缺少旧密码或新密码"}), 400

    conn = db()
    cur = conn.cursor()
    current_hash = _get_setting(cur, "server_manager_pw_hash") or hash_pw("admin123")

    if hash_pw(old_pw) != current_hash:
        conn.close()
        return jsonify({"success": False, "message": "旧密码错误"}), 401

    _set_setting(cur, "server_manager_pw_hash", hash_pw(new_pw))
    conn.commit()
    conn.close()
    return jsonify({"success": True})
# endregion


# region [SERVER REGISTRY]
def _normalize_server_status(status: str, clients_count: int) -> str:
    """规范化服务器状态"""
    s = (status or "").lower().strip()
    if s in {"online", "available"}:
        return "connected" if clients_count > 0 else "available"
    if s in {"connected", "disconnected", "offline"}:
        return "disconnected" if s == "offline" else s
    return "connected" if clients_count > 0 else "available"


@app.route("/api/server/register", methods=["POST", "OPTIONS"])
def server_register():
    """Worker服务器注册"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    sid = d.get("server_id")
    name = d.get("server_name") or d.get("name") or "server"
    ws_url = d.get("server_url") or d.get("url")
    port = d.get("port")

    if not sid:
        return jsonify({"ok": False, "success": False, "message": "missing server_id"}), 400

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM servers WHERE server_id=%s", (sid,))
    exists = cur.fetchone() is not None
    status = _normalize_server_status(d.get("status") or "available", int(d.get("clients_count") or 0))

    if not exists:
        cur.execute("INSERT INTO servers(server_id, server_name, server_url, port, status, last_seen, registered_at, meta) VALUES(%s,%s,%s,%s,%s,NOW(),NOW(),%s)", (sid, name, ws_url, port, status, json.dumps(d)))
    else:
        cur.execute("UPDATE servers SET server_name=%s, server_url=COALESCE(%s, server_url), port=COALESCE(%s, port), status=%s, last_seen=NOW(), meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb WHERE server_id=%s", (name, ws_url, port, status, json.dumps(d), sid))

    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/server/heartbeat", methods=["POST", "OPTIONS"])
def server_hb():
    """服务器心跳"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    sid = d.get("server_id")
    if not sid:
        return jsonify({"ok": False, "message": "missing server_id"}), 400

    clients_count = int(d.get("clients_count") or d.get("clients") or 0)
    status = _normalize_server_status(d.get("status") or "available", clients_count)

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE servers SET last_seen=NOW(), status=%s, clients_count=%s, meta = COALESCE(meta,'{}'::jsonb) || %s::jsonb WHERE server_id=%s", (status, clients_count, json.dumps(d), sid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/server/update_info", methods=["POST", "OPTIONS"])
def server_update_info():
    """更新服务器信息"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    sid = d.get("server_id")
    server_name = d.get("server_name")
    phone = d.get("phone")

    if not sid:
        return jsonify({"ok": False, "success": False, "message": "missing server_id"}), 400

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM servers WHERE server_id=%s", (sid,))
    exists = cur.fetchone() is not None

    if not exists:
        meta = {"phone": phone} if phone else {}
        cur.execute("INSERT INTO servers(server_id, server_name, status, last_seen, registered_at, meta) VALUES(%s,%s,'available',NOW(),NOW(),%s)", (sid, server_name, json.dumps(meta)))
    else:
        update_fields = []
        params = []
        if server_name:
            update_fields.append("server_name=%s")
            params.append(server_name)
        if phone:
            update_fields.append("meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb")
            params.append(json.dumps({"phone": phone}))
        update_fields.append("last_seen=NOW()")
        params.append(sid)
        cur.execute(f"UPDATE servers SET {', '.join(update_fields)} WHERE server_id=%s", tuple(params))

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "success": True, "message": f"服务器信息已更新: {server_name} ({phone})"})


@app.route("/api/heartbeat", methods=["POST", "OPTIONS"])
def registry_heartbeat_alias():
    """Registry心跳(兼容)"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    registry_id = d.get("id")
    name = d.get("name")
    url = d.get("url")
    clients_count = int(d.get("clients_count") or 0)
    status = _normalize_server_status(d.get("status") or "online", clients_count)

    if not registry_id:
        return jsonify({"success": False, "message": "missing id"}), 400

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE servers SET last_seen=NOW(), status=%s, server_name=COALESCE(%s, server_name), server_url=COALESCE(%s, server_url), clients_count=%s, meta = COALESCE(meta,'{}'::jsonb) || %s::jsonb WHERE registry_id=%s", (status, name, url, clients_count, json.dumps(d), registry_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/unregister", methods=["POST", "OPTIONS"])
def registry_unregister_alias():
    """Registry注销(兼容)"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    registry_id = d.get("id")
    if not registry_id:
        return jsonify({"success": False, "message": "missing id"}), 400

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE servers SET status='disconnected', clients_count=0, last_seen=NOW() WHERE registry_id=%s", (registry_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})
# endregion


# region [SERVERS]
@app.route("/api/servers", methods=["GET", "POST", "OPTIONS"])
def servers_collection():
    """服务器列表"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    if request.method == "POST":
        d = _json()
        server_id = d.get("server_id") or gen_id("server")
        name = (d.get("name") or d.get("server_name") or "server").strip()
        url = (d.get("url") or d.get("server_url") or "").strip() or None

        conn = db()
        cur = conn.cursor()
        cur.execute("INSERT INTO servers(server_id, server_name, server_url, status, last_seen, registered_at, meta) VALUES(%s,%s,%s,'available',NOW(),NOW(),%s) ON CONFLICT (server_id) DO UPDATE SET server_name=EXCLUDED.server_name, server_url=EXCLUDED.server_url, status='available', last_seen=NOW()", (server_id, name, url, json.dumps(d)))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "server_id": server_id})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT server_id, server_name, server_url, port, clients_count, status, last_seen, assigned_user AS assigned_user_id, meta FROM servers ORDER BY COALESCE(server_name, server_id)")
    rows = cur.fetchall()
    conn.close()

    servers = []
    now_ts = time.time()
    offline_after = int(os.environ.get("SERVER_OFFLINE_AFTER_SECONDS", "120"))

    for r in rows:
        last_seen = r.get("last_seen")
        status = (r.get("status") or "disconnected").lower()
        clients_count = int(r.get("clients_count") or 0)

        if last_seen:
            try:
                age = now_ts - last_seen.timestamp()
                status_out = "disconnected" if age > offline_after else _normalize_server_status(status, clients_count)
            except Exception:
                status_out = _normalize_server_status(status, clients_count)
        else:
            status_out = _normalize_server_status(status, clients_count)

        meta = r.get("meta") or {}
        phone_number = meta.get("phone") or meta.get("phone_number") if isinstance(meta, dict) else None
        assigned_user_id = r.get("assigned_user_id")
        
        servers.append({
            "server_id": r.get("server_id"), "server_name": r.get("server_name") or r.get("server_id"),
            "server_url": r.get("server_url") or "", "status": status_out, "assigned_user_id": assigned_user_id,
            "is_assigned": assigned_user_id is not None, "is_private": assigned_user_id is not None,
            "is_public": assigned_user_id is None, "last_seen": r.get("last_seen").isoformat() if r.get("last_seen") else None,
            "phone_number": phone_number
        })

    return jsonify({"success": True, "servers": servers})


@app.route("/api/servers/<server_id>", methods=["DELETE", "GET", "OPTIONS"])
def servers_item(server_id: str):
    """服务器详情"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "GET":
        cur.execute("SELECT server_id, server_name, server_url, status, last_seen, assigned_user AS assigned_user_id FROM servers WHERE server_id=%s", (server_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "message": "not_found"}), 404
        return jsonify({"success": True, "server": row})

    cur2 = conn.cursor()
    cur2.execute("DELETE FROM servers WHERE server_id=%s", (server_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/servers/<server_id>/assign", methods=["POST", "OPTIONS"])
def server_assign(server_id: str):
    """服务器分配"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    user_id = d.get("user_id")
    if not user_id:
        return jsonify({"success": False, "message": "missing user_id"}), 400

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT server_id, assigned_user FROM servers WHERE server_id=%s", (server_id,))
    server = cur.fetchone()
    if not server:
        conn.close()
        return jsonify({"success": False, "message": "服务器不存在"}), 404
    
    cur.execute("SELECT user_id FROM users WHERE user_id=%s", (user_id,))
    user = cur.fetchone()
    if not user:
        conn.close()
        return jsonify({"success": False, "message": "用户不存在"}), 404
    
    current_assigned = server.get("assigned_user")
    if current_assigned and current_assigned != user_id:
        conn.close()
        return jsonify({"success": False, "message": f"服务器已分配给其他用户: {current_assigned}", "current_assigned_user": current_assigned}), 409

    cur2 = conn.cursor()
    cur2.execute("UPDATE servers SET assigned_user=%s WHERE server_id=%s", (user_id, server_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"服务器 {server_id} 已分配给用户 {user_id}", "server_id": server_id, "user_id": user_id})


@app.route("/api/servers/<server_id>/unassign", methods=["POST", "OPTIONS"])
def server_unassign(server_id: str):
    """服务器取消分配"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT server_id, assigned_user FROM servers WHERE server_id=%s", (server_id,))
    server = cur.fetchone()
    if not server:
        conn.close()
        return jsonify({"success": False, "message": "服务器不存在"}), 404
    
    current_assigned = server.get("assigned_user")
    if not current_assigned:
        conn.close()
        return jsonify({"success": False, "message": "服务器未分配给任何用户，无需取消"}), 400

    cur2 = conn.cursor()
    cur2.execute("UPDATE servers SET assigned_user=NULL WHERE server_id=%s", (server_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"服务器 {server_id} 已取消分配，现为公共服务器", "server_id": server_id, "previous_user": current_assigned})


@app.route("/api/servers/assigned/<user_id>", methods=["GET", "OPTIONS"])
def servers_assigned(user_id: str):
    """用户已分配服务器"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT server_id, server_name, server_url, status, last_seen FROM servers WHERE assigned_user=%s ORDER BY COALESCE(server_name, server_id)", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return jsonify({"success": True, "servers": rows})


@app.route("/api/users/<user_id>/available-servers", methods=["GET", "OPTIONS"])
def user_available_servers(user_id: str):
    """用户可用服务器"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT server_id, server_name, server_url, status, last_seen, meta FROM servers WHERE assigned_user=%s", (user_id,))
    exclusive = cur.fetchall()
    cur.execute("SELECT server_id, server_name, server_url, status, last_seen, meta FROM servers WHERE assigned_user IS NULL")
    shared = cur.fetchall()
    conn.close()

    def enrich(rows):
        out = []
        for r in rows:
            meta = r.get("meta") or {}
            phone_number = meta.get("phone") or meta.get("phone_number") if isinstance(meta, dict) else None
            out.append({"server_id": r.get("server_id"), "server_name": r.get("server_name") or r.get("server_id"), "server_url": r.get("server_url") or "", "status": r.get("status") or "disconnected", "last_seen": r.get("last_seen").isoformat() if r.get("last_seen") else None, "phone_number": phone_number})
        return out

    return jsonify({"success": True, "exclusive_servers": enrich(exclusive), "shared_servers": enrich(shared)})


@app.route("/api/user/<user_id>/servers", methods=["GET", "OPTIONS"])
@app.route("/api/api/user/<user_id>/servers", methods=["GET", "OPTIONS"])
def user_servers(user_id: str):
    """用户服务器列表"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT server_id FROM servers WHERE assigned_user=%s", (user_id,))
    ex = [i["server_id"] for i in cur.fetchall()]
    cur.execute("SELECT server_id FROM servers WHERE assigned_user IS NULL")
    shared = [i["server_id"] for i in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "shared": shared, "exclusive": ex, "all": shared + ex})


@app.route("/api/user/<user_id>/backends", methods=["GET", "OPTIONS"])
def user_backends(user_id: str):
    """用户后端列表"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    print(f"📡 获取用户后端列表: {user_id}")
    
    try:
        conn = db()
        authed_uid = _maybe_authed_user(conn)
        if authed_uid and authed_uid != user_id:
            conn.close()
            print(f"❌ 权限拒绝: {authed_uid} != {user_id}")
            return jsonify({"success": False, "message": "forbidden"}), 403

        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT server_id, server_name, server_url, status, last_seen, assigned_user AS assigned_user_id FROM servers WHERE assigned_user=%s OR assigned_user IS NULL ORDER BY COALESCE(server_name, server_id)", (user_id,))
        rows = cur.fetchall()
        conn.close()
        
        print(f"✅ 返回 {len(rows)} 个后端")
        return jsonify({"success": True, "backends": rows})
    except Exception as e:
        print(f"❌ 获取后端列表失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
# endregion


# region [ID LIBRARY SYNC]
@app.route("/api/id-library", methods=["GET", "POST", "OPTIONS"])
def id_library():
    """ID库同步 - 获取或保存所有ID"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if request.method == "GET":
        # 获取所有ID库记录
        cur.execute("SELECT apple_id, password, status, usage_status, created_at, updated_at FROM id_library ORDER BY created_at DESC")
        rows = cur.fetchall()
        accounts = []
        for row in rows:
            accounts.append({
                "appleId": row["apple_id"],
                "password": row["password"],
                "status": row["status"] or "normal",
                "usageStatus": row["usage_status"] or "new",
                "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
                "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None
            })
        conn.close()
        return jsonify({"success": True, "accounts": accounts})
    
    elif request.method == "POST":
        # 同步ID库（保存或更新）
        data = _json()
        accounts = data.get("accounts", [])
        
        if not isinstance(accounts, list):
            conn.close()
            return jsonify({"success": False, "message": "accounts must be a list"}), 400
        
        try:
            for account in accounts:
                apple_id = account.get("appleId", "").strip()
                password = account.get("password", "").strip()
                status = account.get("status", "normal")
                usage_status = account.get("usageStatus", "new")
                
                if not apple_id or not password:
                    continue
                
                # 使用UPSERT操作
                cur.execute("""
                    INSERT INTO id_library(apple_id, password, status, usage_status, created_at, updated_at)
                    VALUES(%s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (apple_id) DO UPDATE SET
                        password = EXCLUDED.password,
                        status = EXCLUDED.status,
                        usage_status = EXCLUDED.usage_status,
                        updated_at = NOW()
                """, (apple_id, password, status, usage_status))
            
            conn.commit()
            conn.close()
            return jsonify({"success": True, "message": f"同步了 {len(accounts)} 个账号"})
        except Exception as e:
            conn.rollback()
            conn.close()
            logger.error(f"ID库同步失败: {e}")
            return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/id-library/<apple_id>", methods=["DELETE", "PUT", "OPTIONS"])
def id_library_item(apple_id: str):
    """ID库单个记录操作"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if request.method == "DELETE":
        # 删除ID
        cur.execute("DELETE FROM id_library WHERE apple_id=%s", (apple_id,))
        conn.commit()
        deleted = cur.rowcount > 0
        conn.close()
        if deleted:
            return jsonify({"success": True, "message": "删除成功"})
        else:
            return jsonify({"success": False, "message": "账号不存在"}), 404
    
    elif request.method == "PUT":
        # 更新ID状态（usage_status）
        data = _json()
        usage_status = data.get("usageStatus", "new")
        
        if usage_status not in ["new", "used"]:
            conn.close()
            return jsonify({"success": False, "message": "usageStatus must be 'new' or 'used'"}), 400
        
        cur.execute("""
            UPDATE id_library 
            SET usage_status=%s, updated_at=NOW()
            WHERE apple_id=%s
        """, (usage_status, apple_id))
        conn.commit()
        updated = cur.rowcount > 0
        conn.close()
        if updated:
            return jsonify({"success": True, "message": "更新成功"})
        else:
            return jsonify({"success": False, "message": "账号不存在"}), 404
# endregion


# region [USER DATA]
def _resolve_user_id(cur, identifier: str) -> tuple:
    """通过user_id或username解析真实的user_id，返回(user_id, username)"""
    # 先尝试作为user_id查询
    cur.execute("SELECT user_id, username FROM users WHERE user_id=%s", (identifier,))
    row = cur.fetchone()
    if row:
        return row["user_id"], row["username"]
    # 再尝试作为username查询
    cur.execute("SELECT user_id, username FROM users WHERE username=%s", (identifier,))
    row = cur.fetchone()
    if row:
        return row["user_id"], row["username"]
    return None, None

@app.route("/api/user/<user_id>/credits", methods=["GET", "OPTIONS"])
def user_credits(user_id: str):
    """用户积分（支持user_id或username查询）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 解析用户标识（支持user_id或username）
    real_user_id, username = _resolve_user_id(cur, user_id)
    if not real_user_id:
        conn.close()
        return jsonify({"success": False, "message": "用户不存在"}), 404
    
    cur.execute("SELECT credits FROM user_data WHERE user_id=%s", (real_user_id,))
    row = cur.fetchone()
    conn.close()
    credits = float(row["credits"]) if row and row.get("credits") is not None else 0.0
    return jsonify({"success": True, "credits": credits, "user_id": real_user_id, "username": username})


@app.route("/api/user/<user_id>/deduct", methods=["POST", "OPTIONS"])
def user_deduct(user_id: str):
    """用户扣费"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    amount = d.get("amount") or d.get("credits")
    try:
        amount_f = float(amount)
    except Exception:
        amount_f = 0.0

    if amount_f <= 0:
        return jsonify({"success": False, "message": "invalid_amount"}), 400

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT credits, usage FROM user_data WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"success": False, "message": "user_not_found"}), 404

    credits = float(row.get("credits", 0))
    usage = row.get("usage") or []
    new_credits = max(0.0, credits - amount_f)
    usage.append({"action": "deduct", "amount": amount_f, "ts": now_iso(), "detail": d})

    cur2 = conn.cursor()
    cur2.execute("UPDATE user_data SET credits=%s, usage=%s WHERE user_id=%s", (new_credits, json.dumps(usage), user_id))
    conn.commit()
    conn.close()
    
    try:
        broadcast_user_update(user_id, 'balance_update', {'credits': new_credits, 'balance': new_credits, 'deducted': amount_f})
    except Exception as e:
        logger.warning(f"推送余额更新失败: {e}")
    
    return jsonify({"success": True, "credits": new_credits})


@app.route("/api/user/<user_id>/statistics", methods=["GET", "POST", "OPTIONS"])
def user_statistics(user_id: str):
    """用户统计（支持user_id或username查询）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 解析用户标识（支持user_id或username）
    real_user_id, username = _resolve_user_id(cur, user_id)
    if not real_user_id:
        conn.close()
        return jsonify({"success": False, "message": "用户不存在"}), 404

    if request.method == "GET":
        cur.execute("SELECT u.created, d.stats, d.usage FROM users u LEFT JOIN user_data d ON u.user_id = d.user_id WHERE u.user_id=%s", (real_user_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "message": "user_not_found"}), 404
        return jsonify({"success": True, "user_id": real_user_id, "username": username, "created": row.get("created").isoformat() if row.get("created") else None, "stats": row.get("stats") or [], "usage": row.get("usage") or []})

    d = _json()
    cur.execute("SELECT stats, usage FROM user_data WHERE user_id=%s", (real_user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"success": False, "message": "user_not_found"}), 404

    stats = row.get("stats") or []
    usage = row.get("usage") or []
    entry = dict(d.get("entry") or d)
    entry.setdefault("ts", now_iso())
    stats.append(entry)
    usage.append({"action": "statistics", "ts": now_iso(), "detail": entry})

    cur2 = conn.cursor()
    cur2.execute("UPDATE user_data SET stats=%s, usage=%s WHERE user_id=%s", (json.dumps(stats), json.dumps(usage), real_user_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/inbox/push", methods=["POST", "OPTIONS"])
def inbox_push():
    """收件箱推送"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    uid = d.get("user_id")
    phone = d.get("phone") or d.get("phone_number")
    text = d.get("text") or d.get("message")

    if not uid or not phone:
        return jsonify({"ok": False, "message": "missing user_id or phone"}), 400

    ts = now_iso()
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT inbox FROM user_data WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    inbox = (row.get("inbox") if row else None) or []
    inbox.append({"phone": phone, "text": text, "ts": ts})

    cur2 = conn.cursor()
    if row:
        cur2.execute("UPDATE user_data SET inbox=%s WHERE user_id=%s", (json.dumps(inbox), uid))
    else:
        cur2.execute("INSERT INTO user_data(user_id, inbox) VALUES(%s,%s)", (uid, json.dumps(inbox)))

    conn.commit()
    conn.close()
    
    try:
        broadcast_user_update(uid, 'inbox_update', {'phone': phone, 'text': text, 'ts': ts})
    except Exception as e:
        logger.warning(f"推送收件箱更新失败: {e}")
    
    return jsonify({"ok": True})


@app.route("/api/user/<user_id>/conversations", methods=["GET", "POST", "OPTIONS"])
def conversations_collection(user_id: str):
    """会话列表"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "GET":
        cur.execute("SELECT chat_id, meta, updated FROM conversations WHERE user_id=%s ORDER BY updated DESC", (user_id,))
        rows = cur.fetchall()
        conn.close()
        return jsonify({"success": True, "conversations": rows})

    d = _json()
    chat_id = (d.get("chat_id") or d.get("phone_number") or d.get("id") or "").strip()
    meta = d.get("meta")
    messages = d.get("messages")

    if not chat_id:
        conn.close()
        return jsonify({"success": False, "message": "missing chat_id"}), 400

    meta_json = json.dumps(meta or {})
    messages_json = json.dumps(messages) if messages is not None else None

    if messages_json is None:
        cur.execute("INSERT INTO conversations(user_id, chat_id, meta, updated) VALUES(%s,%s,%s::jsonb,NOW()) ON CONFLICT (user_id, chat_id) DO UPDATE SET meta = COALESCE(conversations.meta,'{}'::jsonb) || EXCLUDED.meta, updated = NOW()", (user_id, chat_id, meta_json))
    else:
        cur.execute("INSERT INTO conversations(user_id, chat_id, meta, messages, updated) VALUES(%s,%s,%s::jsonb,%s::jsonb,NOW()) ON CONFLICT (user_id, chat_id) DO UPDATE SET meta = COALESCE(conversations.meta,'{}'::jsonb) || EXCLUDED.meta, messages = EXCLUDED.messages, updated = NOW()", (user_id, chat_id, meta_json, messages_json))

    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/user/<user_id>/conversations/<chat_id>", methods=["DELETE", "OPTIONS"])
def conversations_delete(user_id: str, chat_id: str):
    """删除会话"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM conversations WHERE user_id=%s AND chat_id=%s", (user_id, chat_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/user/<user_id>/conversations/<chat_id>/messages", methods=["GET", "OPTIONS"])
def conversation_messages(user_id: str, chat_id: str):
    """会话消息"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT messages FROM conversations WHERE user_id=%s AND chat_id=%s", (user_id, chat_id))
    row = cur.fetchone()
    conn.close()
    msgs = row.get("messages") if row else []
    return jsonify({"success": True, "messages": msgs or []})


@app.route("/api/user/<user_id>/sent-records", methods=["GET", "POST", "OPTIONS"])
def sent_records(user_id: str):
    """发送记录"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "GET":
        cur.execute("SELECT phone_number, task_id, detail, ts FROM sent_records WHERE user_id=%s ORDER BY ts DESC LIMIT 500", (user_id,))
        rows = cur.fetchall()
        conn.close()
        return jsonify({"success": True, "records": rows})

    d = _json()
    phone = d.get("phone_number") or d.get("phone")
    task_id = d.get("task_id")

    cur2 = conn.cursor()
    cur2.execute("INSERT INTO sent_records(user_id, phone_number, task_id, detail) VALUES(%s,%s,%s,%s)", (user_id, phone, task_id, json.dumps(d)))
    conn.commit()
    conn.close()
    return jsonify({"success": True})
# endregion


# region [TASK]
def _split_numbers(nums, shard_size: int):
    """分片号码列表"""
    for i in range(0, len(nums), shard_size):
        yield nums[i : i + shard_size]


def _reclaim_stale_shards(conn) -> int:
    """回收超时分片"""
    stale_seconds = int(os.environ.get("SHARD_STALE_SECONDS", "600"))
    cur = conn.cursor()
    cur.execute("UPDATE shards SET status='pending', locked_at=NULL, updated=NOW(), attempts = attempts + 1 WHERE status='running' AND locked_at IS NOT NULL AND locked_at < NOW() - (%s * interval '1 second')", (stale_seconds,))
    reclaimed = cur.rowcount
    if reclaimed:
        conn.commit()
    return reclaimed


@app.route("/api/task/create", methods=["POST", "OPTIONS"])
@app.route("/api/api/task/create", methods=["POST", "OPTIONS"])
def create_task():


    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    uid = d.get("user_id")
    msg = d.get("message")
    nums = d.get("numbers") or []
    cnt = int(d.get("count", 1))

    if not uid or msg is None:
        return jsonify({"ok": False, "message": "missing user_id or message"}), 400
    if not isinstance(nums, list):
        return jsonify({"ok": False, "message": "numbers must be list"}), 400

    conn = db()
    token = _bearer_token()
    if token and not _verify_user_token(conn, uid, token):
        conn.close()
        return jsonify({"ok": False, "message": "invalid_token"}), 401
    
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT credits FROM user_data WHERE user_id=%s", (uid,))
    user_data = cur.fetchone()
    if not user_data:
        conn.close()
        return jsonify({"ok": False, "message": "user_not_found"}), 404
    
    credits = float(user_data.get("credits", 0))
    estimated_cost = len(nums) * float(os.environ.get("CREDIT_PER_SUCCESS", "1"))
    if credits < estimated_cost:
        conn.close()
        return jsonify({"ok": False, "message": "insufficient_credits", "credits": credits, "current": credits, "required": estimated_cost}), 400

    task_id = gen_id("task")
    shard_size = int(d.get("shard_size") or os.environ.get("SHARD_SIZE", "50"))

    _reclaim_stale_shards(conn)
    cur = conn.cursor()
    cur.execute("INSERT INTO tasks(task_id,user_id,message,total,count,status,created,updated) VALUES(%s,%s,%s,%s,%s,'pending',NOW(),NOW())", (task_id, uid, msg, len(nums), cnt))

    for group in _split_numbers(nums, shard_size):
        shard_id = gen_id("shard")
        cur.execute("INSERT INTO shards(shard_id,task_id,phones,status,updated) VALUES(%s,%s,%s,'pending',NOW())", (shard_id, task_id, json.dumps(group)))

    conn.commit()
    conn.close()
    
    # 🔥 核心改造：立即分配并推送分片到 Worker（消灭轮询）
    logger.info(f"🚀 任务 {task_id} 创建完成，立即分配推送...")
    assign_result = _assign_and_push_shards(task_id, uid, msg)
    
    # 更新任务状态为 running（如果有分片成功推送）
    if assign_result.get("pushed", 0) > 0:
        conn2 = db()
        cur2 = conn2.cursor()
        cur2.execute("UPDATE tasks SET status='running', updated=NOW() WHERE task_id=%s", (task_id,))
        conn2.commit()
        conn2.close()
        logger.info(f"✅ 任务 {task_id} 状态更新为 running")
    
    return jsonify({
        "ok": True, 
        "task_id": task_id,
        "assigned": assign_result.get("pushed", 0),
        "total_shards": assign_result.get("total", 0),
        "message": f"任务已创建并立即推送 {assign_result.get('pushed', 0)}/{assign_result.get('total', 0)} 个分片"
    })


@app.route("/api/task/assign", methods=["POST", "OPTIONS"])
@app.route("/api/api/task/assign", methods=["POST", "OPTIONS"])
def assign_task():
    """
    ⚠️ 已废弃端点 - 请使用 create_task 自动分配
    
    此端点在新架构中已不再需要。
    任务创建时会会自动分配并推送给 Worker。
    
    保留此端点仅用于向后兼容和手动重试失败的任务。
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    task_id = d.get("task_id")
    if not task_id:
        return jsonify({"ok": False, "msg": "missing task_id"}), 400
    
    logger.warning(f"⚠️ 调用了已废弃的端点 /api/task/assign，task_id={task_id}")
    logger.warning(f"⚠️ 提示：任务创建时已自动分配，无需手动调用此端点")

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT user_id, message FROM tasks WHERE task_id=%s", (task_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return jsonify({"ok": False, "msg": "task_not_found"}), 404

    uid = r["user_id"]
    msg = r["message"]
    conn.close()
    
    # 使用新的推送机制重新分配
    logger.info(f"🔄 手动重新分配任务 {task_id}...")
    assign_result = _assign_and_push_shards(task_id, uid, msg)
    
    return jsonify({
        "ok": True,
        "deprecated": True,
        "message": "任务已通过 WebSocket 推送机制重新分配",
        "assigned": assign_result.get("pushed", 0),
        "total": assign_result.get("total", 0)
    })


@app.route("/api/server/<server_id>/shards", methods=["GET", "OPTIONS"])
def server_shards(server_id: str):

    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    logger.warning(f"⚠️ Worker {server_id} 调用了已废弃的轮询端点 /api/server/<server_id>/shards")
    logger.warning(f"⚠️ 提示：请升级 Worker 以使用 WebSocket 推送机制")

    # 返回空列表，鼓励使用 WebSocket
    return jsonify({
        "ok": True, 
        "shards": [], 
        "reclaimed": 0,
        "deprecated": True,
        "message": "此端点已废弃，请使用 WebSocket 推送机制。任务会自动推送到 Worker，无需轮询。"
    })


# @app.route("/api/server/report", methods=["POST", "OPTIONS"])  # 注释掉HTTP上报，强制使用WS
# def server_report():
#     """服务器上报"""
#     ...  # 保持代码但注释


def report_shard_result(shard_id: str, sid: str, uid: str, suc: int, fail: int, detail: dict):
    """处理分片结果上报逻辑"""
    sent = suc + fail
    credits = float(suc) * float(os.environ.get("CREDIT_PER_SUCCESS", "1"))

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT 1 FROM reports WHERE shard_id=%s", (shard_id,))
    already = cur.fetchone() is not None

    if not already:
        cur2 = conn.cursor()
        cur2.execute("INSERT INTO reports(shard_id,server_id,user_id,success,fail,sent,credits,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)", (shard_id, sid, uid, suc, fail, sent, credits, json.dumps(detail)))
        cur2.execute("UPDATE shards SET status='done', result=%s, updated=NOW() WHERE shard_id=%s", (json.dumps({"success": suc, "fail": fail, "sent": sent}), shard_id))

        cur.execute("SELECT credits,usage FROM user_data WHERE user_id=%s", (uid,))
        r = cur.fetchone()
        if r:
            c = float(r.get("credits", 0))
            log = r.get("usage") or []
        else:
            c = 0.0
            log = []
            cur2.execute("INSERT INTO user_data(user_id, credits, usage) VALUES(%s,%s,%s)", (uid, 0, json.dumps([])))

        new_c = max(0.0, c - credits)
        log.append({"sid": sid, "shard": shard_id, "success": suc, "fail": fail, "sent": sent, "credits": credits, "ts": now_iso()})
        cur2.execute("UPDATE user_data SET credits=%s, usage=%s WHERE user_id=%s", (new_c, json.dumps(log), uid))
    else:
        cur2 = conn.cursor()
        cur2.execute("UPDATE shards SET status='done', updated=NOW() WHERE shard_id=%s", (shard_id,))

    cur.execute("SELECT task_id FROM shards WHERE shard_id=%s", (shard_id,))
    task_row = cur.fetchone()
    task_id = task_row.get("task_id") if task_row else None
    
    cur.execute("SELECT COUNT(*) FILTER (WHERE status='done') AS done, COUNT(*) AS total FROM shards WHERE task_id = (SELECT task_id FROM shards WHERE shard_id=%s)", (shard_id,))
    row = cur.fetchone()
    task_completed = False
    if row:
        done_cnt = int(row.get("done", 0))
        total_cnt = int(row.get("total", 0))
        if total_cnt > 0 and done_cnt >= total_cnt:
            cur2 = conn.cursor()
            cur2.execute("UPDATE tasks SET status='done', updated=NOW() WHERE task_id = (SELECT task_id FROM shards WHERE shard_id=%s)", (shard_id,))
            task_completed = True

    conn.commit()
    
    if task_id:
        cur.execute("SELECT COUNT(*) FILTER (WHERE status='pending') AS pending, COUNT(*) FILTER (WHERE status='running') AS running, COUNT(*) FILTER (WHERE status='done') AS done, COUNT(*) AS total FROM shards WHERE task_id=%s", (task_id,))
        shard_counts = cur.fetchone() or {}
        cur.execute("SELECT COALESCE(SUM(success),0) AS success, COALESCE(SUM(fail),0) AS fail, COALESCE(SUM(sent),0) AS sent FROM reports WHERE shard_id IN (SELECT shard_id FROM shards WHERE task_id=%s)", (task_id,))
        result_counts = cur.fetchone() or {}
        cur.execute("SELECT status FROM tasks WHERE task_id=%s", (task_id,))
        task_status_row = cur.fetchone()
        task_status_val = task_status_row.get("status") if task_status_row else "running"
        
        update_data = {"task_id": task_id, "status": task_status_val, "shards": {"pending": int(shard_counts.get("pending", 0)), "running": int(shard_counts.get("running", 0)), "done": int(shard_counts.get("done", 0)), "total": int(shard_counts.get("total", 0))}, "result": {"success": int(result_counts.get("success", 0)), "fail": int(result_counts.get("fail", 0)), "sent": int(result_counts.get("sent", 0))}, "credits": new_c if not already else None, "completed": task_completed}
        
        try:
            broadcast_task_update(task_id, update_data)
        except Exception as e:
            logger.warning(f"推送任务更新失败: {e}")
    
    conn.close()
    return {"ok": True, "deducted": (not already)}


@app.route("/api/task/<task_id>/status", methods=["GET", "OPTIONS"])
def task_status(task_id: str):
    """任务状态"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    _reclaim_stale_shards(conn)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT task_id, user_id, message, total, status, created, updated FROM tasks WHERE task_id=%s", (task_id,))
    task = cur.fetchone()
    if not task:
        conn.close()
        return jsonify({"success": False, "message": "task_not_found"}), 404

    cur.execute("SELECT COUNT(*) FILTER (WHERE status='pending') AS pending, COUNT(*) FILTER (WHERE status='running') AS running, COUNT(*) FILTER (WHERE status='done') AS done, COUNT(*) AS total FROM shards WHERE task_id=%s", (task_id,))
    shard_counts = cur.fetchone() or {}

    cur.execute("SELECT COALESCE(SUM(success),0) AS success, COALESCE(SUM(fail),0) AS fail, COALESCE(SUM(sent),0) AS sent FROM reports WHERE shard_id IN (SELECT shard_id FROM shards WHERE task_id=%s)", (task_id,))
    rep = cur.fetchone() or {}
    conn.close()

    return jsonify({"ok": True, "success": True, "task_id": task_id, "user_id": task.get("user_id"), "message": task.get("message", ""), "status": task["status"], "total": task["total"], "shards": {"pending": int(shard_counts.get("pending", 0)), "running": int(shard_counts.get("running", 0)), "done": int(shard_counts.get("done", 0)), "total": int(shard_counts.get("total", 0))}, "result": {"success": int(rep.get("success", 0)), "fail": int(rep.get("fail", 0)), "sent": int(rep.get("sent", 0))}, "created": task["created"].isoformat() if task.get("created") else None, "updated": task["updated"].isoformat() if task.get("updated") else None, "task": task})


@app.route("/api/task/<task_id>/events", methods=["GET", "OPTIONS"])
def task_events_sse(task_id: str):
    """任务SSE事件"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    interval = float(request.args.get("interval", "1"))
    max_seconds = int(request.args.get("max_seconds", "3600"))
    start = time.time()

    def gen():
        last_payload = None
        while True:
            if time.time() - start > max_seconds:
                yield "event: end\ndata: {}\n\n"
                return
            try:
                conn = db()
                _reclaim_stale_shards(conn)
                cur = conn.cursor(cursor_factory=RealDictCursor)
                cur.execute("SELECT COUNT(*) FILTER (WHERE status='pending') AS pending, COUNT(*) FILTER (WHERE status='running') AS running, COUNT(*) FILTER (WHERE status='done') AS done, COUNT(*) AS total FROM shards WHERE task_id=%s", (task_id,))
                sc = cur.fetchone() or {}
                cur.execute("SELECT COALESCE(SUM(success),0) AS success, COALESCE(SUM(fail),0) AS fail, COALESCE(SUM(sent),0) AS sent FROM reports WHERE shard_id IN (SELECT shard_id FROM shards WHERE task_id=%s)", (task_id,))
                rp = cur.fetchone() or {}
                cur.execute("SELECT status FROM tasks WHERE task_id=%s", (task_id,))
                ts = (cur.fetchone() or {}).get("status")
                conn.close()
                payload = {"task_id": task_id, "status": ts, "shards": sc, "result": rp}
                payload_s = json.dumps(payload, ensure_ascii=False)
                if payload_s != last_payload:
                    last_payload = payload_s
                    yield f"data: {payload_s}\n\n"
                if ts == "done":
                    yield "event: end\ndata: {}\n\n"
                    return
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            time.sleep(interval)

    return Response(stream_with_context(gen()), mimetype="text/event-stream")
# endregion


# region [INBOX & HEARTBEAT]
@app.route("/api/user/<user_id>/inbox", methods=["GET", "OPTIONS"])
def user_inbox(user_id: str):
    """用户收件箱"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT inbox FROM user_data WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    inbox = json.loads(row["inbox"]) if row and row["inbox"] else []
    
    cur.execute("SELECT chat_id, meta, messages, updated FROM conversations WHERE user_id=%s ORDER BY updated DESC", (user_id,))
    conversations = cur.fetchall()
    conn.close()
    
    chat_list = []
    for conv in conversations:
        meta = json.loads(conv["meta"]) if isinstance(conv["meta"], str) else (conv["meta"] or {})
        messages = json.loads(conv["messages"]) if isinstance(conv["messages"], str) else (conv["messages"] or [])
        last_message = messages[-1] if messages else None
        last_message_preview = ""
        if last_message:
            last_message_preview = (last_message.get("text", last_message.get("message", ""))[:50] if isinstance(last_message, dict) else str(last_message)[:50])
        chat_list.append({"chat_id": conv["chat_id"], "name": meta.get("name", meta.get("phone_number", conv["chat_id"])), "phone_number": meta.get("phone_number", conv["chat_id"]), "last_message_preview": last_message_preview, "updated": conv["updated"].isoformat() if conv["updated"] else None})
    
    return jsonify({"ok": True, "inbox": inbox, "conversations": chat_list})


@app.route("/api/backend/heartbeat", methods=["POST", "OPTIONS"])
def backend_heartbeat():
    """后端心跳"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    d = _json()
    server_id = d.get("server_id")
    if not server_id:
        return jsonify({"ok": False, "message": "missing server_id"}), 400
    
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE servers SET status='connected', updated=NOW() WHERE server_id=%s", (server_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "heartbeat_received"})
# endregion


# region [COMPAT]
@app.route("/api/admin/assign", methods=["POST", "OPTIONS"])
def admin_assign_alias():
    """管理员分配(兼容)"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    server_id = d.get("server_id")
    user_id = d.get("user_id")
    if not server_id or not user_id:
        return jsonify({"ok": False, "message": "missing server_id/user_id"}), 400

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE servers SET assigned_user=%s WHERE server_id=%s", (user_id, server_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})
# endregion


# region [FRONTEND WEBSOCKET]
@sock.route('/ws/frontend')
def frontend_websocket(ws):
    """前端WebSocket端点 - 用于前端前端订阅任务和用户更新"""
    client_id = id(ws)  # 使用WebSocket对象ID作为唯一标识
    user_id = None
    subscribed_tasks = set()
    
    try:
        logger.info(f"前端WS连接建立: {client_id}")
        
        # 注册客户端
        with _frontend_lock:
            _frontend_clients[client_id] = {
                "ws": ws,
                "user_id": None,
                "subscribed_tasks": set(),
                "connected_at": time.time()
            }
        
        while True:
            try:
                data = ws.receive(timeout=60)
                if data is None:
                    break
                
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    ws.send(json.dumps({"type": "error", "message": "invalid_json"}))
                    continue
                
                action = msg.get("action")
                payload = msg.get("data", {})
                
                if action == "subscribe_user":
                    # 订阅用户更新
                    user_id = payload.get("user_id")
                    if user_id:
                        with _frontend_lock:
                            _frontend_clients[client_id]["user_id"] = user_id
                        ws.send(json.dumps({"type": "user_subscribed", "user_id": user_id, "ok": True}))
                        logger.info(f"前端订阅用户: {user_id}")
                
                elif action == "subscribe_task":
                    # 订阅任务更新
                    task_id = payload.get("task_id")
                    if task_id:
                        with _frontend_lock:
                            _frontend_clients[client_id]["subscribed_tasks"].add(task_id)
                            if task_id not in _task_subscribers:
                                _task_subscribers[task_id] = set()
                            _task_subscribers[task_id].add(client_id)
                        ws.send(json.dumps({"type": "subscribed", "task_id": task_id, "ok": True}))
                        logger.info(f"前端订阅任务: {task_id}")
                
                elif action == "unsubscribe_task":
                    # 取消订阅任务
                    task_id = payload.get("task_id")
                    if task_id:
                        with _frontend_lock:
                            if client_id in _frontend_clients:
                                _frontend_clients[client_id]["subscribed_tasks"].discard(task_id)
                            if task_id in _task_subscribers:
                                _task_subscribers[task_id].discard(client_id)
                                if not _task_subscribers[task_id]:
                                    del _task_subscribers[task_id]
                        ws.send(json.dumps({"type": "unsubscribed", "task_id": task_id, "ok": True}))
                
                elif action == "ping":
                    # 心跳响应
                    ws.send(json.dumps({"type": "pong", "ts": now_iso()}))
                
            except Exception as e:
                if "timed out" not in str(e).lower():
                    logger.warning(f"前端WS消息处理错误: {e}")
                    break
    
    except Exception as e:
        logger.warning(f"前端WS错误: {e}")
    
    finally:
        # 清理连接
        with _frontend_lock:
            if client_id in _frontend_clients:
                client = _frontend_clients[client_id]
                # 清理任务订阅
                for task_id in client.get("subscribed_tasks", set()):
                    if task_id in _task_subscribers:
                        _task_subscribers[task_id].discard(client_id)
                        if not _task_subscribers[task_id]:
                            del _task_subscribers[task_id]
                del _frontend_clients[client_id]
        logger.info(f"前端WS断开: {client_id}")


def broadcast_task_update(task_id: str, update_data: dict):
    """推送任务更新到所有订阅的前端客户端"""
    if task_id not in _task_subscribers:
        return
    
    payload = json.dumps({'type': 'task_update', 'task_id': task_id, 'data': update_data})
    
    with _frontend_lock:
        subscribers = list(_task_subscribers.get(task_id, []))
    
    failed_clients = []
    for client_id in subscribers:
        with _frontend_lock:
            client = _frontend_clients.get(client_id)
        if client:
            try:
                client["ws"].send(payload)
            except Exception as e:
                logger.warning(f"推送任务更新失败 {client_id}: {e}")
                failed_clients.append(client_id)
    
    # 清理失败的连接
    if failed_clients:
        with _frontend_lock:
            for client_id in failed_clients:
                if client_id in _frontend_clients:
                    del _frontend_clients[client_id]
                if task_id in _task_subscribers:
                    _task_subscribers[task_id].discard(client_id)


def broadcast_user_update(user_id: str, update_type: str, data: dict):
    """推送用户更新到所有订阅该用户的前端客户端"""
    payload = json.dumps({'type': update_type, 'user_id': user_id, 'data': data, 'ts': now_iso()})
    
    failed_clients = []
    with _frontend_lock:
        clients_to_notify = [(cid, c) for cid, c in _frontend_clients.items() if c.get("user_id") == user_id]
    
    for client_id, client in clients_to_notify:
        try:
            client["ws"].send(payload)
        except Exception as e:
            logger.warning(f"推送用户更新失败 {client_id}: {e}")
            failed_clients.append(client_id)
    
    # 清理失败的连接
    if failed_clients:
        with _frontend_lock:
            for client_id in failed_clients:
                if client_id in _frontend_clients:
                    del _frontend_clients[client_id]
# endregion


# region [WORKER WEBSOCKET]
@sock.route('/ws/worker')
def worker_websocket(ws):
    """Worker WebSocket端点 - 用于macOS客户端连接"""
    server_id = None
    try:
        print("✅ Worker WS连接建立")
        
        while True:
            try:
                data = ws.receive(timeout=60)
                if data is None:
                    break
                
                msg = json.loads(data)
                action = msg.get("action")
                payload = msg.get("data", {})
                
                if action == "register":
                    server_id = payload.get("server_id")
                    server_name = payload.get("server_name", "")
                    meta = payload.get("meta", {})
                    
                    if server_id:
                        # ✅ 1. 存储WebSocket连接到内存
                        with _worker_lock:
                            _worker_clients[server_id] = {
                                "ws": ws,
                                "server_name": server_name,
                                "meta": meta,
                                "ready": meta.get("ready", False),
                                "connected_at": time.time()
                            }
                        
                        # ✅ 2. 使用Redis/内存标记在线状态
                        redis_manager.worker_online(server_id, {
                            "server_name": server_name,
                            "ready": meta.get("ready", False),
                            "load": 0,
                            "meta": json.dumps(meta)
                        })
                        
                        ws.send(json.dumps({"type": "registered", "server_id": server_id, "ok": True}))
                        print(f"✅ Worker注册成功: {server_id}")
                
                elif action == "ready":
                    if server_id:
                        ready = payload.get("ready", False)
                        # ✅ 更新内存中的就绪状态
                        with _worker_lock:
                            if server_id in _worker_clients:
                                _worker_clients[server_id]["ready"] = ready
                        
                        # ✅ 更新Redis中的就绪状态
                        redis_manager.update_heartbeat(server_id)
                        print(f"Worker就绪状态: {server_id} ready={ready}")
                
                elif action == "heartbeat":
                    if server_id:
                        # ✅ 更新心跳
                        redis_manager.update_heartbeat(server_id)
                        ws.send(json.dumps({"type": "heartbeat_ack", "ok": True}))
                
                elif action == "shard_result":
                    # Worker上报结果
                    shard_id = payload.get("shard_id")
                    success = int(payload.get("success", 0))
                    fail = int(payload.get("fail", 0))
                    uid = payload.get("user_id")
                    
                    if shard_id and uid and server_id:
                        # ✅ 减少该Worker的负载
                        current_load = redis_manager.get_worker_load(server_id)
                        new_load = max(0, current_load - 1)
                        redis_manager.set_worker_load(server_id, new_load)
                        
                        # 原有的结果处理逻辑
                        result = report_shard_result(shard_id, server_id, uid, success, fail, payload)
                        ws.send(json.dumps({"type": "shard_result_ack", "shard_id": shard_id, **result}))
            
            except Exception as e:
                if "timed out" not in str(e).lower():
                    break
    
    except Exception as e:
        print(f"Worker WS错误: {e}")
    
    finally:
        # ✅ 清理Worker状态
        if server_id:
            with _worker_lock:
                _worker_clients.pop(server_id, None)
            
            redis_manager.worker_offline(server_id)
            print(f"Worker断开: {server_id}")
            
def send_shard_to_worker(server_id: str, shard: dict) -> bool:
    """向指定worker发送分片任务 - 通过WebSocket立即推送"""
    with _worker_lock:
        client = _worker_clients.get(server_id)
        if not client:
            logger.warning(f"Worker {server_id} 未连接")
            return False
        if not client.get("ready"):
            logger.warning(f"Worker {server_id} 未就绪")
            return False
        try:
            ws = client["ws"]
            ws.send(json.dumps({"type": "shard_run", "shard": shard}))
            logger.info(f"✅ 推送分片 {shard.get('shard_id', 'unknown')[:8]}... 到 Worker {server_id}")
            return True
        except Exception as e:
            logger.warning(f"❌ 发送分片到 Worker {server_id} 失败: {e}")
            return False

def _assign_and_push_shards(task_id: str, user_id: str, message: str) -> dict:
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # 1. ✅ 从Redis获取在线Worker（不再是内存）
        available_servers = redis_manager.get_online_workers()
        
        if not available_servers:
            print(f"❌ 任务 {task_id} 无可用Worker")
            conn.close()
            return {"total": 0, "pushed": 0, "failed": 0}
        
        print(f"✅ 任务 {task_id} 可用Worker: {len(available_servers)} 个")
        
        # 2. 获取待处理分片
        cur.execute("""
            SELECT shard_id, phones 
            FROM shards 
            WHERE task_id=%s AND status='pending'
            ORDER BY shard_id
        """, (task_id,))
        pending_shards = cur.fetchall()
        
        if not pending_shards:
            conn.close()
            return {"total": 0, "pushed": 0, "failed": 0}
        
        # 3. ✅ 智能分配（基于负载）
        total_shards = len(pending_shards)
        pushed_count = 0
        
        for idx, shard_row in enumerate(pending_shards):
            shard_id = shard_row.get("shard_id")
            phones = shard_row.get("phones")
            
            # ✅ 选择负载最轻的Worker
            best_worker = None
            min_load = float('inf')
            
            for worker_id in available_servers:
                load = redis_manager.get_worker_load(worker_id)
                if load < min_load:
                    min_load = load
                    best_worker = worker_id
            
            if not best_worker:
                continue
            
            # ✅ 增加该Worker的负载
            redis_manager.set_worker_load(best_worker, min_load + 1)
            
            # 推送分片
            shard_data = {
                "shard_id": shard_id,
                "task_id": task_id,
                "user_id": user_id,
                "phones": phones,
                "message": message
            }
            
            push_success = send_shard_to_worker(best_worker, shard_data)
            
            if push_success:
                # 更新数据库
                cur2 = conn.cursor()
                cur2.execute("""
                    UPDATE shards 
                    SET server_id=%s, status='running', locked_at=NOW(), updated=NOW() 
                    WHERE shard_id=%s
                """, (best_worker, shard_id))
                pushed_count += 1
        
        conn.commit()
        conn.close()
        
        print(f"✅ 任务 {task_id} 分配完成: 总计 {total_shards}, 成功 {pushed_count}")
        return {"total": total_shards, "pushed": pushed_count, "failed": total_shards - pushed_count}
    
    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"❌ 分配任务 {task_id} 失败: {e}")
        return {"total": 0, "pushed": 0, "failed": 0}

def get_ready_workers() -> list:
    """获取所有就绪的worker"""
    with _worker_lock:
        return [
            {"server_id": sid, "server_name": c.get("server_name", ""), "ready": c.get("ready", False)}
            for sid, c in _worker_clients.items()
            if c.get("ready")
        ]
# endregion



# region [MAIN]

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    
    # 使用gevent
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler
    
    server = pywsgi.WSGIServer(('0.0.0.0', port), app, handler_class=WebSocketHandler)
    print(f"Server starting on port {port} with gevent...")
    server.serve_forever()
# endregion

# endregion