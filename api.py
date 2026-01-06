#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# region API
from gevent import monkey
monkey.patch_all()

# region [IMPORTS]
import os
import json
import time
import secrets
import hashlib
import sys
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

from flask import Flask, request, jsonify, Response, stream_with_context, send_from_directory, make_response
from flask_cors import CORS
from flask_sock import Sock

import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse
# endregion

# region [APP INIT]
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', stream=sys.stdout)
logger = logging.getLogger(__name__)

# region ws dbg
def _wsdbg(hypothesisId: str, location: str, message: str, data: dict):
    """WS debugging logs (internal use)."""
    try:
        payload = {
            "sessionId": "debug-session",
            "runId": "ws-flap-railway1",
            "hypothesisId": hypothesisId,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
        }
        logger.info("[WSDBG] " + json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass
# endregion



app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
}, supports_credentials=True)

sock = Sock(app)



# 获取项目根目录（index.html所在位置）
BASE_DIR = Path(__file__).resolve().parent.parent

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


# 获取数据库连接
from psycopg2 import pool

# Database Connection Pool
_db_pool = None

def _init_db_pool():
    global _db_pool
    if _db_pool is None:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
             # Default fallback for local development
            default_db_config = {
                "host": os.environ.get("DB_HOST", "localhost"),
                "port": os.environ.get("DB_PORT", "5555"),
                "database": os.environ.get("DB_NAME", "autosender"),
                "user": os.environ.get("DB_USER", "autosender"), 
                "password": os.environ.get("DB_PASSWORD", "autosender123")
            }
            database_url = f"postgresql://{default_db_config['user']}:{default_db_config['password']}@{default_db_config['host']}:{default_db_config['port']}/{default_db_config['database']}"
        
        try:
            # Create a thread-safe connection pool
            _db_pool = psycopg2.pool.ThreadedConnectionPool(1, 20, database_url)
            logger.info("Database connection pool initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize database pool: {e}")
            raise

class PooledConnectionWrapper:
    """Wrapper to return connection to pool on close() instead of closing it."""
    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn
        self._closed = False

    def close(self):
        if not self._closed and self._conn:
            self._pool.putconn(self._conn)
            self._closed = True
    
    def __getattr__(self, name):
        return getattr(self._conn, name)

def db():
    global _db_pool
    if _db_pool is None:
        _init_db_pool()
    
    try:
        conn = _db_pool.getconn()
        return PooledConnectionWrapper(_db_pool, conn)
    except Exception as e:
        logger.error(f"Failed to get connection from pool: {e}")
        raise RuntimeError(f"Database connection failure: {e}") from e


def now_iso() -> str:
    """获取当前UTC时间ISO格式"""
    return datetime.now(timezone.utc).isoformat()


def gen_id(prefix: str) -> str:
    """生成带前缀的4位短ID（人类可读）"""
    # 用户ID使用4位纯数字（0000-9999），无前缀
    if prefix == "u":
        short_id = ''.join(secrets.choice("0123456789") for _ in range(4))
        return short_id  # 返回纯4位数字，无前缀
    # 其他ID使用数字和大写字母，排除容易混淆的字符（0,O,1,I,L）
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
    """验证用户Token（检查是否过期）"""
    if not user_id or not token:
        return False
    th = hash_token(token)
    cur = conn.cursor()
    # 检查token是否存在且未过期
    cur.execute("SELECT 1 FROM user_tokens WHERE user_id=%s AND token_hash=%s AND (expires_at IS NULL OR expires_at > NOW())", (user_id, th))
    ok = cur.fetchone() is not None
    if ok:
        cur.execute("UPDATE user_tokens SET last_used=NOW() WHERE user_id=%s AND token_hash=%s", (user_id, th))
        conn.commit()
    return ok

def _verify_admin_token(conn, admin_id_or_token: str, token: str = None) -> Optional[str]:
    """验证管理员Token（检查是否过期）
    支持两种调用方式:
    1. _verify_admin_token(conn, admin_id, token) - 验证指定管理员
    2. _verify_admin_token(conn, token) - 从Token查找并验证管理员 (此时admin_id_or_token为token)
    """
    if token is None:
        # 方式2: 只传入了token
        token = admin_id_or_token
        admin_id = None
    else:
        # 方式1: 传入了admin_id和token
        admin_id = admin_id_or_token

    if not token:
        return None
        
    th = hash_token(token)
    cur = conn.cursor()
    
    if admin_id:
        # 验证指定管理员
        cur.execute("SELECT 1 FROM admin_tokens WHERE admin_id=%s AND token_hash=%s AND (expires_at IS NULL OR expires_at > NOW())", (admin_id, th))
        ok = cur.fetchone() is not None
        if ok:
            cur.execute("UPDATE admin_tokens SET last_used=NOW() WHERE admin_id=%s AND token_hash=%s", (admin_id, th))
            conn.commit()
            sys_log("INFO", "AdminAuth", f"Admin {admin_id} accessed with token.", {"token_hash_prefix": th[:8]})
        return admin_id if ok else None
    else:
        # 从Token查找管理员
        cur.execute("SELECT admin_id FROM admin_tokens WHERE token_hash=%s AND (expires_at IS NULL OR expires_at > NOW()) LIMIT 1", (th,))
        row = cur.fetchone()
        found_admin_id = row[0] if row else None
        
        if found_admin_id:
            cur.execute("UPDATE admin_tokens SET last_used=NOW() WHERE admin_id=%s AND token_hash=%s", (found_admin_id, th))
            conn.commit()
            sys_log("INFO", "AdminAuth", f"Admin {found_admin_id} accessed with token.", {"token_hash_prefix": th[:8]})
            return found_admin_id
        return None

# 获取系统日志接口
@app.route("/api/admin/logs", methods=["GET"])
def get_system_logs():
    token = _bearer_token()
    conn = db()
    admin_id = _verify_admin_token(conn, token)
    
    if not admin_id:
        conn.close()
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
        
    try:
        limit = int(request.args.get("limit", 100))
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, level, module, message, detail, ts 
            FROM system_logs 
            ORDER BY id DESC 
            LIMIT %s
        """, (limit,))
        logs = cur.fetchall()
        
        # 转换时间对象
        for log in logs:
            if log.get("ts"):
                log["ts"] = log["ts"].isoformat()
        
        conn.close()
        return jsonify({"ok": True, "logs": logs})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"ok": False, "error": str(e)}), 500

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
# 初始化数据库表
def init_db() -> None:
    print("[INFO] 正在连接数据库...")
    conn = db()
    try:
        cur = conn.cursor()
        print("[INFO] 正在创建表...")

        if os.environ.get("RESET_DB", "").strip() == "1":
            cur.execute("DROP TABLE IF EXISTS users CASCADE")
            cur.execute("DROP TABLE IF EXISTS user_data CASCADE")
            cur.execute("DROP TABLE IF EXISTS user_tokens CASCADE")
            cur.execute("DROP TABLE IF EXISTS admins CASCADE")
            cur.execute("DROP TABLE IF EXISTS admin_tokens CASCADE")
            cur.execute("DROP TABLE IF EXISTS admin_configs CASCADE")
            cur.execute("DROP TABLE IF EXISTS server_manager_tokens CASCADE")
            cur.execute("DROP TABLE IF EXISTS settings CASCADE")
            cur.execute("DROP TABLE IF EXISTS servers CASCADE")
            cur.execute("DROP TABLE IF EXISTS tasks CASCADE")
            cur.execute("DROP TABLE IF EXISTS shards CASCADE")
            cur.execute("DROP TABLE IF EXISTS reports CASCADE")
            cur.execute("DROP TABLE IF EXISTS conversations CASCADE")
            cur.execute("DROP TABLE IF EXISTS sent_records CASCADE")
            cur.execute("DROP TABLE IF EXISTS id_library CASCADE")

        cur.execute("""CREATE TABLE IF NOT EXISTS users(user_id VARCHAR PRIMARY KEY, username VARCHAR UNIQUE NOT NULL, pw_hash VARCHAR NOT NULL, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, created_by_admin VARCHAR)""")
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_by_admin VARCHAR")
        except:
            pass
        cur.execute("""CREATE TABLE IF NOT EXISTS user_data(user_id VARCHAR PRIMARY KEY, credits NUMERIC DEFAULT 1000, stats JSONB DEFAULT '[]'::jsonb, usage JSONB DEFAULT '[]'::jsonb, inbox JSONB DEFAULT '[]'::jsonb, FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS user_tokens(token_hash VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_used TIMESTAMP, expires_at TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE)""")
        
        try:
            cur.execute("ALTER TABLE user_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP")
        except:
            pass
        cur.execute("""CREATE TABLE IF NOT EXISTS admins(admin_id VARCHAR PRIMARY KEY, pw_hash VARCHAR NOT NULL, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS admin_tokens(token_hash VARCHAR PRIMARY KEY, admin_id VARCHAR NOT NULL, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_used TIMESTAMP, expires_at TIMESTAMP, FOREIGN KEY(admin_id) REFERENCES admins(admin_id) ON DELETE CASCADE)""")
        
        try:
            cur.execute("ALTER TABLE admin_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP")
        except:
            pass
        cur.execute("""CREATE TABLE IF NOT EXISTS admin_configs(admin_id VARCHAR PRIMARY KEY, selected_servers JSONB DEFAULT '[]'::jsonb, user_groups JSONB DEFAULT '[]'::jsonb, updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(admin_id) REFERENCES admins(admin_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS server_manager_tokens(token_hash VARCHAR PRIMARY KEY, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_used TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS settings(key VARCHAR PRIMARY KEY, value TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS servers(server_id VARCHAR PRIMARY KEY, server_name VARCHAR, server_url TEXT, port INT, clients_count INT DEFAULT 0, status VARCHAR DEFAULT 'disconnected', last_seen TIMESTAMP, registered_at TIMESTAMP, registry_id VARCHAR, meta JSONB DEFAULT '{}'::jsonb, assigned_user VARCHAR, assigned_by_admin VARCHAR, FOREIGN KEY(assigned_user) REFERENCES users(user_id) ON DELETE SET NULL)""")
        try:
            cur.execute("ALTER TABLE servers ADD COLUMN IF NOT EXISTS assigned_by_admin VARCHAR")
        except:
            pass
        cur.execute("""CREATE TABLE IF NOT EXISTS tasks(task_id VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, message TEXT NOT NULL, total INT, count INT, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status VARCHAR DEFAULT 'pending', FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS shards(shard_id VARCHAR PRIMARY KEY, task_id VARCHAR NOT NULL, server_id VARCHAR, phones JSONB NOT NULL, status VARCHAR DEFAULT 'pending', attempts INT DEFAULT 0, locked_at TIMESTAMP, updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, result JSONB DEFAULT '{}'::jsonb, FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE, FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE SET NULL)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS reports(report_id SERIAL PRIMARY KEY, shard_id VARCHAR, server_id VARCHAR, user_id VARCHAR, success INT, fail INT, sent INT, credits NUMERIC, detail JSONB, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS conversations(user_id VARCHAR NOT NULL, chat_id VARCHAR NOT NULL, meta JSONB DEFAULT '{}'::jsonb, messages JSONB DEFAULT '[]'::jsonb, updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, chat_id), FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS sent_records(id SERIAL PRIMARY KEY, user_id VARCHAR NOT NULL, phone_number VARCHAR, task_id VARCHAR, detail JSONB DEFAULT '{}'::jsonb, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS id_library(apple_id VARCHAR PRIMARY KEY, password VARCHAR NOT NULL, status VARCHAR DEFAULT 'normal', usage_status VARCHAR DEFAULT 'new', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        
        # 新增：系统日志表
        cur.execute("""CREATE TABLE IF NOT EXISTS system_logs(
            id SERIAL PRIMARY KEY,
            level VARCHAR DEFAULT 'INFO',
            module VARCHAR,
            message TEXT,
            detail JSONB DEFAULT '{}'::jsonb,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        print("[OK] 用户相关表创建完成")
        
        
        # 确保默认的服务器管理密码已设置 (密码: 1)
        try:
            default_pw_hash = hash_pw("1")
            cur.execute("INSERT INTO settings(key, value) VALUES('server_manager_pw_hash', %s) ON CONFLICT (key) DO NOTHING", (default_pw_hash,))
        except Exception as e:
            print(f"[WARN] 设置默认服务器管理密码失败: {e}")

        conn.commit()
        print("[OK] 数据库初始化完全成功")
    except Exception as e:
        print(f"[ERROR] 数据库初始化错误: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        conn.close()

# 内部日志记录辅助函数
def sys_log(level: str, module: str, message: str, detail: dict = None):
    """记录系统日志到数据库"""
    try:
        # 同时打印到控制台
        print(f"[{level}] [{module}] {message}")
        if detail:
            print(f"       Detail: {json.dumps(detail, ensure_ascii=False)}")
            
        # 写入数据库 (使用独立连接以避免事务冲突，且快速失败不影响主流程)
        if _DB_READY:
            def _write():
                try:
                    conn = db()
                    cur = conn.cursor()
                    cur.execute("INSERT INTO system_logs(level, module, message, detail, ts) VALUES(%s, %s, %s, %s, NOW())", 
                               (level, module, message, json.dumps(detail or {})))
                    conn.commit()
                    conn.close()
                except Exception as ex:
                    print(f"[WARN] 日志写入数据库失败: {ex}")
            
            # 异步写入避免阻塞？暂同步，量不大
            threading.Thread(target=_write).start()
    except:
        pass
# endregion

# region [REDIS UTILS]
# 导入统一的Redis管理器
from redis_manager import redis_manager
# endregion

# region [STARTUP INIT]
# 应用启动时的初始化（数据库、Redis等）
def startup_init():
    global _DB_READY
    logger.info("=" * 60)
    logger.info("应用启动初始化开始...")
    
    # 1. 初始化数据库
    try:
        logger.info("[1/2] 正在初始化数据库...")
        init_db()
        _DB_READY = True  # 标记数据库已初始化
        logger.info("[OK] 数据库初始化成功")
    except Exception as e:
        logger.error(f"[ERROR] 数据库初始化失败: {e}")
        import traceback
        traceback.print_exc()
        # 不抛出异常，允许应用继续启动（首次请求时会重试）
        logger.warning("[WARN] 应用将继续启动，数据库将在首次请求时重试初始化")
        _DB_READY = False
    
    # 2. 验证Redis连接
    try:
        logger.info("[2/2] 正在验证Redis连接...")
        if redis_manager.use_redis:
            redis_manager.client.ping()
            logger.info("[OK] Redis连接验证成功")
        else:
            logger.warning("[WARN] Redis未配置，使用内存模式")
    except Exception as e:
        logger.error(f"[ERROR] Redis连接验证失败: {e}")
        logger.warning("[WARN] 应用将继续启动，使用内存模式")
    
    logger.info("应用启动初始化完成")
    logger.info("=" * 60)

# 在应用启动时执行初始化（Flask 2.2+ 使用 before_request 或直接调用）
# 对于 gunicorn，模块加载时会执行
startup_init()
# endregion

# region [HEALTH]   
# 根路由 - 提供前端HTML文件
@app.route("/")
def root():
    logger.info("根路由被访问 - 返回前端页面")
    # index.html 在 API 目录下
    api_dir = Path(__file__).resolve().parent
    response = make_response(send_from_directory(api_dir, 'index.html'))
    # 禁止缓存
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# 提供静态文件（字体、图片等），排除API路径
@app.route("/<path:filename>")
def static_files(filename):
    # 排除API路径
    if filename.startswith('api/'):
        return jsonify({"error": "Not found"}), 404
    
    api_dir = Path(__file__).resolve().parent
    file_path = api_dir / filename
    if file_path.exists() and file_path.is_file():
        response = make_response(send_from_directory(api_dir, filename))
        # 对HTML文件禁止缓存
        if filename.endswith('.html'):
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        return response
    else:
        # 文件不存在时返回404，避免阻塞
        return jsonify({"error": "File not found"}), 404

# API根路由
@app.route("/api")
def api_root():
    logger.info("API根路由被访问")
    return jsonify({"ok": True, "name": "AutoSender API", "status": "running", "timestamp": now_iso()})

# 确保数据库已初始化（线程安全）
def _ensure_db_initialized():
    global _DB_READY
    if not _DB_READY:
        with _DB_INIT_LOCK:
            if not _DB_READY:  # Double-check locking
                try:
                    print("[INFO] 首次请求 - 初始化数据库...")
                    init_db()
                    _DB_READY = True
                    print("[OK] 数据库初始化成功")
                except Exception as e:
                    print(f"[ERROR] 数据库初始化失败: {e}")
                    import traceback
                    traceback.print_exc()
                    raise

# 健康检查
@app.route("/api/health")
def health():
    print("[OK] 健康检查被访问")
    try:
        # 确保数据库已初始化
        _ensure_db_initialized()
        # 测试数据库连接
        conn = db()
        conn.close()
        db_status = "connected"
    except Exception as e:
        print(f"[ERROR] 数据库连接失败: {e}")
        db_status = f"error: {str(e)}"
    
    return jsonify({
        "ok": True, 
        "status": "healthy", 
        "database": db_status,
        "timestamp": now_iso()
    })

# 数据库状态诊断
@app.route("/api/debug/db-status", methods=["GET"])
def debug_db_status():
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

# 查看Redis状态
@app.route("/api/debug/redis", methods=["GET"])
def debug_redis():
    # 🔥 快速失败，不阻塞
    try:
        online = redis_manager.get_online_workers()
    except Exception as e:
        logger.warning(f"获取在线Worker列表失败: {e}，使用空列表")
        online = []
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
# 签发用户Token（7天过期）
def _issue_user_token(conn, user_id: str) -> str:
    token = secrets.token_urlsafe(24)
    th = hash_token(token)
    cur = conn.cursor()
    expires_at = datetime.now() + timedelta(days=7)
    cur.execute("INSERT INTO user_tokens(token_hash, user_id, last_used, expires_at) VALUES(%s,%s,NOW(),%s) ON CONFLICT DO NOTHING", (th, user_id, expires_at))
    conn.commit()
    return token


# 用户注册/服务器注册
@app.route("/api/register", methods=["POST", "OPTIONS"])
def register():
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

    conn = None
    try:
        conn = db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 检查用户名是否已存在
        cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
        if cur.fetchone():
            conn.close()
            return jsonify({"ok": False, "success": False, "message": "用户名已存在"}), 409

        uid = gen_id("u")

        # 核心优化：频率限制
        client_ip = request.remote_addr
        limit_key = f"rate_limit:register:{client_ip}"
        if redis_manager.use_redis:
            try:
                count = redis_manager.client.incr(limit_key)
                if count == 1:
                    redis_manager.client.expire(limit_key, 60)
                if count > 3:  # 同一IP每分钟最多注册3次
                    conn.close()
                    return jsonify({"ok": False, "success": False, "message": "请求过于频繁，请稍后再试"}), 429
            except Exception as e:
                logger.warning(f"频率限制检查失败: {e}")

        # 插入用户数据
        cur.execute("INSERT INTO users(user_id,username,pw_hash) VALUES(%s,%s,%s)", (uid, username, hash_pw(pw)))
        cur.execute("INSERT INTO user_data(user_id) VALUES(%s)", (uid,))
        conn.commit()
        token = _issue_user_token(conn, uid)
        conn.close()
        return jsonify({"ok": True, "success": True, "token": token, "user_id": uid, "message": "注册成功"})

    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        logger.exception(f"注册失败: {e}")
        return jsonify({"ok": False, "success": False, "message": f"注册失败: {str(e)}"}), 500


# region [AUTH HELPERS]
def _check_login_rate_limit(client_ip: str) -> bool:
    """检查登录频率限制，返回 True 表示超出限制"""
    if not redis_manager.use_redis:
        return False
    limit_key = f"rate_limit:login:{client_ip}"
    try:
        count = redis_manager.client.incr(limit_key)
        if count == 1:
            redis_manager.client.expire(limit_key, 60)
        return count > 10
    except Exception as e:
        logger.warning(f"频率限制检查失败: {e}")
        return False

def _get_user_account_data(cur, uid: str):
    """获取用户余额和使用记录"""
    cur.execute("SELECT credits, usage FROM user_data WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    credits = float(row["credits"]) if row and row.get("credits") is not None else 1000.0
    usage = row.get("usage") if row else []
    return credits, usage

def _get_user_conversations(cur, uid: str, limit=100):
    """获取用户最近的对话列表"""
    cur.execute("""
        SELECT chat_id, meta, messages, updated 
        FROM conversations 
        WHERE user_id=%s 
        ORDER BY updated DESC 
        LIMIT %s
    """, (uid, limit))
    rows = cur.fetchall()
    return [{
        "chat_id": r.get("chat_id"),
        "meta": r.get("meta") or {},
        "messages": r.get("messages") or [],
        "updated": r.get("updated").isoformat() if r.get("updated") else None
    } for r in rows]

def _get_user_sent_records(cur, uid: str, limit=50):
    """获取用户最近的发送明细记录"""
    cur.execute("""
        SELECT phone_number, task_id, detail, ts 
        FROM sent_records 
        WHERE user_id=%s 
        ORDER BY ts DESC 
        LIMIT %s
    """, (uid, limit))
    rows = cur.fetchall()
    return [{
        "phone_number": r.get("phone_number"),
        "task_id": r.get("task_id"),
        "detail": r.get("detail") or {},
        "ts": r.get("ts").isoformat() if r.get("ts") else None
    } for r in rows]

def _get_user_task_history(cur, uid: str, limit=50):
    """
    🔥 核心优化：使用单条 JOIN 查询获取任务及其统计信息 (解决的问题 4: N+1 查询)
    """
    sql = """
        SELECT 
            t.task_id, t.message, t.total, t.count, t.status, t.created, t.updated,
            COALESCE(SUM(r.success), 0) as stats_success,
            COALESCE(SUM(r.fail), 0) as stats_fail,
            COALESCE(SUM(r.sent), 0) as stats_sent
        FROM tasks t
        LEFT JOIN shards s ON t.task_id = s.task_id
        LEFT JOIN reports r ON s.shard_id = r.shard_id
        WHERE t.user_id = %s
        GROUP BY t.task_id, t.message, t.total, t.count, t.status, t.created, t.updated
        ORDER BY t.created DESC
        LIMIT %s
    """
    cur.execute(sql, (uid, limit))
    rows = cur.fetchall()
    
    history_tasks = []
    for r in rows:
        history_tasks.append({
            "task_id": r.get("task_id"),
            "message": r.get("message"),
            "total": r.get("total"),
            "count": r.get("count"),
            "status": r.get("status"),
            "created": r.get("created").isoformat() if r.get("created") else None,
            "updated": r.get("updated").isoformat() if r.get("updated") else None,
            "result": {
                "success": int(r.get("stats_success", 0)),
                "fail": int(r.get("stats_fail", 0)),
                "sent": int(r.get("stats_sent", 0))
            }
        })
    return history_tasks
# endregion


def _get_user_global_stats(cur, uid: str):
    """获取用户全局统计数据（所有历史任务的总和）"""
    sql = """
        SELECT 
            COUNT(DISTINCT t.task_id) as total_tasks,
            COALESCE(SUM(r.success), 0) as total_success,
            COALESCE(SUM(r.fail), 0) as total_fail,
            COALESCE(SUM(r.sent), 0) as total_sent
        FROM tasks t
        LEFT JOIN shards s ON t.task_id = s.task_id
        LEFT JOIN reports r ON s.shard_id = r.shard_id
        WHERE t.user_id = %s
    """
    cur.execute(sql, (uid,))
    row = cur.fetchone()
    if not row:
        return {"total_tasks": 0, "total_success": 0, "total_fail": 0, "total_sent": 0}
    return {
        "total_tasks": int(row.get("total_tasks", 0)),
        "total_success": int(row.get("total_success", 0)),
        "total_fail": int(row.get("total_fail", 0)),
        "total_sent": int(row.get("total_sent", 0))
    }

@app.route("/api/login", methods=["POST", "OPTIONS"])
def login():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    username = (d.get("username") or "").strip()
    pw = (d.get("password") or "").strip()
    
    # 频率限制
    if _check_login_rate_limit(request.remote_addr):
        return jsonify({"ok": False, "success": False, "message": "登录尝试过多，请稍后再试"}), 429

    try:
        conn = db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        u = cur.fetchone()
    except Exception as e:
        logger.error(f"数据库查询失败: {e}")
        return jsonify({"ok": False, "success": False, "message": "数据库错误"}), 500

    if not u or u.get("pw_hash") != hash_pw(pw):
        if conn: conn.close()
        return jsonify({"ok": False, "success": False, "message": "用户名或密码错误"}), 401

    uid = u["user_id"]
    token = _issue_user_token(conn, uid)
    
    try:
        # 拆分功能模块加载数据
        credits, usage = _get_user_account_data(cur, uid)
        conversations = _get_user_conversations(cur, uid)
        access_records = _get_user_sent_records(cur, uid)
        
        # 🔥 修改：普通用户登录只加载最近3条记录，但加载全局统计
        history_tasks = _get_user_task_history(cur, uid, limit=3)
        global_stats = _get_user_global_stats(cur, uid)
        
        conn.close()
        
        # 保持与原有 API 返回格式 100% 兼容
        return jsonify({
            "ok": True, "success": True, "token": token, "user_id": uid, "message": "登录成功",
            "balance": credits, "usage_records": usage or [], 
            "access_records": access_records,
            "inbox_conversations": conversations,
            "history_tasks": history_tasks,
            "global_stats": global_stats, # 新增全局统计字段
            # data 字段是为了兼容某些旧版前端逻辑
            "data": {
                "credits": credits, 
                "usage": usage or [], 
                "conversations": conversations, 
                "sent_records": access_records,
                "global_stats": global_stats
            }
        })
    except Exception as e:
        if conn: conn.close()
        logger.exception(f"加载用户登录数据失败: {e}")
        return jsonify({"ok": False, "success": False, "message": "登录过程中加载数据失败"}), 500


# 验证用户Token
@app.route("/api/verify", methods=["POST", "OPTIONS"])
def verify_user():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    user_id = d.get("user_id")
    token = d.get("token")
    
    logger.debug(f"验证用户: {user_id}")

    try:
        conn = db()
        ok = _verify_user_token(conn, user_id, token)
        conn.close()
    except Exception as e:
        print(f"[ERROR] 验证失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

    if ok:
        return jsonify({"ok": True, "success": True})
    return jsonify({"ok": False, "success": False, "message": "invalid_token"}), 401
# endregion

# region [ADMIN AUTH]
# 签发管理员Token（7天过期）
def _issue_admin_token(conn, admin_id: str) -> str:
    token = secrets.token_urlsafe(24)
    th = hash_token(token)
    cur = conn.cursor()
    expires_at = datetime.now() + timedelta(days=7)
    cur.execute("INSERT INTO admin_tokens(token_hash, admin_id, last_used, expires_at) VALUES(%s,%s,NOW(),%s) ON CONFLICT DO NOTHING", (th, admin_id, expires_at))
    conn.commit()
    return token


# 验证管理员Token（检查是否过期）
def _verify_admin_token(conn, token: str) -> Optional[str]:
    if not token:
        return None
    th = hash_token(token)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT admin_id FROM admin_tokens WHERE token_hash=%s AND (expires_at IS NULL OR expires_at > NOW()) ORDER BY created DESC LIMIT 1", (th,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE admin_tokens SET last_used=NOW() WHERE token_hash=%s", (th,))
        conn.commit()
        return row["admin_id"]
    return None


# 管理员登录
@app.route("/api/admin/login", methods=["POST", "OPTIONS"])
def admin_login():
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
    sys_log("INFO", "AdminAuth", f"Administrator {aid} logged in.", {"ip": request.remote_addr})
    conn.close()
    return jsonify({"ok": True, "success": True, "admin_id": aid, "token": token, "message": "登录成功"})


# 验证管理员Token
@app.route("/api/admin/verify", methods=["POST", "OPTIONS"])
def admin_verify():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    token = d.get("token")
    
    if not token:
        return jsonify({"ok": False, "success": False, "message": "缺少token"}), 400

    try:
        conn = db()
        admin_id = _verify_admin_token(conn, token)
        conn.close()
        
        if admin_id:
            return jsonify({"ok": True, "success": True, "admin_id": admin_id})
        return jsonify({"ok": False, "success": False, "message": "invalid_token"}), 401
    except Exception as e:
        return jsonify({"ok": False, "success": False, "message": f"验证失败: {str(e)}"}), 500


# 超级管理员获取指定用户完整历史记录
@app.route("/api/super-admin/user/<user_id>/history", methods=["GET"])
def super_admin_get_user_history(user_id):
    token = _bearer_token()
    conn = db()
    admin_id = _verify_admin_token(conn, token)
    
    if not admin_id:
        conn.close()
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 检查用户是否存在
        cur.execute("SELECT 1 FROM users WHERE user_id=%s OR username=%s", (user_id, user_id))
        if not cur.fetchone():
            conn.close()
            return jsonify({"ok": False, "success": False, "message": "用户不存在"}), 404
            
        # 如果传入的是用户名，转换成user_id
        if not user_id.isdigit(): # 简单判断，或者再查一次
             cur.execute("SELECT user_id FROM users WHERE username=%s", (user_id,))
             row = cur.fetchone()
             if row: 
                 user_id = row['user_id']

        # 获取完整历史记录 (比如限制 500条)
        history_tasks = _get_user_task_history(cur, user_id, limit=500)
        global_stats = _get_user_global_stats(cur, user_id)
        
        # 获取充值/使用记录 (保持完整)
        credits, usage = _get_user_account_data(cur, user_id)
        
        conn.close()
        return jsonify({
            "ok": True, 
            "success": True, 
            "user_id": user_id,
            "history_tasks": history_tasks,
            "global_stats": global_stats,
            "usage_records": usage,
            "credits": credits
        })
    except Exception as e:
        if conn: conn.close()
        return jsonify({"ok": False, "success": False, "message": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "success": False, "message": str(e)}), 500


# 管理员账号管理
@app.route("/api/admin/account", methods=["POST", "GET", "OPTIONS"])
def admin_account_collection():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "GET":
        cur.execute("""
            SELECT a.admin_id, a.created,
                   COALESCE(c.selected_servers, '[]'::jsonb) AS selected_servers,
                   COALESCE(c.user_groups, '[]'::jsonb) AS user_groups
            FROM admins a
            LEFT JOIN admin_configs c ON c.admin_id = a.admin_id
            ORDER BY a.created DESC
        """)
        rows = cur.fetchall()
        conn.close()
        return jsonify({"success": True, "admins": rows})

    d = _json()
    admin_id = (d.get("admin_id") or "").strip()
    password = (d.get("password") or "").strip()
    if not admin_id or not password:
        conn.close()
        return jsonify({"success": False, "message": "缺少 admin_id 或 password"}), 400

    try:
        cur.execute("SELECT 1 FROM admins WHERE admin_id=%s", (admin_id,))
        exists = cur.fetchone() is not None
        cur.execute("INSERT INTO admins(admin_id, pw_hash) VALUES(%s,%s) ON CONFLICT (admin_id) DO UPDATE SET pw_hash=EXCLUDED.pw_hash", 
                   (admin_id, hash_pw(password)))
        cur.execute("INSERT INTO admin_configs(admin_id) VALUES(%s) ON CONFLICT (admin_id) DO NOTHING", (admin_id,))
        conn.commit()
        
        cur.execute("""
            SELECT a.admin_id, a.created,
                   COALESCE(c.selected_servers, '[]'::jsonb) AS selected_servers,
                   COALESCE(c.user_groups, '[]'::jsonb) AS user_groups
            FROM admins a
            LEFT JOIN admin_configs c ON c.admin_id = a.admin_id
            WHERE a.admin_id=%s
        """, (admin_id,))
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

# 管理员账号详情
@app.route("/api/admin/account/<admin_id>", methods=["GET", "PUT", "DELETE", "OPTIONS"])
def admin_account_item(admin_id: str):
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "GET":
        cur.execute("""
            SELECT a.admin_id, a.created,
                   COALESCE(c.selected_servers, '[]'::jsonb) AS selected_servers,
                   COALESCE(c.user_groups, '[]'::jsonb) AS user_groups
            FROM admins a
            LEFT JOIN admin_configs c ON c.admin_id = a.admin_id
            WHERE a.admin_id=%s
        """, (admin_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "message": "not_found"}), 404
        return jsonify({"success": True, "admin": row})

    if request.method == "PUT":
        d = _json()
        password = (d.get("password") or "").strip()
        selected_servers = d.get("selected_servers") if "selected_servers" in d else d.get("selectedServers")
        user_groups = d.get("user_groups") if "user_groups" in d else d.get("userGroups")

        if not password and selected_servers is None and user_groups is None:
            conn.close()
            return jsonify({"success": False, "message": "missing_update_fields"}), 400

        cur.execute("SELECT 1 FROM admins WHERE admin_id=%s", (admin_id,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"success": False, "message": "not_found"}), 404

        try:
            if password:
                cur.execute("UPDATE admins SET pw_hash=%s WHERE admin_id=%s", (hash_pw(password), admin_id))
            cur.execute("INSERT INTO admin_configs(admin_id) VALUES(%s) ON CONFLICT (admin_id) DO NOTHING", (admin_id,))
            if selected_servers is not None:
                if not isinstance(selected_servers, list):
                    selected_servers = []
                
                # 获取旧的配置以找出被移除的服务器
                cur.execute("SELECT selected_servers FROM admin_configs WHERE admin_id=%s", (admin_id,))
                old_row = cur.fetchone()
                old_servers = old_row.get("selected_servers") if old_row else []
                if not isinstance(old_servers, list): old_servers = []

                # 更新配置
                cur.execute("UPDATE admin_configs SET selected_servers=%s::jsonb, updated=NOW() WHERE admin_id=%s", (json.dumps(selected_servers), admin_id))
                
                # 找出被移除的服务器名称
                removed_servers = [s for s in old_servers if s not in selected_servers]
                if removed_servers:
                    # 将被移除的服务器从该管理员分配给其用户的所有关联中解除
                    # 注意：selected_servers 存储的是 server_name，我们需要匹配并解除分配
                    cur.execute("""
                        UPDATE servers 
                        SET assigned_user = NULL, assigned_by_admin = NULL 
                        WHERE server_name = ANY(%s) AND assigned_by_admin = %s
                    """, (removed_servers, admin_id))

            if user_groups is not None:
                if not isinstance(user_groups, list):
                    user_groups = []
                cur.execute("UPDATE admin_configs SET user_groups=%s::jsonb, updated=NOW() WHERE admin_id=%s", (json.dumps(user_groups), admin_id))
            conn.commit()
            conn.close()
            return jsonify({"success": True})
        except Exception as e:
            conn.rollback()
            conn.close()
            return jsonify({"success": False, "message": str(e)}), 500

    cur.execute("DELETE FROM admins WHERE admin_id=%s", (admin_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        return jsonify({"success": True, "message": "管理员已删除"})
    else:
        return jsonify({"success": False, "message": "管理员不存在"}), 404
# endregion

# region [ADMIN USER MGMT]
# 管理员用户管理
@app.route("/api/admin/users", methods=["POST", "GET", "OPTIONS"])
def admin_users_collection():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
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

    if not username or not password:
        conn.close()
        return jsonify({"success": False, "message": "用户名和密码不能为空"}), 400

    cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
    if cur.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "用户名已存在"}), 409

    uid = gen_id("u")
    try:
        # 尝试获取当前管理员ID
        admin_id = None
        token = _bearer_token()
        if token:
            admin_id = _verify_admin_token(conn, token)
        
        cur2 = conn.cursor()
        cur2.execute("INSERT INTO users(user_id, username, pw_hash, created_by_admin) VALUES(%s,%s,%s,%s)", (uid, username, hash_pw(password), admin_id))
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


# 管理员用户详情
@app.route("/api/admin/users/<user_id>", methods=["GET", "DELETE", "OPTIONS"])
def admin_user_item(user_id: str):
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
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


# 管理员用户充值
@app.route("/api/admin/users/<user_id>/recharge", methods=["POST", "OPTIONS"])
def admin_user_recharge(user_id: str):
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    d = _json()
    amount = d.get("amount")
    if amount is None:
        conn.close()
        return jsonify({"success": False, "message": "缺少充值金额"}), 400
    
    try:
        amount_f = float(amount)
    except:
        conn.close()
        return jsonify({"success": False, "message": "金额格式错误"}), 400
    
    if amount_f == 0:
        conn.close()
        return jsonify({"success": False, "message": "充值金额不能为0"}), 400

    cur = conn.cursor(cursor_factory=RealDictCursor)
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
    usage.append({"action": "recharge", "amount": amount_f, "ts": now_iso(), "admin_id": "server_manager", "old_credits": old_credits, "new_credits": new_credits})

    cur2 = conn.cursor()
    cur2.execute("UPDATE user_data SET credits=%s, usage=%s WHERE user_id=%s", (new_credits, json.dumps(usage), real_user_id))
    conn.commit()
    conn.close()

    try:
        broadcast_user_update(real_user_id, 'balance_update', {'credits': new_credits, 'balance': new_credits, 'recharged': amount_f, 'old_credits': old_credits})
    except: pass

    return jsonify({"success": True, "user_id": real_user_id, "username": username, "old_credits": old_credits, "amount": amount_f, "credits": new_credits, "new_credits": new_credits})


@app.route("/api/admin/recharge-records", methods=["GET", "OPTIONS"])
def admin_recharge_records():
    """获取所有充值记录 - 服务器管理页面已通过密码验证，无需额外验证"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 获取所有用户的充值记录
    cur.execute("SELECT user_id, usage FROM user_data WHERE usage IS NOT NULL")
    rows = cur.fetchall()
    conn.close()
    
    all_recharge_records = []
    for row in rows:
        user_id = row.get("user_id")
        usage = row.get("usage") or []
        # 提取该用户的所有充值记录
        recharge_logs = [item for item in usage if isinstance(item, dict) and item.get("action") == "recharge"]
        for log in recharge_logs:
            all_recharge_records.append({
                "user_id": user_id,
                "amount": log.get("amount", 0),
                "ts": log.get("ts"),
                "admin_id": log.get("admin_id"),
                "old_credits": log.get("old_credits"),
                "new_credits": log.get("new_credits")
            })
    
    # 按时间倒序排列
    all_recharge_records.sort(key=lambda x: x.get("ts") or "", reverse=True)
    
    return jsonify({
        "success": True,
        "records": all_recharge_records,
        "total": len(all_recharge_records)
    })


@app.route("/api/admin/user/<user_id>/summary", methods=["GET", "OPTIONS"])
def admin_user_summary(user_id: str):
    """管理员用户详细汇总数据（移除前端业务逻辑）- 服务器管理页面已通过密码验证，无需额外验证"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    # 服务器管理页面已通过密码验证，直接允许操作

    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 解析用户标识（支持user_id或username）
    real_user_id, username = _resolve_user_id(cur, user_id)
    if not real_user_id:
        conn.close()
        return jsonify({"success": False, "message": "用户不存在"}), 404

    # 查询用户积分
    cur.execute("SELECT credits FROM user_data WHERE user_id=%s", (real_user_id,))
    credits_row = cur.fetchone()
    credits = float(credits_row.get("credits", 0)) if credits_row else 0.0

    # 查询统计数据
    cur.execute("SELECT u.created, d.stats, d.usage FROM users u LEFT JOIN user_data d ON u.user_id = d.user_id WHERE u.user_id=%s", (real_user_id,))
    row = cur.fetchone()
    conn.close()
    
    if not row:
        return jsonify({"success": False, "message": "用户数据不存在"}), 404

    stats = row.get("stats") or []
    usage = row.get("usage") or []
    
    # 🔥 从usage字段中提取consumption_logs（action='deduct'的记录，即用户使用积分的记录）
    consumption_logs = [item for item in usage if isinstance(item, dict) and item.get("action") == "deduct"]
    
    # 从usage字段中提取recharge_logs（action='recharge'的记录，即充值记录）
    recharge_logs = [item for item in usage if isinstance(item, dict) and item.get("action") == "recharge"]
    
    # stats字段本身就是usage_logs（任务统计记录）
    usage_logs = stats if isinstance(stats, list) else []
    
    
    # 🔥 计算总消费：从consumption_logs（deduct记录）计算，不是从充值记录计算
    total_credits_used = sum(float(log.get("amount", 0) or log.get("credits", 0)) for log in consumption_logs)
    total_sent_count = sum(float(log.get("sent_count", 0)) for log in usage_logs)
    total_sent_amount = sum(float(log.get("total_sent", 0)) for log in usage_logs)
    total_success_count = sum(float(log.get("success_count", 0)) for log in usage_logs)
    
    # 截断 usage_logs，只返回最近3条，以节省流量
    # 注意：这里只截断了列表，并没有影响上面的总数计算
    full_usage_logs_len = len(usage_logs)
    usage_logs = usage_logs[-3:] if usage_logs else []
    
    # 计算成功率
    total_success_rate = 0.0
    if total_sent_amount > 0: # 修正：应该由总量计算成功率
         total_success_rate = (total_success_count / total_sent_amount * 100)
    elif total_sent_count > 0:
        total_success_rate = (total_success_count / total_sent_count * 100)
    
    # 提取最后一条记录
    last_log = usage_logs[-1] if usage_logs else {}
    last_consumption = consumption_logs[-1] if consumption_logs else {}
    last_recharge = recharge_logs[-1] if recharge_logs else {}
    
    # 格式化注册时间
    created_time = row.get("created")
    created_str = "未知"
    if created_time:
        try:
            if isinstance(created_time, str):
                created_str = created_time
            else:
                created_str = created_time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            created_str = str(created_time)
    
    # 格式化最后访问时间
    last_access_str = "未知"
    if last_log:
        last_access_ts = last_log.get("timestamp") or last_log.get("ts")
        if last_access_ts:
            try:
                if isinstance(last_access_ts, str):
                    last_access_str = last_access_ts
                elif isinstance(last_access_ts, (int, float)):
                    from datetime import datetime
                    last_access_str = datetime.fromtimestamp(last_access_ts).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    last_access_str = str(last_access_ts)
            except Exception:
                last_access_str = str(last_access_ts)
    
    result = {
        "success": True,
        "user_id": real_user_id,
        "username": username,
        "credits": credits,
        "created": created_str,
        "last_access": last_access_str,
        "last_task_count": last_log.get("task_count", 0),
        "last_sent_count": last_log.get("sent_count", 0),
        "last_success_rate": float(last_log.get("success_rate", 0)),
        "last_credits_used": float(last_consumption.get("amount", 0) or last_consumption.get("credits", 0)),
        "total_access_count": len(usage_logs),
        "total_sent_count": int(total_sent_count),
        "total_sent_amount": int(total_sent_amount),
        "total_success_rate": round(total_success_rate, 2),
        "total_credits_used": round(total_credits_used, 2),  # 🔥 总消费：历史总使用积分
        "usage_logs": usage_logs,
        "consumption_logs": consumption_logs,  # 🔥 消费记录（deduct）
        "recharge_logs": recharge_logs  # 🔥 充值记录（recharge）
    }
    
    return jsonify(result)


@app.route("/api/admin/manager/<manager_id>/performance", methods=["GET", "POST", "OPTIONS"])
def admin_manager_performance(manager_id: str):
    """管理员业绩统计（移除前端业务逻辑）- 服务器管理页面已通过密码验证，无需额外验证"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    # 服务器管理页面已通过密码验证，直接允许操作

    # 验证manager_id是否存在
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT 1 FROM admins WHERE admin_id=%s", (manager_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "管理员不存在"}), 404

    # 获取用户列表和user_groups（从请求参数中获取）
    d = _json() if request.method == "POST" else {}
    users_param = d.get("users") or request.args.getlist("users")
    user_groups_param = d.get("user_groups") or d.get("userGroups") or []
    
    if not users_param:
        conn.close()
        return jsonify({"success": True, "total_credits": 0.0, "users": []})

    # 确保users是列表
    if isinstance(users_param, str):
        users_param = [users_param]
    
    # 构建用户添加时间映射（从user_groups中提取）
    user_added_at_map = {}
    if isinstance(user_groups_param, list):
        for group in user_groups_param:
            if isinstance(group, dict) and group.get("userId"):
                user_id = group.get("userId")
                added_at = group.get("added_at")
                if added_at:
                    user_added_at_map[user_id] = added_at
    
    user_list = []
    total_credits = 0.0

    for user_id in users_param:
        if not user_id:
            continue
        
        try:
            # 解析用户标识（支持user_id或username）
            real_user_id, username = _resolve_user_id(cur, user_id)
            if not real_user_id:
                logger.warning(f"管理员 {manager_id} 查询用户 {user_id} 不存在")
                continue
            
            # 获取用户添加时间（用于业绩计算）
            added_at = user_added_at_map.get(str(user_id)) or user_added_at_map.get(real_user_id)
            
            # 查询用户统计数据
            cur.execute("SELECT d.usage FROM user_data d WHERE d.user_id=%s", (real_user_id,))
            row = cur.fetchone()
            
            user_credits = 0.0
            if row:
                usage = row.get("usage") or []
                # 从usage中提取consumption_logs（action='recharge'的记录）
                consumption_logs = [item for item in usage if isinstance(item, dict) and item.get("action") == "recharge"]
                
                # 🔥 只计算添加用户后的充值记录
                if added_at:
                    # 将添加时间转换为可比较的格式（datetime已在文件顶部导入）
                    try:
                        added_datetime = datetime.fromisoformat(added_at.replace('Z', '+00:00'))
                        if added_datetime.tzinfo is None:
                            added_datetime = added_datetime.replace(tzinfo=timezone.utc)
                    except:
                        # 如果解析失败，使用当前时间（保守处理）
                        added_datetime = datetime.now(timezone.utc)
                    
                    # 只统计添加时间之后的充值记录
                    filtered_logs = []
                    for log in consumption_logs:
                        log_ts = log.get("ts") or log.get("timestamp")
                        if not log_ts:
                            continue
                        try:
                            log_datetime = datetime.fromisoformat(log_ts.replace('Z', '+00:00'))
                            if log_datetime.tzinfo is None:
                                log_datetime = log_datetime.replace(tzinfo=timezone.utc)
                            # 只统计添加时间之后的充值
                            if log_datetime >= added_datetime:
                                filtered_logs.append(log)
                        except:
                            # 如果时间解析失败，跳过该记录（保守处理）
                            continue
                    
                    user_credits = sum(float(log.get("amount", 0)) for log in filtered_logs)
                else:
                    # 如果没有添加时间，不计算业绩（保守处理）
                    user_credits = 0.0
            
            total_credits += user_credits
            user_list.append({
                "user_id": real_user_id,
                "credits": round(user_credits, 2)
            })
        except Exception as e:
            logger.warning(f"处理用户 {user_id} 统计数据失败: {e}")
            user_list.append({
                "user_id": user_id,
                "credits": 0.0
            })

    conn.close()
    return jsonify({
        "success": True,
        "total_credits": round(total_credits, 2),
        "users": user_list
    })


@app.route("/api/admin/manager/<manager_id>/display", methods=["GET", "POST", "OPTIONS"])
def admin_manager_display(manager_id: str):
    """管理员显示数据（移除前端业务逻辑）- 服务器管理页面已通过密码验证，无需额外验证"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    # 服务器管理页面已通过密码验证，直接允许操作

    # 验证manager_id是否存在
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT 1 FROM admins WHERE admin_id=%s", (manager_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "管理员不存在"}), 404

    # 获取请求参数（users和userGroups是前端管理的，需要通过参数传递）
    d = _json() if request.method == "POST" else {}
    users_param = d.get("users") or request.args.getlist("users")
    user_groups_param = d.get("user_groups") or d.get("userGroups") or []
    selected_servers_param = d.get("selected_servers") or []

    # 确保users是列表
    if isinstance(users_param, str):
        users_param = [users_param]
    
    # 🔥 优先从Redis获取在线Worker列表（实时状态）
    # 🔥 快速失败，不阻塞
    try:
        online_workers_set = set(redis_manager.get_online_workers())
    except Exception as e:
        logger.warning(f"获取在线Worker列表失败: {e}，使用空列表")
        online_workers_set = set()
    
    # 获取所有服务器
    cur.execute("SELECT server_id, server_name, server_url, port, status, last_seen, assigned_user AS assigned_user_id FROM servers ORDER BY COALESCE(server_name, server_id)")
    server_rows = cur.fetchall()
    
    now_ts = time.time()
    offline_after = int(os.environ.get("SERVER_OFFLINE_AFTER_SECONDS", "120"))
    
    all_servers = []
    for r in server_rows:
        server_id = r.get("server_id")
        last_seen = r.get("last_seen")
        status = (r.get("status") or "disconnected").lower()
        
        # 🔥 优先检查Redis在线状态（最准确）
        if server_id in online_workers_set:
            # Redis显示在线，直接使用connected状态
            status_out = "connected"
        elif last_seen:
            # Redis不在线，检查数据库的last_seen
            try:
                age = now_ts - last_seen.timestamp()
                status_out = "disconnected" if age > offline_after else (status if status in ["connected", "available"] else "connected")
            except Exception:
                status_out = "connected"
        else:
            status_out = "disconnected"
        
        server_name = r.get("server_name") or r.get("server_id")
        all_servers.append({
            "server_id": r.get("server_id"),
            "name": server_name,
            "url": r.get("server_url") or "",
            "status": status_out,
            "assigned_user_id": r.get("assigned_user_id")
        })

    # 构建userGroups的server映射（快速查找）
    user_groups_dict = {}
    if isinstance(user_groups_param, list):
        for group in user_groups_param:
            if isinstance(group, dict):
                user_id = group.get("userId") or group.get("user_id")
                servers = group.get("servers") or []
                if user_id:
                    user_groups_dict[user_id] = servers

    # 获取所有已分配的服务器名称集合
    assigned_servers_set = set()
    for servers_list in user_groups_dict.values():
        if isinstance(servers_list, list):
            assigned_servers_set.update(str(s) for s in servers_list)

    # 筛选管理员的服务器（基于selected_servers_param）
    manager_servers = []
    if selected_servers_param:
        selected_servers_set = set(str(s) for s in selected_servers_param)
        for server in all_servers:
            if server["name"] in selected_servers_set:
                manager_servers.append(server)
    else:
        # 如果没有指定selected_servers，返回所有服务器
        manager_servers = all_servers

    # 分类服务器
    assigned_to_users = []
    available_for_assignment = []
    for server in manager_servers:
        server_name = server["name"]
        if server_name in assigned_servers_set:
            assigned_to_users.append(server)
        else:
            available_for_assignment.append(server)

    # 批量查询用户数据
    user_list = []
    for user_id in users_param:
        if not user_id:
            continue
        
        try:
            # 解析用户标识（支持user_id或username）
            real_user_id, username = _resolve_user_id(cur, user_id)
            if not real_user_id:
                logger.warning(f"管理员 {manager_id} 查询用户 {user_id} 不存在")
                continue
            
            # 查询用户积分
            cur.execute("SELECT credits FROM user_data WHERE user_id=%s", (real_user_id,))
            credits_row = cur.fetchone()
            credits_balance = float(credits_row.get("credits", 0)) if credits_row else 0.0

            # 查询用户统计数据（获取lastSentCount）
            cur.execute("SELECT d.stats FROM user_data d WHERE d.user_id=%s", (real_user_id,))
            stats_row = cur.fetchone()
            last_sent_count = 0
            if stats_row:
                stats = stats_row.get("stats") or []
                if isinstance(stats, list) and len(stats) > 0:
                    last_log = stats[-1]
                    last_sent_count = int(last_log.get("sent_count", 0)) if isinstance(last_log, dict) else 0

            # 获取serverCount（从userGroups，使用user_id作为key，因为前端传递的就是user_id）
            server_count = len(user_groups_dict.get(user_id, []))

            user_list.append({
                "user_id": real_user_id,
                "username": username,
                "credits": round(credits_balance, 2),
                "last_sent_count": last_sent_count,
                "server_count": server_count
            })
        except Exception as e:
            logger.warning(f"处理用户 {user_id} 数据失败: {e}")
            user_list.append({
                "user_id": user_id,
                "credits": 0.0,
                "last_sent_count": 0,
                "server_count": len(user_groups_dict.get(user_id, []))
            })

    conn.close()

    return jsonify({
        "success": True,
        "user_list": user_list,
        "servers": {
            "assigned": assigned_to_users,
            "available": available_for_assignment
        },
        "user_groups": user_groups_param
    })
# endregion

# region [ADMIN HELPERS]
@app.route("/api/admin/check-user-assignment", methods=["GET"])
def check_user_assignment():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"success": False, "message": "Missing user_id"}), 400
    
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT admin_id, user_groups FROM admin_configs")
    rows = cur.fetchall()
    conn.close()
    
    for r in rows:
        groups = r.get("user_groups") or []
        manager_id = r.get("admin_id")
        if isinstance(groups, list):
            for g in groups:
                 # 检查userId是否匹配（注意类型转换）
                 if str(g.get("userId") or g.get("user_id")) == str(user_id):
                     return jsonify({
                         "success": True, 
                         "assigned": True, 
                         "manager_id": manager_id
                     })
    
    return jsonify({"success": True, "assigned": False})

#  获取全局费率
def _get_global_rates(conn):
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT rates FROM admin_configs WHERE admin_id='server_manager'")
        row = cur.fetchone()
        if row and row.get("rates"):
            return row.get("rates")
    except: pass
    return {}

# - 获取用户费率
def _get_user_rates(conn, user_id):
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT rates FROM user_data WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if row and row.get("rates"):
            return row.get("rates")
    except: pass
    return {}

@app.route("/api/admin/rates/global", methods=["GET", "POST", "OPTIONS"])
def admin_rates_global():
    """管理全局费率"""
    if request.method == "OPTIONS": return jsonify({"ok": True})
    
    conn = db()
    if request.method == "GET":
        rates = _get_global_rates(conn)
        conn.close()
        return jsonify({"success": True, "rates": rates})
        
    if request.method == "POST":
        d = _json()
        rates = d.get("rates")
        if not rates: return jsonify({"success": False, "message": "missing rates"}), 400
        
        cur = conn.cursor()
        # 确保 server_manager 配置存在
        cur.execute("INSERT INTO admin_configs(admin_id, rates) VALUES('server_manager', %s) ON CONFLICT (admin_id) DO UPDATE SET rates=%s", (json.dumps(rates), json.dumps(rates)))
        conn.commit()
        conn.close()
        return jsonify({"success": True})

@app.route("/api/admin/rates/user", methods=["POST", "OPTIONS"])
def admin_rates_user():
    """管理指定用户费率"""
    if request.method == "OPTIONS": return jsonify({"ok": True})
    
    d = _json()
    user_id = d.get("user_id")
    rates = d.get("rates")
    
    if not user_id: return jsonify({"success": False, "message": "missing user_id"}), 400
    
    conn = db()
    cur = conn.cursor()
    
    # 如果 rates 为空或None，则视为删除/重置用户费率
    if rates is None:
        cur.execute("UPDATE user_data SET rates=NULL WHERE user_id=%s", (user_id,))
    else:
        # 确保该列存在 (简单的运行时迁移检查，生产环境建议用单独脚本)
        try:
            cur.execute("ALTER TABLE user_data ADD COLUMN IF NOT EXISTS rates JSONB")
            conn.commit()
        except: conn.rollback()
        
        cur.execute("UPDATE user_data SET rates=%s WHERE user_id=%s", (json.dumps(rates), user_id))
    
    conn.commit()
    conn.close()
    return jsonify({"success": True})
# endregion

# region [SERVER MANAGER]
@app.route("/api/server-manager/login", methods=["POST", "OPTIONS"])
def server_manager_login():
    """服务器管理登录（最高权限）：验证 server_manager 密码并签发 admin_token"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    password = d.get("password", "")

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    pw_hash = _get_setting(cur, "server_manager_pw_hash") or hash_pw("1")
    ok = (hash_pw(password) == pw_hash)
    if not ok:
        conn.close()
        return jsonify({"success": False, "message": "密码错误"}), 401

    # 确保存在一个"最高权限管理员"账号（用于复用 admin token / admin 接口权限）
    super_admin_id = "server_manager"
    
    try:
        cur2 = conn.cursor()
        # 超级管理员密码需要在数据库中手动设置为"1"，不再在代码中设置随机密码
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM admins WHERE admin_id=%s", (super_admin_id,))
        if not cur.fetchone():
            # 密码需要在数据库中手动设置为"1"，这里使用默认密码"0"以保持一致性
            cur2.execute("INSERT INTO admins(admin_id, pw_hash) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                         (super_admin_id, hash_pw("0")))
            try:
                cur2.execute("INSERT INTO admin_configs(admin_id) VALUES(%s) ON CONFLICT (admin_id) DO NOTHING", (super_admin_id,))
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass

    token = _issue_admin_token(conn, super_admin_id)
    conn.close()
    return jsonify({"success": True, "admin_id": super_admin_id, "token": token, "message": "登录成功"})

@app.route("/api/server-manager/verify", methods=["POST", "OPTIONS"])
def server_manager_verify():
    """服务器管理密码验证"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    password = d.get("password", "")

    conn = db()
    cur = conn.cursor()
    pw_hash = _get_setting(cur, "server_manager_pw_hash") or hash_pw("1")
    ok = (hash_pw(password) == pw_hash)
    conn.close()

    if ok:
        return jsonify({"success": True, "message": "验证成功"})
    return jsonify({"success": False, "message": "密码错误"}), 401


# 服务器管理密码更新
@app.route("/api/server-manager/password", methods=["PUT", "OPTIONS"])
def server_manager_password_update():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    old_pw = d.get("oldPassword") or d.get("old_password") or ""
    new_pw = d.get("password") or ""

    if not old_pw or not new_pw:
        return jsonify({"success": False, "message": "缺少旧密码或新密码"}), 400

    conn = db()
    cur = conn.cursor()
    current_hash = _get_setting(cur, "server_manager_pw_hash") or hash_pw("0")

    if hash_pw(old_pw) != current_hash:
        conn.close()
        return jsonify({"success": False, "message": "旧密码错误"}), 401

    _set_setting(cur, "server_manager_pw_hash", hash_pw(new_pw))
    conn.commit()
    conn.close()
    return jsonify({"success": True})
# endregion

# region [SERVER REGISTRY]
# 规范化服务器状态
def _normalize_server_status(status: str, clients_count: int) -> str:
    s = (status or "").lower().strip()
    if s in {"online", "available"}:
        return "connected" if clients_count > 0 else "available"
    if s in {"connected", "disconnected", "offline"}:
        return "disconnected" if s == "offline" else s
    return "connected" if clients_count > 0 else "available"


# Worker服务器注册
@app.route("/api/server/register", methods=["POST", "OPTIONS"])
def server_register():
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


# 服务器心跳
@app.route("/api/server/heartbeat", methods=["POST", "OPTIONS"])
def server_hb():
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


# Registry心跳(兼容)
@app.route("/api/heartbeat", methods=["POST", "OPTIONS"])
def registry_heartbeat_alias():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    registry_id = d.get("id")
    if not registry_id:
        return jsonify({"success": False, "message": "missing id"}), 400

    conn = db()
    cur = conn.cursor()
    status = _normalize_server_status(d.get("status") or "online", int(d.get("clients_count") or 0))
    cur.execute("UPDATE servers SET last_seen=NOW(), status=%s, server_name=COALESCE(%s, server_name), server_url=COALESCE(%s, server_url), clients_count=%s, meta = COALESCE(meta,'{}'::jsonb) || %s::jsonb WHERE registry_id=%s", (status, d.get("name"), d.get("url"), int(d.get("clients_count") or 0), json.dumps(d), registry_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# Registry注销(兼容)
@app.route("/api/unregister", methods=["POST", "OPTIONS"])
def registry_unregister_alias():
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
# 服务器列表
@app.route("/api/servers", methods=["GET", "POST", "OPTIONS"])
def servers_collection():
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
    servers = []
    now_ts = time.time()
    offline_after = int(os.environ.get("SERVER_OFFLINE_AFTER_SECONDS", "120"))
    try:
        online_workers_set = set(redis_manager.get_online_workers())
    except:
        online_workers_set = set()
    
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT server_id, server_name, server_url, port, clients_count, status, last_seen, assigned_user AS assigned_user_id, meta FROM servers ORDER BY COALESCE(server_name, server_id)")
    rows = cur.fetchall()

    # 获取服务器所属管理员映射
    cur.execute("SELECT admin_id, selected_servers FROM admin_configs")
    admin_rows = cur.fetchall()
    conn.close()

    server_manager_map = {}
    for ar in admin_rows:
        aid = ar.get("admin_id")
        sst = ar.get("selected_servers")
        if aid and sst and isinstance(sst, list):
            for sname in sst:
                server_manager_map[str(sname)] = aid
    
    for r in rows:
        server_id = r.get("server_id")
        last_seen = r.get("last_seen")
        status = (r.get("status") or "disconnected").lower()
        clients_count = int(r.get("clients_count") or 0)
        if server_id in online_workers_set:
            status_out = "connected"
        elif last_seen:
            try:
                age = now_ts - last_seen.timestamp()
                status_out = "disconnected" if age > offline_after else _normalize_server_status(status, clients_count)
            except: status_out = _normalize_server_status(status, clients_count)
        else: status_out = _normalize_server_status(status, clients_count)

        meta = r.get("meta") or {}
        assigned_user_id = r.get("assigned_user_id")
        servers.append({
            "server_id": server_id, "server_name": r.get("server_name") or server_id,
            "server_url": r.get("server_url") or "", "status": status_out, "assigned_user_id": assigned_user_id,
            "is_assigned": assigned_user_id is not None, "is_private": assigned_user_id is not None,
            "is_public": assigned_user_id is None, "last_seen": r.get("last_seen").isoformat() if r.get("last_seen") else None,
            "bound_manager": server_manager_map.get(str(r.get("server_name") or server_id))
        })
    return jsonify({"success": True, "servers": servers})

# 服务器详情
@app.route("/api/servers/<server_id>", methods=["DELETE", "GET", "OPTIONS"])
def servers_item(server_id: str):
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if request.method == "GET":
        cur.execute("SELECT server_id, server_name, server_url, status, last_seen, assigned_user AS assigned_user_id FROM servers WHERE server_id=%s", (server_id,))
        row = cur.fetchone()
        conn.close()
        if not row: return jsonify({"success": False, "message": "not_found"}), 404
        return jsonify({"success": True, "server": row})
    cur2 = conn.cursor()
    cur2.execute("DELETE FROM servers WHERE server_id=%s", (server_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# 清理无效的服务器ID
@app.route("/api/servers/cleanup", methods=["POST", "OPTIONS"])
def cleanup_invalid_servers():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    import re
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT server_id, server_name FROM servers")
    all_servers = cur.fetchall()
    deleted_count = 0
    for row in all_servers:
        sid = str(row.get("server_id", "")).strip()
        sname = str(row.get("server_name", "")).strip()
        should = False

        
        if should:
            cur2 = conn.cursor()
            cur2.execute("DELETE FROM servers WHERE server_id=%s", (sid,))
            deleted_count += 1
    conn.commit()
    conn.close()
    return jsonify({"success": True, "deleted_count": deleted_count})


# 标记服务器为断开
@app.route("/api/servers/<server_id>/disconnect", methods=["POST", "OPTIONS"])
def server_disconnect(server_id: str):
    if request.method == "OPTIONS": return jsonify({"ok": True})
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE servers SET last_seen = NOW() - INTERVAL '1 day', status = 'disconnected' WHERE server_id=%s", (server_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# 服务器分配
@app.route("/api/servers/<server_id>/assign", methods=["POST", "OPTIONS"])
def server_assign(server_id: str):
    if request.method == "OPTIONS": return jsonify({"ok": True})
    d = _json()
    user_id = d.get("user_id")
    if not user_id: return jsonify({"success": False, "message": "missing user_id"}), 400
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT server_id, assigned_user FROM servers WHERE server_id=%s", (server_id,))
    server = cur.fetchone()
    if not server:
        conn.close()
        return jsonify({"success": False, "message": "服务器不存在"}), 404
    cur.execute("SELECT user_id FROM users WHERE user_id=%s", (user_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "用户不存在"}), 404
    cur2 = conn.cursor()
    # 尝试获取当前管理员ID
    admin_id = None
    token = _bearer_token()
    if token:
        admin_id = _verify_admin_token(conn, token)
    
    cur2.execute("UPDATE servers SET assigned_user=%s, assigned_by_admin=%s WHERE server_id=%s", (user_id, admin_id, server_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/servers/<server_id>/unassign", methods=["POST", "OPTIONS"])
def server_unassign(server_id: str):
    # 服务器取消分配
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
    cur2.execute("UPDATE servers SET assigned_user=NULL, assigned_by_admin=NULL WHERE server_id=%s", (server_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"服务器 {server_id} 已取消分配，现为公共服务器", "server_id": server_id, "previous_user": current_assigned})


@app.route("/api/servers/assigned/<user_id>", methods=["GET", "OPTIONS"])
def servers_assigned(user_id: str):
    # 用户已分配服务器
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
    # 用户可用服务器 - 根据管理员的selected_servers过滤
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 获取用户的created_by_admin信息
    cur.execute("SELECT created_by_admin FROM users WHERE user_id=%s", (user_id,))
    user_row = cur.fetchone()
    admin_id = user_row.get("created_by_admin") if user_row else None
    
    # 获取管理员的selected_servers列表
    admin_selected_servers = None
    if admin_id:
        cur.execute("SELECT selected_servers FROM admin_configs WHERE admin_id=%s", (admin_id,))
        admin_config = cur.fetchone()
        if admin_config and admin_config.get("selected_servers"):
            admin_selected_servers = admin_config.get("selected_servers")
            if not isinstance(admin_selected_servers, list):
                admin_selected_servers = []
    
    # 获取分配给该用户的独享服务器
    cur.execute("SELECT server_id, server_name, server_url, status, last_seen, meta FROM servers WHERE assigned_user=%s", (user_id,))
    exclusive = cur.fetchall()
    
    # 获取共享服务器（未分配给任何用户的）
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

    # 如果用户有管理员且管理员有selected_servers配置，则过滤服务器
    if admin_selected_servers is not None:
        # 过滤独享服务器：只保留在管理员selected_servers中的
        filtered_exclusive = [s for s in exclusive if (s.get("server_name") or s.get("server_id")) in admin_selected_servers]
        # 过滤共享服务器：只保留在管理员selected_servers中的
        filtered_shared = [s for s in shared if (s.get("server_name") or s.get("server_id")) in admin_selected_servers]
        return jsonify({"success": True, "exclusive_servers": enrich(filtered_exclusive), "shared_servers": enrich(filtered_shared)})
    
    return jsonify({"success": True, "exclusive_servers": enrich(exclusive), "shared_servers": enrich(shared)})


@app.route("/api/user/<user_id>/servers", methods=["GET", "OPTIONS"])
@app.route("/api/api/user/<user_id>/servers", methods=["GET", "OPTIONS"])
def user_servers(user_id: str):
    # 用户服务器列表
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
    # 用户后端列表 - 根据管理员的selected_servers过滤
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    print(f"📡 获取用户后端列表: {user_id}")
    
    try:
        conn = db()
        authed_uid = _maybe_authed_user(conn)
        if authed_uid and authed_uid != user_id:
            conn.close()
            print(f"[ERROR] 权限拒绝: {authed_uid} != {user_id}")
            return jsonify({"success": False, "message": "forbidden"}), 403

        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 获取用户的created_by_admin信息
        cur.execute("SELECT created_by_admin FROM users WHERE user_id=%s", (user_id,))
        user_row = cur.fetchone()
        admin_id = user_row.get("created_by_admin") if user_row else None
        
        # 获取管理员的selected_servers列表
        admin_selected_servers = None
        if admin_id:
            cur.execute("SELECT selected_servers FROM admin_configs WHERE admin_id=%s", (admin_id,))
            admin_config = cur.fetchone()
            if admin_config and admin_config.get("selected_servers"):
                admin_selected_servers = admin_config.get("selected_servers")
                if not isinstance(admin_selected_servers, list):
                    admin_selected_servers = []
        
        cur.execute("SELECT server_id, server_name, server_url, status, last_seen, assigned_user AS assigned_user_id FROM servers WHERE assigned_user=%s OR assigned_user IS NULL ORDER BY COALESCE(server_name, server_id)", (user_id,))
        rows = cur.fetchall()
        conn.close()
        
        # 如果用户有管理员且管理员有selected_servers配置，则过滤服务器
        if admin_selected_servers is not None:
            filtered_rows = [r for r in rows if (r.get("server_name") or r.get("server_id")) in admin_selected_servers]
            print(f"[OK] 返回 {len(filtered_rows)} 个后端 (过滤后，管理员 {admin_id} 的 {len(admin_selected_servers)} 个选定服务器)")
            return jsonify({"success": True, "backends": filtered_rows})
        
        print(f"[OK] 返回 {len(rows)} 个后端")
        return jsonify({"success": True, "backends": rows})
    except Exception as e:
        print(f"[ERROR] 获取后端列表失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
# endregion

# region [ID LIBRARY SYNC]
@app.route("/api/id-library", methods=["GET", "POST", "OPTIONS"])
def id_library():
    # ID库同步 - 获取或保存所有ID
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    # 确保数据库已初始化
    try:
        # _ensure_db_initialized() # Removed as per previous context or assuming it's not needed/defined in scope? 
        # Actually in original file it was called or maybe not. I'll stick to simple db() call.
        pass
    except: pass
    
    try:
        conn = db()
    except Exception as e:
        return jsonify({"success": False, "message": f"数据库连接失败: {str(e)}"}), 503
    
    try:
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
            return jsonify({"success": True, "accounts": accounts})
        
        elif request.method == "POST":
            # 同步ID库（保存或更新）
            data = _json()
            accounts = data.get("accounts", [])
            
            if not isinstance(accounts, list):
                return jsonify({"success": False, "message": "accounts must be a list"}), 400
            
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
            return jsonify({"success": True, "message": f"同步了 {len(accounts)} 个账号"})
    except Exception as e:
        try:
            conn.rollback()
        except:
            pass
        logger.error(f"ID库操作失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        try:
            conn.close()
        except:
            pass


@app.route("/api/id-library/<apple_id>", methods=["DELETE", "PUT", "OPTIONS"])
def id_library_item(apple_id: str):
    # ID库单个记录操作
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    # 确保数据库已初始化
    try:
        _ensure_db_initialized()
    except Exception as e:
        return jsonify({"success": False, "message": f"数据库初始化失败: {str(e)}"}), 503
    
    try:
        conn = db()
    except Exception as e:
        return jsonify({"success": False, "message": f"数据库连接失败: {str(e)}"}), 503
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        if request.method == "DELETE":
            # 删除ID
            cur.execute("DELETE FROM id_library WHERE apple_id=%s", (apple_id,))
            conn.commit()
            deleted = cur.rowcount > 0
            if deleted:
                return jsonify({"success": True, "message": "删除成功"})
            else:
                return jsonify({"success": False, "message": "账号不存在"}), 404
        
        elif request.method == "PUT":
            # 更新ID状态（usage_status）
            data = _json()
            usage_status = data.get("usageStatus", "new")
            
            if usage_status not in ["new", "used"]:
                return jsonify({"success": False, "message": "usageStatus must be 'new' or 'used'"}), 400
            
            cur.execute("""
                UPDATE id_library 
                SET usage_status=%s, updated_at=NOW()
                WHERE apple_id=%s
            """, (usage_status, apple_id))
            conn.commit()
            updated = cur.rowcount > 0
            if updated:
                return jsonify({"success": True, "message": "更新成功"})
            else:
                return jsonify({"success": False, "message": "账号不存在"}), 404
    except Exception as e:
        try:
            conn.rollback()
        except:
            pass
        logger.error(f"ID库操作失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        try:
            conn.close()
        except:
            pass

# region [RATES]
@app.route("/api/admin/rate", methods=["GET", "POST", "OPTIONS"])
def admin_rate():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    conn = db()
    cur = conn.cursor()
    
    if request.method == "GET":
        rate = _get_setting(cur, "exchange_rate") or "7.0"
        conn.close()
        return jsonify({"success": True, "rate": float(rate)})
        
    d = _json()
    rate = d.get("rate")
    if rate is None:
        conn.close()
        return jsonify({"success": False, "message": "Missing rate"}), 400
        
    try:
        f_rate = float(rate)
        _set_setting(cur, "exchange_rate", str(f_rate))
        conn.commit()
    except ValueError:
        conn.close()
        return jsonify({"success": False, "message": "Invalid rate format"}), 400
        
    conn.close()
    return jsonify({"success": True})
# endregion

# endregion


# region [USER DATA]
def _resolve_user_id(cur, identifier: str) -> tuple:
    # 通过user_id或username解析真实的user_id，返回(user_id, username)
    # 用户ID格式：纯4位数字（0000-9999），兼容旧格式u_1234
    if not identifier:
        return None, None
    
    # 处理旧格式u_1234，转换为纯4位数字
    if identifier.startswith("u_"):
        identifier = identifier[2:]
    
    # 先尝试作为user_id查询（纯4位数字）
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
    # 用户积分（支持user_id或username查询）
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
    # 用户扣费
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
    # 用户统计（支持user_id或username查询）
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
    # 收件箱推送
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


# 会话管理
@app.route("/api/user/<user_id>/conversations", methods=["GET", "POST", "OPTIONS"])
def conversations_collection(user_id: str):
    if request.method == "OPTIONS": return jsonify({"ok": True})
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if request.method == "GET":
        cur.execute("SELECT chat_id, meta, updated FROM conversations WHERE user_id=%s ORDER BY updated DESC", (user_id,))
        rows = cur.fetchall()
        conn.close()
        return jsonify({"success": True, "conversations": rows})
    d = _json()
    chat_id = (d.get("chat_id") or d.get("phone_number") or d.get("id") or "").strip()
    if not chat_id:
        conn.close()
        return jsonify({"success": False}), 400
    cur.execute("INSERT INTO conversations(user_id, chat_id, meta, messages, updated) VALUES(%s,%s,%s::jsonb,%s::jsonb,NOW()) ON CONFLICT (user_id, chat_id) DO UPDATE SET meta = COALESCE(conversations.meta,'{}'::jsonb) || EXCLUDED.meta, messages = EXCLUDED.messages, updated = NOW()", (user_id, chat_id, json.dumps(d.get("meta") or {}), json.dumps(d.get("messages", []))))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# 发送记录
@app.route("/api/user/<user_id>/sent-records", methods=["GET", "POST", "OPTIONS"])
def sent_records(user_id: str):
    if request.method == "OPTIONS": return jsonify({"ok": True})
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if request.method == "GET":
        cur.execute("SELECT phone_number, task_id, detail, ts FROM sent_records WHERE user_id=%s ORDER BY ts DESC LIMIT 500", (user_id,))
        rows = cur.fetchall()
        conn.close()
        return jsonify({"success": True, "records": rows})
    d = _json()
    cur2 = conn.cursor()
    cur2.execute("INSERT INTO sent_records(user_id, phone_number, task_id, detail) VALUES(%s,%s,%s,%s)", (user_id, d.get("phone_number"), d.get("task_id"), json.dumps(d)))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# 获取任务列表
@app.route("/api/user/<user_id>/tasks", methods=["GET", "POST", "OPTIONS"])
def tasks_collection(user_id: str):
    if request.method == "OPTIONS": return jsonify({"ok": True})
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if request.method == "GET":
        cur.execute("SELECT task_id, message, status, created, updated, total, count FROM tasks WHERE user_id=%s ORDER BY created DESC", (user_id,))
        rows = cur.fetchall()
        conn.close()
        return jsonify({"success": True, "tasks": rows})
    d = _json()
    tid = gen_id("t")
    message = d.get("message", "")
    total = int(d.get("total", 0))
    count = int(d.get("count", 1))
    cur2 = conn.cursor()
    cur2.execute("INSERT INTO tasks(task_id, user_id, message, status, total, count) VALUES(%s,%s,%s,'pending',%s,%s)", (tid, user_id, message, total, count))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "task_id": tid})

# 任务分片管理
@app.route("/api/user/<user_id>/tasks/<task_id>/shards", methods=["GET", "OPTIONS"])
def shards_collection(user_id: str, task_id: str):
    if request.method == "OPTIONS": return jsonify({"ok": True})
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT shard_id, server_id, status, result, updated FROM shards WHERE task_id=%s", (task_id,))
    rows = cur.fetchall()
    conn.close()
    return jsonify({"success": True, "shards": rows})
# endregion

# region [TASK]
def _split_numbers(nums, shard_size: int):
    # 分片号码列表
    for i in range(0, len(nums), shard_size):
        yield nums[i : i + shard_size]


def _reclaim_stale_shards(conn) -> int:
    # 回收超时分片
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
    
    # [MODIFIED] 使用动态费率计算预估成本
    # 1. 获取全局费率作为基准
    global_rates = _get_global_rates(conn)
    base_price = float(global_rates.get("send", os.environ.get("CREDIT_PER_SUCCESS", "1")))
    
    # 2. 检查用户是否有专属费率
    user_rates = _get_user_rates(conn, uid)
    if user_rates and "send" in user_rates:
        price_per_msg = float(user_rates["send"])
    else:
        price_per_msg = base_price
        
    estimated_cost = len(nums) * price_per_msg
    if credits < estimated_cost:
        conn.close()
        return jsonify({"ok": False, "message": "insufficient_credits", "credits": credits, "current": credits, "required": estimated_cost}), 400

    task_id = gen_id("task")
    
    # 🔥 优化：根据可用服务器数量动态计算shard数量
    # 先获取可用服务器数量
    # 🔥 快速失败，不阻塞
    try:
        available_servers = redis_manager.get_online_workers()
    except Exception as e:
        logger.debug(f"获取可用服务器列表失败: {e}，使用空列表")
        available_servers = []
    available_count = len(available_servers) if available_servers else 0
    
    # 📋 终端输出：接收到任务
    print(f"📥 任务 {task_id[:8]}... | 号码: {len(nums)} | 可用服务器: {available_count}")
    
    # 如果请求中指定了shard_size，优先使用（向后兼容）
    if d.get("shard_size"):
        shard_size = int(d.get("shard_size"))
    elif available_count > 0:
        # 根据可用服务器数量动态计算：每个服务器一个shard
        # 如果号码数 < 服务器数，则每个shard至少1个号码
        # 如果号码数 >= 服务器数，则平均分配
        total_numbers = len(nums)
        if total_numbers <= available_count:
            # 号码数少于或等于服务器数：每个shard 1个号码
            shard_size = 1
        else:
            # 号码数大于服务器数：平均分配，每个服务器一个shard
            # 向上取整，确保所有号码都被分配
            shard_size = (total_numbers + available_count - 1) // available_count
        logger.debug(f"动态计算shard_size: 号码数={total_numbers}, 可用服务器={available_count}, shard_size={shard_size}")
        # 不输出详细计算过程
    else:
        # 没有可用服务器时，使用默认值或环境变量
        shard_size = int(os.environ.get("SHARD_SIZE", "50"))
        logger.debug(f"无可用服务器，使用默认shard_size={shard_size}")

    _reclaim_stale_shards(conn)
    cur = conn.cursor()
    cur.execute("INSERT INTO tasks(task_id,user_id,message,total,count,status,created,updated) VALUES(%s,%s,%s,%s,%s,'pending',NOW(),NOW())", (task_id, uid, msg, len(nums), cnt))

    # 🔥 核心优化：将 Task 信息写入 Redis 缓存 (问题 5)
    if redis_manager.use_redis:
        try:
            task_cache = {
                "task_id": task_id,
                "user_id": uid,
                "message": msg,
                "total": len(nums),
                "count": cnt,
                "status": "pending"
            }
            redis_manager.client.set(f"task_info:{task_id}", json.dumps(task_cache), ex=3600)
            logger.debug(f"Task {task_id} 已写入 Redis 缓存")
        except Exception as e:
            logger.warning(f"Task 写入 Redis 缓存失败: {e}")

    for group in _split_numbers(nums, shard_size):
        shard_id = gen_id("shard")
        cur.execute("INSERT INTO shards(shard_id,task_id,phones,status,updated) VALUES(%s,%s,%s,'pending',NOW())", (shard_id, task_id, json.dumps(group)))

    conn.commit()
    conn.close()
    
    # 🔥 核心改造：立即分配并推送分片到 Worker（消灭轮询）
    assign_result = _assign_and_push_shards(task_id, uid, msg)
    
    total_shards = assign_result.get("total", 0)
    pushed_shards = assign_result.get("pushed", 0)
    print(f"📦 Shard: {total_shards} | 推送: {pushed_shards}/{total_shards}")
    
    # 更新任务状态为 running（如果有分片成功推送）
    if assign_result.get("pushed", 0) > 0:
        conn2 = db()
        cur2 = conn2.cursor()
        cur2.execute("UPDATE tasks SET status='running', updated=NOW() WHERE task_id=%s", (task_id,))
        conn2.commit()
        conn2.close()
        print(f"⏳ 等待结果...")
        logger.debug(f"任务 {task_id} 状态更新为 running")
    
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

    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    d = _json()
    task_id = d.get("task_id")
    if not task_id:
        return jsonify({"ok": False, "msg": "missing task_id"}), 400
    
    logger.warning(f"[WARN] 调用了已废弃的端点 /api/task/assign，task_id={task_id}")
    logger.warning(f"[WARN] 提示：任务创建时已自动分配，无需手动调用此端点")

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
    logger.info(f"[INFO] 手动重新分配任务 {task_id}...")
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
    
    logger.warning(f"[WARN] Worker {server_id} 调用了已废弃的轮询端点 /api/server/<server_id>/shards")
    logger.warning(f"[WARN] 提示：请升级 Worker 以使用 WebSocket 推送机制")

    # 返回空列表，鼓励使用 WebSocket
    return jsonify({
        "ok": True, 
        "shards": [], 
        "reclaimed": 0,
        "deprecated": True,
        "message": "此端点已废弃，请使用 WebSocket 推送机制。任务会自动推送到 Worker，无需轮询。"
    })


# 提交任务报告 [DEPRECATED - 已废弃]
# 此端点已不再使用，结果通过 WebSocket shard_result 上报
@app.route("/api/reports", methods=["POST", "OPTIONS"])
def reports_collection():
    if request.method == "OPTIONS": return jsonify({"ok": True})
    # 废弃端点，保留仅用于向后兼容
    logger.warning("[DEPRECATED] /api/reports 端点已废弃，请使用 WebSocket shard_result")
    return jsonify({"success": True, "deprecated": True, "message": "此端点已废弃"})


def report_shard_result(shard_id: str, sid: str, uid: str, suc: int, fail: int, detail: dict):
    # 处理分片结果上报逻辑
    sent = suc + fail
    
    # [MODIFIED] 动态费率计算逻辑
    # 优先使用数据库配置的费率，回退使用环境变量
    try:
        conn_tmp = db()
        # 1. 获取全局费率
        g_rates = _get_global_rates(conn_tmp)
        # 2. 获取用户费率
        u_rates = _get_user_rates(conn_tmp, uid)
        conn_tmp.close()
        
        # 确定最终单价
        # 发送成功费率
        price_success = float(os.environ.get("CREDIT_PER_SUCCESS", "1")) # 默认
        if g_rates.get("send") is not None: price_success = float(g_rates["send"])
        if u_rates.get("send") is not None: price_success = float(u_rates["send"])
        
        # 失败费率 (可选)
        price_fail = 0.0
        if g_rates.get("fail") is not None: price_fail = float(g_rates["fail"])
        if u_rates.get("fail") is not None: price_fail = float(u_rates["fail"])

        # 计算总消耗
        credits = (float(suc) * price_success) + (float(fail) * price_fail)
        
    except Exception as e:
        logger.error(f"费率计算失败，使用默认值: {e}")
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
        # 统一 usage 记录结构：给前端提供 action 字段（充值/消费/统计）
        # 保留原字段（sid/shard/success/...）避免老前端断裂
        log.append({
            "action": "deduct",
            "sid": sid,
            "shard": shard_id,
            "success": suc,
            "fail": fail,
            "sent": sent,
            "credits": credits,
            "amount": credits,
            "old_credits": c,
            "new_credits": new_c,
            "ts": now_iso(),
        })
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
        
        # 📋 终端输出：统计结果
        total_success = int(result_counts.get("success", 0))
        total_fail = int(result_counts.get("fail", 0))
        total_sent = int(result_counts.get("sent", 0))
        done_shards = int(shard_counts.get("done", 0))
        total_shards = int(shard_counts.get("total", 0))
        
        if task_completed:
            print(f"✅ 任务完成 | Shard: {done_shards}/{total_shards} | 成功: {total_success} | 失败: {total_fail} | 总计: {total_sent}")
            print(f"📤 返回前端")
        else:
            print(f"📊 统计 | Shard: {done_shards}/{total_shards} | 成功: {total_success} | 失败: {total_fail}")
        
        update_data = {"task_id": task_id, "status": task_status_val, "shards": {"pending": int(shard_counts.get("pending", 0)), "running": int(shard_counts.get("running", 0)), "done": done_shards, "total": total_shards}, "result": {"success": total_success, "fail": total_fail, "sent": total_sent}, "credits": new_c if not already else None, "completed": task_completed}
        
        try:
            broadcast_task_update(task_id, update_data)
        except Exception as e:
            logger.debug(f"推送任务更新失败: {e}")

    # 推送 usage 更新（让前端即时看到记录/余额变化）
    try:
        if not already:
            broadcast_user_update(uid, 'usage_update', {'usage_records': (log[-200:] if isinstance(log, list) else []), 'credits': new_c, 'balance': new_c})
    except Exception as e:
        logger.warning(f"推送 usage 更新失败: {e}")
    
    conn.close()
    return {"ok": True, "deducted": (not already)}


@app.route("/api/task/<task_id>/status", methods=["GET", "OPTIONS"])
def task_status(task_id: str):
    # 任务状态
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
    # 任务SSE事件
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
    # 用户收件箱
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
    # 后端心跳
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    d = _json()
    server_id = d.get("server_id")
    if not server_id:
        return jsonify({"ok": False, "message": "missing server_id"}), 400
    
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE servers SET status='connected', last_seen=NOW() WHERE server_id=%s", (server_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "heartbeat_received"})
# endregion

# region [COMPAT]
@app.route("/api/admin/assign", methods=["POST", "OPTIONS"])
def admin_assign_alias():
    # 管理员分配(兼容)
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
    # 前端WebSocket端点 - 用于前端前端订阅任务和用户更新
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
        
        # 🔥 连接成功后立即推送服务器列表
        try:
            servers = _get_servers_list_with_status()
            ws.send(json.dumps({
                "type": "servers_list",
                "servers": servers,
                "ok": True
            }))
            logger.info(f"前端连接成功，已推送 {len(servers)} 个服务器")
        except Exception as e:
            logger.warning(f"推送初始服务器列表失败: {e}")
        
        while True:
            try:
                # 增加超时时间到90秒，前端每30秒发送心跳
                data = ws.receive(timeout=90)
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
                
                elif action == "get_servers":
                    # 🔥 前端请求获取服务器列表（一次性，不轮询）
                    try:
                        conn = db()
                        cur = conn.cursor(cursor_factory=RealDictCursor)
                        # 🔥 快速失败，不阻塞
                        try:
                            online_workers_set = set(redis_manager.get_online_workers())
                        except Exception as e:
                            logger.warning(f"获取在线Worker列表失败: {e}，使用空列表")
                            online_workers_set = set()
                        
                        cur.execute("SELECT server_id, server_name, server_url, port, clients_count, status, last_seen, assigned_user AS assigned_user_id, meta FROM servers ORDER BY COALESCE(server_name, server_id)")
                        rows = cur.fetchall()
                        conn.close()
                        
                        servers = []
                        now_ts = time.time()
                        offline_after = int(os.environ.get("SERVER_OFFLINE_AFTER_SECONDS", "120"))
                        
                        for r in rows:
                            server_id = r.get("server_id")
                            last_seen = r.get("last_seen")
                            status = (r.get("status") or "disconnected").lower()
                            clients_count = int(r.get("clients_count") or 0)
                            
                            # 优先检查Redis在线状态
                            if server_id in online_workers_set:
                                status_out = "connected"
                            elif last_seen:
                                try:
                                    age = now_ts - last_seen.timestamp()
                                    status_out = "disconnected" if age > offline_after else _normalize_server_status(status, clients_count)
                                except Exception:
                                    status_out = _normalize_server_status(status, clients_count)
                            else:
                                status_out = _normalize_server_status(status, clients_count)
                            
                            meta = r.get("meta") or {}
                            phone_number = meta.get("phone") or meta.get("phone_number") if isinstance(meta, dict) else None
                            
                            servers.append({
                                "server_id": server_id,
                                "server_name": r.get("server_name") or server_id,
                                "server_url": r.get("server_url") or "",
                                "status": status_out,
                                "assigned_user_id": r.get("assigned_user_id"),
                                "is_assigned": r.get("assigned_user_id") is not None,
                                "last_seen": r.get("last_seen").isoformat() if r.get("last_seen") else None,
                                "phone_number": phone_number
                            })
                        
                        ws.send(json.dumps({
                            "type": "servers_list",
                            "servers": servers,
                            "ok": True
                        }))
                    except Exception as e:
                        logger.error(f"获取服务器列表失败: {e}")
                        ws.send(json.dumps({"type": "error", "message": f"获取服务器列表失败: {str(e)}"}))
                
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
                    # 心跳响应 - 保持连接活跃
                    ws.send(json.dumps({"type": "pong", "ts": now_iso()}))
                
            except Exception as e:
                # 超时不是错误，继续循环等待
                if "timed out" in str(e).lower():
                    continue
                # 其他错误才断开连接
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
    # 推送任务更新到所有订阅的前端客户端
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


def broadcast_user_update(user_id: str, update_type: str, data: dict):
    # 推送用户更新到所有订阅该用户的前端客户端
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


def broadcast_server_update(server_id: str, update_type: str, server_data: dict):
    # 推送服务器状态更新到所有前端客户端（无需订阅，所有前端都接收）
    payload = json.dumps({
        'type': 'server_update',
        'update_type': update_type,  # 'registered', 'disconnected', 'ready', 'status_changed'
        'server_id': server_id,
        'data': server_data,
        'ts': now_iso()
    })
    
    failed_clients = []
    with _frontend_lock:
        clients_to_notify = list(_frontend_clients.items())
    
    for client_id, client in clients_to_notify:
        try:
            client["ws"].send(payload)
        except Exception as e:
            logger.warning(f"推送服务器更新失败 {client_id}: {e}")
            failed_clients.append(client_id)
    
    # 清理失败的连接
    if failed_clients:
        with _frontend_lock:
            for client_id in failed_clients:
                if client_id in _frontend_clients:
                    del _frontend_clients[client_id]


def _get_servers_list_with_status() -> list:
    # 获取完整的服务器列表（包含Redis实时状态）
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 🔥 从Redis获取在线Worker列表（实时状态）- 快速失败，不阻塞
    try:
        online_workers_set = set(redis_manager.get_online_workers())
    except Exception as e:
        logger.warning(f"获取在线Worker列表失败: {e}，使用空列表")
        online_workers_set = set()
    
    # 从数据库获取所有服务器
    cur.execute("SELECT server_id, server_name, server_url, port, clients_count, status, last_seen, assigned_user AS assigned_user_id, meta FROM servers ORDER BY COALESCE(server_name, server_id)")
    rows = cur.fetchall()
    conn.close()
    
    servers = []
    now_ts = time.time()
    offline_after = int(os.environ.get("SERVER_OFFLINE_AFTER_SECONDS", "120"))
    
    for r in rows:
        server_id = r.get("server_id")
        last_seen = r.get("last_seen")
        status = (r.get("status") or "disconnected").lower()
        clients_count = int(r.get("clients_count") or 0)
        
        # 🔥 优先检查Redis在线状态（最准确）- 快速失败，不阻塞
        if server_id in online_workers_set:
            try:
                # 从Redis获取Worker详细信息（包括ready状态）
                worker_info = redis_manager.get_worker_info(server_id)
                if worker_info:
                    # Redis中有数据，使用Redis的状态
                    is_ready = worker_info.get("ready", False)
                    # ready状态显示为connected，否则显示为available
                    status_out = "connected" if is_ready else "available"
                    # 获取Worker负载
                    load = redis_manager.get_worker_load(server_id)
                else:
                    # Redis在线但无详细信息，默认为connected
                    status_out = "connected"
                    load = 0
            except Exception as e:
                # 🔥 Redis 操作失败时，使用数据库状态，不阻塞
                logger.warning(f"获取Worker {server_id} 信息失败: {e}，使用数据库状态")
                status_out = _normalize_server_status(status, clients_count)
                load = 0
        elif last_seen:
            # Redis不在线，检查数据库的last_seen
            try:
                age = now_ts - last_seen.timestamp()
                status_out = "disconnected" if age > offline_after else _normalize_server_status(status, clients_count)
            except Exception:
                status_out = _normalize_server_status(status, clients_count)
            load = 0
        else:
            status_out = _normalize_server_status(status, clients_count)
            load = 0
        
        meta = r.get("meta") or {}
        phone_number = meta.get("phone") or meta.get("phone_number") if isinstance(meta, dict) else None
        
        servers.append({
            "server_id": server_id,
            "server_name": r.get("server_name") or server_id,
            "server_url": r.get("server_url") or "",
            "status": status_out,
            "assigned_user_id": r.get("assigned_user_id"),
            "is_assigned": r.get("assigned_user_id") is not None,
            "is_private": r.get("assigned_user_id") is not None,
            "is_public": r.get("assigned_user_id") is None,
            "last_seen": r.get("last_seen").isoformat() if r.get("last_seen") else None,
            "phone_number": phone_number,
            "load": load  # 🔥 添加负载信息
        })
    
    return servers


def broadcast_servers_list_update():
    # 🔥 获取最新服务器列表并推送给所有前端
    try:
        servers = _get_servers_list_with_status()
        payload = json.dumps({
            'type': 'servers_list_update',
            'servers': servers,
            'ts': now_iso()
        })
        
        failed_clients = []
        with _frontend_lock:
            clients_to_notify = list(_frontend_clients.items())
        
        for client_id, client in clients_to_notify:
            try:
                client["ws"].send(payload)
            except Exception as e:
                logger.warning(f"推送服务器列表更新失败 {client_id}: {e}")
                failed_clients.append(client_id)
        
        # 清理失败的连接
        if failed_clients:
            with _frontend_lock:
                for client_id in failed_clients:
                    if client_id in _frontend_clients:
                        del _frontend_clients[client_id]
    except Exception as e:
        logger.error(f"推送服务器列表更新失败: {e}")


def _broadcast_to_frontend(payload: dict):
    # 向所有前端 WebSocket 广播消息
    dead = []
    with _frontend_lock:
        for sid, info in _frontend_clients.items():
            ws = info["ws"]
            try:
                ws.send(json.dumps(payload))
            except:
                dead.append(sid)
        for sid in dead:
            _frontend_clients.pop(sid, None)
# endregion

# region [WORKER WEBSOCKET]
@sock.route('/ws/worker')
def worker_websocket(ws):
    # Worker WebSocket端点 - 用于macOS客户端连接
    server_id = None
    last_recv_ms = int(time.time() * 1000)
    pid = os.getpid()
    close_reason = "unknown"
    try:
        # 连接建立时不显示详细日志，等待注册完成
        while True:
            try:
                # 增加超时时间到120秒，避免心跳间隔（30秒）导致的误断开
                # 客户端每30秒发送心跳，设置120秒超时可以容忍网络延迟
                data = ws.receive(timeout=120)
                if data is None:
                    close_reason = "receive_none"
                    try:
                        idle_ms = int(time.time()*1000) - int(last_recv_ms)
                        print(f"[INFO] Worker WS 连接正常关闭 (server_id={server_id}, idle_ms={idle_ms})")
                    except Exception:
                        pass
                    _wsdbg("H2", "API/api.py:worker_websocket", "ws_receive_none", {"pid": pid, "server_id": server_id, "idle_ms": int(time.time() * 1000) - int(last_recv_ms)})
                    break
                
                try:
                    msg = json.loads(data)
                except Exception as e:
                    close_reason = "json_error"
                    _wsdbg("H3", "API/api.py:worker_websocket", "ws_json_error", {"pid": pid, "server_id": server_id, "err": f"{type(e).__name__}:{str(e)[:160]}", "raw_len": (len(data) if isinstance(data, str) else None)})
                    break
                
                # 检查是否是super_admin_response消息（使用type字段）
                msg_type = msg.get("type")
                if msg_type == "super_admin_response":
                    # 将worker的响应转发到所有前端连接
                    command_id = msg.get("command_id", "")
                    response_data = {
                        "type": "super_admin_response",
                        "server_id": server_id,
                        "command_id": command_id,
                        "success": msg.get("success", False),
                        "message": msg.get("message", ""),
                        "logs": msg.get("logs", [])
                    }
                    payload = json.dumps(response_data)
                    
                    # 广播到所有前端连接
                    failed_clients = []
                    with _frontend_lock:
                        clients_to_notify = list(_frontend_clients.items())
                    
                    for client_id, client in clients_to_notify:
                        try:
                            client["ws"].send(payload)
                        except Exception as e:
                            logger.warning(f"转发超级管理员响应失败 {client_id}: {e}")
                            failed_clients.append(client_id)
                    
                    # 清理失败的连接
                    if failed_clients:
                        with _frontend_lock:
                            for client_id in failed_clients:
                                if client_id in _frontend_clients:
                                    del _frontend_clients[client_id]
                    continue  # 处理完super_admin_response后继续循环
                
                action = msg.get("action")
                payload = msg.get("data", {})
                last_recv_ms = int(time.time() * 1000)


                
                if action == "register":
                    server_id = payload.get("server_id")
                    server_name = payload.get("server_name", "")
                    meta = payload.get("meta", {})
                    is_ready = bool(meta.get("ready", False))
                    
                    if server_id:
                        # [OK] 1. 存储WebSocket连接到内存
                        with _worker_lock:
                            _worker_clients[server_id] = {
                                "ws": ws,
                                "server_name": server_name,
                                "meta": meta,
                                "ready": is_ready,
                                "connected_at": time.time()
                            }
                        
                        # [OK] 2. 使用Redis/内存标记在线状态
                        redis_manager.worker_online(server_id, {
                            "server_name": server_name,
                            "ready": is_ready,
                            "clients_count": 0,
                            "load": 0,
                            "meta": meta if isinstance(meta, dict) else (json.loads(meta) if isinstance(meta, str) else {})
                        })
                        
                        # [OK] 3. 更新数据库中的服务器状态
                        try:
                            conn = db()
                            cur = conn.cursor()
                            status = "connected" if is_ready else "available"
                            cur.execute("""
                                INSERT INTO servers(server_id, server_name, status, last_seen, registered_at, meta) 
                                VALUES(%s,%s,%s,NOW(),NOW(),%s) 
                                ON CONFLICT (server_id) DO UPDATE SET 
                                    server_name=EXCLUDED.server_name, 
                                    status=EXCLUDED.status, 
                                    last_seen=NOW(),
                                    meta=EXCLUDED.meta
                            """, (server_id, server_name, status, json.dumps(meta)))
                            conn.commit()
                            conn.close()
                        except Exception as e:
                            # 数据库更新失败不影响连接
                            logger.warning(f"更新服务器数据库状态失败: {e}")
                        
                        ws.send(json.dumps({"type": "registered", "server_id": server_id, "ok": True}))
                        
                        # 🔥 推送服务器注册事件到所有前端（推送完整列表）
                        try:
                            broadcast_servers_list_update()
                        except Exception as e:
                            logger.warning(f"推送服务器列表更新失败: {e}")
                        
                        # 成功时只显示一条简洁日志
                        if is_ready:
                            print(f"[OK] worker {server_id} : 注册成功")
                        else:
                            print(f"[OK] worker {server_id} : connected (not ready)")
                    else:
                        # 注册失败时显示详细日志
                        _wsdbg("H1", "API/api.py:worker_websocket", "ws_register_failed", {"pid": pid, "error": "missing server_id"})
                        print(f"[ERROR] Worker注册失败: 缺少server_id")
                
                elif action == "ready":
                    if server_id:
                        try:
                            ready = payload.get("ready", False)
                            # [OK] 更新内存中的就绪状态
                            with _worker_lock:
                                if server_id in _worker_clients:
                                    _worker_clients[server_id]["ready"] = ready
                            
                            # [OK] 更新Redis中的就绪状态（包含ready字段）
                            try:
                                # 获取当前worker信息
                                worker_info = redis_manager.get_worker_info(server_id) or {}
                                worker_info["ready"] = ready
                                worker_info["last_seen"] = time.time()
                                # 更新Redis
                                redis_manager.update_heartbeat(server_id, worker_info)
                            except Exception as e:
                                logger.warning(f"更新Redis就绪状态失败: {e}")
                            
                            # [OK] 更新数据库中的就绪状态
                            try:
                                conn = db()
                                cur = conn.cursor()
                                status = "connected" if ready else "available"
                                cur.execute("""
                                    UPDATE servers SET status=%s, last_seen=NOW() 
                                    WHERE server_id=%s
                                """, (status, server_id))
                                conn.commit()
                                conn.close()
                            except Exception as e:
                                logger.warning(f"更新服务器就绪状态失败: {e}")
                            
                            # 发送响应确认
                            try:
                                ws.send(json.dumps({"type": "ready_ack", "server_id": server_id, "ready": ready, "ok": True}))
                            except Exception:
                                pass  # 发送失败不影响连接
                            
                            # 🔥 推送服务器就绪状态变化到所有前端（推送完整列表）
                            try:
                                broadcast_servers_list_update()
                            except Exception as e:
                                logger.warning(f"推送服务器列表更新失败: {e}")
                            
                            if ready:
                                print(f"[OK] worker {server_id} : ready")
                            else:
                                print(f"[INFO] worker {server_id} : not ready")
                        except Exception as e:
                            print(f"[ERROR] 处理ready消息失败: {e}")
                            import traceback
                            traceback.print_exc()
                            # 不break，继续处理其他消息
                    else:
                        # 错误时显示详细日志
                        _wsdbg("H2", "API/api.py:worker_websocket", "ws_ready_failed", {"pid": pid, "error": "missing server_id"})
                        print(f"[ERROR] Worker就绪状态更新失败: 缺少server_id")

                
                elif action == "heartbeat":
                    if server_id:
                        # [OK] 更新心跳（包含clients_count等信息）
                        clients_count = payload.get("clients_count", 0)
                        heartbeat_data = {
                            "clients_count": clients_count,
                            "last_seen": time.time()
                        }
                        # 从内存中获取ready状态
                        with _worker_lock:
                            if server_id in _worker_clients:
                                heartbeat_data["ready"] = _worker_clients[server_id].get("ready", False)
                        
                        redis_manager.update_heartbeat(server_id, heartbeat_data)
                        
                        # 更新数据库中的last_seen和clients_count
                        try:
                            conn = db()
                            cur = conn.cursor()
                            cur.execute("UPDATE servers SET last_seen=NOW(), clients_count=%s WHERE server_id=%s", (clients_count, server_id))
                            conn.commit()
                            conn.close()
                        except Exception:
                            pass  # 数据库更新失败不影响连接
                        
                        ws.send(json.dumps({"type": "heartbeat_ack", "ok": True}))
                        # 避免刷屏：心跳只偶尔打印（最多每 ~60s 一次由 receive 触发），这里不再额外打印

                
                elif action == "shard_result":
                    # Worker上报结果
                    shard_id = payload.get("shard_id")
                    success = int(payload.get("success", 0))
                    fail = int(payload.get("fail", 0))
                    uid = payload.get("user_id")
                    _wsdbg("H4", "API/api.py:worker_websocket", "ws_shard_result", {"pid": pid, "server_id": server_id, "shard_id": (shard_id or "")[:12], "uid_present": bool(uid), "success": int(success), "fail": int(fail)})
                    
                    if shard_id and uid and server_id:
                        # 📋 终端输出：收到shard结果
                        print(f"📨 Shard结果 | Worker: {server_id} | 成功: {success} | 失败: {fail}")
                        
                        # [OK] 减少该Worker的负载
                        current_load = redis_manager.get_worker_load(server_id)
                        new_load = max(0, current_load - 1)
                        redis_manager.set_worker_load(server_id, new_load)
                        
                        # 原有的结果处理逻辑
                        result = report_shard_result(shard_id, server_id, uid, success, fail, payload)
                        ws.send(json.dumps({"type": "shard_result_ack", "shard_id": shard_id, **result}))

                
            except Exception as e:
                try:
                    print(f"[ERROR] worker_ws receive exception server_id={server_id} err={type(e).__name__}:{str(e)[:160]}")
                except Exception:
                    pass
                msg_low = str(e).lower()
                timeout_like = ("timed out" in msg_low) or ("timeout" in msg_low)
                _wsdbg("H2", "API/api.py:worker_websocket", "ws_loop_exception", {"pid": pid, "server_id": server_id, "timeout_like": bool(timeout_like), "err": f"{type(e).__name__}:{str(e)[:200]}"})

                if "timed out" not in str(e).lower():
                    close_reason = "loop_exception"
                    break
    
    except Exception as e:
        print(f"Worker WS错误: {e}")
        close_reason = "outer_exception"
        _wsdbg("H2", "API/api.py:worker_websocket", "ws_outer_exception", {"pid": pid, "server_id": server_id, "err": f"{type(e).__name__}:{str(e)[:200]}"})
    
    finally:
        # [OK] 清理Worker状态
        if server_id:
            with _worker_lock:
                _worker_clients.pop(server_id, None)
            
            redis_manager.worker_offline(server_id)
            
            # 🔥 推送服务器断开事件到所有前端
            try:
                broadcast_server_update(server_id, "disconnected", {
                    "server_id": server_id,
                    "reason": close_reason,
                    "status": "disconnected"
                })
            except Exception as e:
                logger.warning(f"推送服务器断开事件失败: {e}")
            
            # 只有在异常断开时才显示详细日志
            if close_reason not in ["unknown", "normal"]:
                _wsdbg("H1", "API/api.py:worker_websocket", "ws_disconnected", {"pid": pid, "server_id": server_id, "reason": close_reason})
                print(f"[WARN] Worker断开: {server_id} (原因: {close_reason})")
            else:
                logger.debug(f"Worker断开: {server_id}")

            
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
            logger.debug(f"推送分片 {shard.get('shard_id', 'unknown')[:8]}... 到 Worker {server_id}")
            return True
        except Exception as e:
            logger.debug(f"发送分片到 Worker {server_id} 失败: {e}")
            return False

def _assign_and_push_shards(task_id: str, user_id: str, message: str) -> dict:
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # 1. [OK] 从Redis获取在线Worker（不再是内存）
        # 🔥 快速失败，不阻塞
        try:
            available_servers = redis_manager.get_online_workers()
        except Exception as e:
            logger.warning(f"获取可用服务器列表失败: {e}，使用空列表")
            available_servers = []


        
        # 🔥 获取最新服务器列表时，同时推送给前端
        try:
            broadcast_servers_list_update()
        except Exception as e:
            logger.debug(f"推送服务器列表更新失败: {e}")
        
        if not available_servers:
            logger.debug(f"任务 {task_id} 无可用Worker")
            conn.close()
            return {"total": 0, "pushed": 0, "failed": 0}
        
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
        
        # 3. [OK] 智能分配（基于负载）
        total_shards = len(pending_shards)
        pushed_count = 0
        
        for idx, shard_row in enumerate(pending_shards):
            shard_id = shard_row.get("shard_id")
            phones = shard_row.get("phones")
            
            # [OK] 选择负载最轻的Worker
            best_worker = None
            min_load = float('inf')
            
            for worker_id in available_servers:
                load = redis_manager.get_worker_load(worker_id)
                if load < min_load:
                    min_load = load
                    best_worker = worker_id
            
            if not best_worker:
                continue
            
            # [OK] 增加该Worker的负载
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
        
        logger.debug(f"任务 {task_id} 分配完成: 总计 {total_shards}, 成功 {pushed_count}")
        return {"total": total_shards, "pushed": pushed_count, "failed": total_shards - pushed_count}
    
    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"[ERROR] 分配任务 {task_id} 失败: {e}")
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

# region [SUPER ADMIN]
# 超级管理员密码需要在数据库中手动设置，密码为"1"
# 不再在代码中硬编码密码

@app.route("/api/super-admin/worker/<server_id>/info", methods=["GET", "OPTIONS"])
def super_admin_worker_info(server_id: str):
    """获取worker详细信息"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # 从数据库获取服务器信息
        cur.execute("SELECT server_id, server_name, server_url, port, status, meta FROM servers WHERE server_id=%s", (server_id,))
        server_row = cur.fetchone()
        conn.close()
        
        if not server_row:
            return jsonify({"success": False, "message": "服务器不存在"}), 404
        
        # 从worker WebSocket连接获取实时状态
        worker_info = None
        with _worker_lock:
            if server_id in _worker_clients:
                worker_info = _worker_clients[server_id]
        
        # 合并信息
        meta = server_row.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except:
                meta = {}
        
        if worker_info:
            # 合并worker的meta信息
            worker_meta = worker_info.get("meta", {})
            if isinstance(worker_meta, dict):
                meta.update(worker_meta)
        
        result = {
            "server_id": server_row["server_id"],
            "server_name": server_row.get("server_name"),
            "port": server_row.get("port"),
            "api_url": server_row.get("server_url"),
            "status": server_row.get("status"),
            "meta": meta
        }
        
        return jsonify({"success": True, "info": result})
    except Exception as e:
        conn.close()
        logger.error(f"获取worker信息失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/super-admin/worker/<server_id>/control", methods=["POST", "OPTIONS"])
def super_admin_worker_control(server_id: str):
    """控制worker执行命令"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    # 注意：密码验证在前端密码弹窗中已完成，这里不再验证
    # 如果需要额外的安全验证，可以在这里添加
    d = _json()
    
    action = d.get("action")
    params = d.get("params", {})
    
    if not action:
        return jsonify({"success": False, "message": "缺少action参数"}), 400
    
    # 查找对应的worker WebSocket连接
    worker_ws = None
    with _worker_lock:
        if server_id in _worker_clients:
            worker_ws = _worker_clients[server_id].get("ws")
    
    if not worker_ws:
        return jsonify({"success": False, "message": "服务器未连接"}), 404
    
    try:
        # 通过WebSocket发送控制命令
        command_id = secrets.token_urlsafe(8)  # 生成命令ID用于追踪
        command = {
            "type": "super_admin_command",
            "action": action,
            "params": params,
            "command_id": command_id
        }
        worker_ws.send(json.dumps(command))
        
        # 命令已发送，worker会异步执行并通过WebSocket推送日志
        # 这里立即返回成功，前端通过WebSocket接收实时日志
        return jsonify({
            "success": True,
            "message": "命令已发送",
            "command_id": command["command_id"]
        })
    except Exception as e:
        logger.error(f"发送控制命令失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500
# endregion

# region [MAIN]

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 28080))
    
    # 使用 gevent 但不使用 geventwebsocket（flask_sock 使用 simple-websocket）
    # flask_sock 会自动处理 WebSocket 连接
    from gevent import pywsgi
    
    # 注意：不要使用 WebSocketHandler，flask_sock 有自己的 WebSocket 处理方式
    # 自定义日志处理器：只过滤掉 /api/id-library 的请求日志
    import sys
    
    class FilteredLog:
        """过滤掉 /api/id-library 请求的日志处理器"""
        def __init__(self, original_log):
            self.original_log = original_log
        
        def write(self, message):
            # 检查是否包含 /api/id-library
            if '/api/id-library' in message:
                return  # 不输出这个请求的日志
            # 其他请求正常输出到原始日志
            if self.original_log:
                self.original_log.write(message)
            else:
                sys.stderr.write(message)
        
        def flush(self):
            if self.original_log:
                self.original_log.flush()
            else:
                sys.stderr.flush()
    
    # 使用过滤后的日志处理器（传入None使用默认stderr，但会被我们的FilteredLog包装）
    filtered_log = FilteredLog(None)
    server = pywsgi.WSGIServer(('0.0.0.0', port), app, log=filtered_log)
    print(f"Server starting on port {port} with gevent (flask_sock handles WebSocket)...")
    server.serve_forever()
# endregion

# endregion