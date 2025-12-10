#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API服务器 - 使用PostgreSQL数据库
用于用户注册、登录、积分管理、服务器分配、数据存储

运行方式：
    本地：python api_server.py
    Railway：自动部署（通过 Procfile）

数据库：
    使用PostgreSQL（Railway提供免费PostgreSQL数据库）
    环境变量：DATABASE_URL（Railway自动设置）
"""

import os
import hashlib
import secrets
import json
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

app = Flask(__name__)
# 配置CORS，允许所有来源（生产环境建议限制特定域名）
CORS(app, resources={r"/api/*": {"origins": "*", "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]}})

# ==================== 数据库连接 ====================
def get_db_connection():
    """获取PostgreSQL数据库连接"""
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        # 本地开发环境，使用默认配置
        return psycopg2.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            database=os.environ.get('DB_NAME', 'autosender'),
            user=os.environ.get('DB_USER', 'postgres'),
            password=os.environ.get('DB_PASSWORD', 'postgres'),
            port=os.environ.get('DB_PORT', '5432')
        )
    
    # Railway环境，解析DATABASE_URL
    result = urlparse(database_url)
    return psycopg2.connect(
        host=result.hostname,
        database=result.path[1:],
        user=result.username,
        password=result.password,
        port=result.port
    )

def init_database():
    """初始化数据库表结构"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 用户表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username VARCHAR(255) PRIMARY KEY,
            user_id VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            email VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 用户数据表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_data (
            user_id VARCHAR(255) PRIMARY KEY,
            credits DECIMAL(10, 2) DEFAULT 1000.0,
            statistics JSONB DEFAULT '[]'::jsonb,
            usage_logs JSONB DEFAULT '[]'::jsonb,
            consumption_logs JSONB DEFAULT '[]'::jsonb,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Token表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_tokens (
            token VARCHAR(255) PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_data(user_id) ON DELETE CASCADE
        )
    """)
    
    # 全局服务器池表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS servers (
            server_id VARCHAR(255) PRIMARY KEY,
            server_name VARCHAR(255) NOT NULL,
            server_url VARCHAR(500) NOT NULL,
            status VARCHAR(50) DEFAULT 'available',
            is_public BOOLEAN DEFAULT TRUE,
            assigned_user_id VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (assigned_user_id) REFERENCES user_data(user_id) ON DELETE SET NULL
        )
    """)
    
    # 服务器分配表（记录分配历史）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS server_assignments (
            assignment_id SERIAL PRIMARY KEY,
            server_id VARCHAR(255) NOT NULL,
            user_id VARCHAR(255) NOT NULL,
            assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            unassigned_at TIMESTAMP,
            FOREIGN KEY (server_id) REFERENCES servers(server_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES user_data(user_id) ON DELETE CASCADE
        )
    """)
    
    # 管理员账户表（用于登录窗口的管理员账号）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_accounts (
            admin_id VARCHAR(255) PRIMARY KEY,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 服务器管理密码表（独立的，用于进入服务器管理面板）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS server_manager_config (
            config_key VARCHAR(255) PRIMARY KEY,
            config_value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    
    # 初始化服务器管理密码（如果不存在）
    default_manager_password = '159357'
    cur.execute("SELECT config_key FROM server_manager_config WHERE config_key = 'password'")
    if not cur.fetchone():
        cur.execute("""
            INSERT INTO server_manager_config (config_key, config_value)
            VALUES ('password', %s)
        """, (hash_password(default_manager_password),))
        conn.commit()
        print(f"✅ 已创建默认服务器管理密码: {default_manager_password}")
    
    cur.close()
    conn.close()

# ==================== 工具函数 ====================
def hash_password(password):
    """密码哈希"""
    return hashlib.sha256(password.encode()).hexdigest()

def generate_token():
    """生成token"""
    return secrets.token_urlsafe(32)

# ==================== API接口 ====================

@app.route('/api/register', methods=['POST'])
def register():
    """用户注册"""
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        email = data.get('email', '').strip()
        
        if not username or not password:
            return jsonify({"success": False, "message": "用户名和密码不能为空"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 检查用户名是否已存在
        cur.execute("SELECT username FROM users WHERE username = %s", (username,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "用户名已存在"}), 400
        
        # 创建用户
        user_id = f"user_{secrets.token_urlsafe(16)}"
        cur.execute("""
            INSERT INTO users (username, user_id, password_hash, email)
            VALUES (%s, %s, %s, %s)
        """, (username, user_id, hash_password(password), email))
        
        # 初始化用户数据
        cur.execute("""
            INSERT INTO user_data (user_id, credits)
            VALUES (%s, 1000.0)
        """, (user_id,))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "message": "注册成功", "user_id": user_id})
    except Exception as e:
        return jsonify({"success": False, "message": f"注册失败: {str(e)}"}), 500

@app.route('/api/login', methods=['POST'])
def login():
    """用户登录"""
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        
        if not username or not password:
            return jsonify({"success": False, "message": "用户名和密码不能为空"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT u.user_id, u.password_hash
            FROM users u
            WHERE u.username = %s
        """, (username,))
        user = cur.fetchone()
        
        if not user or user['password_hash'] != hash_password(password):
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "用户名或密码错误"}), 401
        
        # 生成token
        token = generate_token()
        user_id = user['user_id']
        
        cur.execute("""
            INSERT INTO user_tokens (token, user_id)
            VALUES (%s, %s)
        """, (token, user_id))
        
        # 获取用户积分
        cur.execute("SELECT credits FROM user_data WHERE user_id = %s", (user_id,))
        credits_data = cur.fetchone()
        credits = credits_data['credits'] if credits_data else 1000.0
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({
            "success": True,
            "message": "登录成功",
            "user_id": user_id,
            "token": token,
            "credits": float(credits)
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"登录失败: {str(e)}"}), 500

@app.route('/api/verify', methods=['POST'])
def verify():
    """验证用户token"""
    try:
        data = request.json
        user_id = data.get('user_id')
        token = data.get('token')
        
        if not user_id or not token:
            return jsonify({"valid": False}), 401
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT token FROM user_tokens
            WHERE token = %s AND user_id = %s
        """, (token, user_id))
        
        valid = cur.fetchone() is not None
        cur.close()
        conn.close()
        
        return jsonify({"valid": valid})
    except:
        return jsonify({"valid": False}), 401

@app.route('/api/user/<user_id>/credits', methods=['GET'])
def get_credits(user_id):
    """获取用户积分"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT credits FROM user_data WHERE user_id = %s", (user_id,))
        data = cur.fetchone()
        cur.close()
        conn.close()
        
        return jsonify({"credits": float(data['credits']) if data else 0})
    except:
        return jsonify({"credits": 0}), 500

@app.route('/api/user/<user_id>/deduct', methods=['POST'])
def deduct_credits(user_id):
    """扣除用户积分"""
    try:
        data = request.json
        amount = float(data.get('amount', 0))
        
        if amount <= 0:
            return jsonify({"success": False, "message": "扣除金额必须大于0"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("SELECT credits FROM user_data WHERE user_id = %s", (user_id,))
        user_data = cur.fetchone()
        if not user_data:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "用户不存在"}), 404
        
        current_credits = float(user_data['credits'])
        if current_credits < amount:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "积分不足"}), 400
        
        # 扣除积分
        new_credits = current_credits - amount
        cur.execute("""
            UPDATE user_data
            SET credits = %s, last_updated = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (new_credits, user_id))
        
        # 记录消费记录
        cur.execute("""
            UPDATE user_data
            SET consumption_logs = consumption_logs || %s::jsonb
            WHERE user_id = %s
        """, (json.dumps([{
            "timestamp": datetime.now().isoformat(),
            "amount": amount,
            "type": "deduct",
            "description": f"扣除 {amount} 积分"
        }]), user_id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "credits": new_credits})
    except Exception as e:
        return jsonify({"success": False, "message": f"扣除失败: {str(e)}"}), 500

@app.route('/api/user/<user_id>/add_credits', methods=['POST'])
def add_credits(user_id):
    """充值积分"""
    try:
        data = request.json
        amount = float(data.get('amount', 0))
        
        if amount <= 0:
            return jsonify({"success": False, "message": "充值金额必须大于0"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("SELECT credits FROM user_data WHERE user_id = %s", (user_id,))
        user_data = cur.fetchone()
        if not user_data:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "用户不存在"}), 404
        
        current_credits = float(user_data['credits'])
        new_credits = current_credits + amount
        
        cur.execute("""
            UPDATE user_data
            SET credits = %s, last_updated = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (new_credits, user_id))
        
        # 记录充值记录
        cur.execute("""
            UPDATE user_data
            SET consumption_logs = consumption_logs || %s::jsonb
            WHERE user_id = %s
        """, (json.dumps([{
            "timestamp": datetime.now().isoformat(),
            "amount": amount,
            "type": "add",
            "description": f"充值 {amount} 积分"
        }]), user_id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "credits": new_credits, "message": f"成功充值 {amount} 积分"})
    except Exception as e:
        return jsonify({"success": False, "message": f"充值失败: {str(e)}"}), 500

# ==================== 服务器管理API ====================

@app.route('/api/servers', methods=['GET'])
def get_all_servers():
    """获取所有服务器列表"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT server_id, server_name, server_url, status, is_public, assigned_user_id, last_seen
            FROM servers
            ORDER BY created_at DESC
        """)
        servers = cur.fetchall()
        cur.close()
        conn.close()
        
        return jsonify({
            "success": True,
            "servers": [dict(s) for s in servers]
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/servers', methods=['POST'])
def register_server():
    """注册新服务器到全局池"""
    try:
        data = request.json
        server_id = data.get('server_id')
        server_name = data.get('server_name')
        server_url = data.get('server_url')
        
        if not server_id or not server_name or not server_url:
            return jsonify({"success": False, "message": "服务器信息不完整"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 检查是否已存在
        cur.execute("SELECT server_id FROM servers WHERE server_id = %s", (server_id,))
        if cur.fetchone():
            # 更新最后活跃时间
            cur.execute("""
                UPDATE servers
                SET last_seen = CURRENT_TIMESTAMP, status = 'available'
                WHERE server_id = %s
            """, (server_id,))
        else:
            # 插入新服务器
            cur.execute("""
                INSERT INTO servers (server_id, server_name, server_url, status, is_public)
                VALUES (%s, %s, %s, 'available', TRUE)
            """, (server_id, server_name, server_url))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "message": "服务器注册成功"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/servers/<server_id>/assign', methods=['POST'])
def assign_server(server_id):
    """分配服务器给用户（设置为独享）"""
    try:
        data = request.json
        user_id = data.get('user_id')
        
        if not user_id:
            return jsonify({"success": False, "message": "用户ID不能为空"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 检查服务器是否存在
        cur.execute("SELECT assigned_user_id FROM servers WHERE server_id = %s", (server_id,))
        server = cur.fetchone()
        if not server:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "服务器不存在"}), 404
        
        # 更新服务器分配
        cur.execute("""
            UPDATE servers
            SET assigned_user_id = %s, is_public = FALSE
            WHERE server_id = %s
        """, (user_id, server_id))
        
        # 记录分配历史
        cur.execute("""
            INSERT INTO server_assignments (server_id, user_id)
            VALUES (%s, %s)
        """, (server_id, user_id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "message": "服务器分配成功"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/servers/<server_id>/unassign', methods=['POST'])
def unassign_server(server_id):
    """取消服务器分配（恢复为公共服务器）"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 获取当前分配的用户
        cur.execute("SELECT assigned_user_id FROM servers WHERE server_id = %s", (server_id,))
        server = cur.fetchone()
        if not server:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "服务器不存在"}), 404
        
        user_id = server[0]
        
        # 更新服务器为公共
        cur.execute("""
            UPDATE servers
            SET assigned_user_id = NULL, is_public = TRUE
            WHERE server_id = %s
        """, (server_id,))
        
        # 更新分配历史
        if user_id:
            cur.execute("""
                UPDATE server_assignments
                SET unassigned_at = CURRENT_TIMESTAMP
                WHERE server_id = %s AND user_id = %s AND unassigned_at IS NULL
            """, (server_id, user_id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "message": "服务器已恢复为公共服务器"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/users/<user_id>/available-servers', methods=['GET'])
def get_available_servers(user_id):
    """获取用户可用的服务器列表（独享服务器 + 公共服务器）"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 获取用户的独享服务器
        cur.execute("""
            SELECT server_id, server_name, server_url, status
            FROM servers
            WHERE assigned_user_id = %s AND status = 'available'
        """, (user_id,))
        exclusive_servers = cur.fetchall()
        
        # 获取所有公共服务器（排除所有已分配的）
        cur.execute("""
            SELECT server_id, server_name, server_url, status
            FROM servers
            WHERE is_public = TRUE AND assigned_user_id IS NULL AND status = 'available'
        """)
        public_servers = cur.fetchall()
        
        cur.close()
        conn.close()
        
        return jsonify({
            "success": True,
            "exclusive_servers": [dict(s) for s in exclusive_servers],
            "public_servers": [dict(s) for s in public_servers],
            "total": len(exclusive_servers) + len(public_servers)
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/server-manager/verify', methods=['POST', 'OPTIONS'])
def verify_server_manager_password():
    """验证服务器管理密码（用于进入服务器管理面板）"""
    # 处理CORS预检请求
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    
    try:
        data = request.json
        password = data.get('password', '').strip()
        
        if not password:
            return jsonify({"success": False, "message": "密码不能为空"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 从服务器管理配置表验证密码
        cur.execute("""
            SELECT config_value
            FROM server_manager_config
            WHERE config_key = 'password'
        """)
        config = cur.fetchone()
        
        # 如果配置不存在，自动创建默认密码
        if not config:
            default_password = '159357'
            cur.execute("""
                INSERT INTO server_manager_config (config_key, config_value)
                VALUES ('password', %s)
                ON CONFLICT (config_key) DO NOTHING
            """, (hash_password(default_password),))
            conn.commit()
            # 重新查询
            cur.execute("""
                SELECT config_value
                FROM server_manager_config
                WHERE config_key = 'password'
            """)
            config = cur.fetchone()
        
        cur.close()
        conn.close()
        
        if config and config['config_value'] == hash_password(password):
            return jsonify({
                "success": True,
                "message": "密码验证成功"
            })
        else:
            return jsonify({"success": False, "message": "密码错误，默认密码为：159357"}), 401
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"验证服务器管理密码错误: {error_detail}")
        return jsonify({"success": False, "message": f"服务器错误: {str(e)}"}), 500

@app.route('/api/server-manager/password', methods=['GET', 'OPTIONS'])
def get_server_manager_password():
    """获取服务器管理密码配置信息（不包括密码本身）"""
    # 处理CORS预检请求
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT config_key, created_at, last_updated
            FROM server_manager_config
            WHERE config_key = 'password'
        """)
        config = cur.fetchone()
        
        cur.close()
        conn.close()
        
        if config:
            return jsonify({
                "success": True,
                "config": {
                    "config_key": config['config_key'],
                    "created_at": config['created_at'].isoformat() if config['created_at'] else None,
                    "last_updated": config['last_updated'].isoformat() if config['last_updated'] else None
                }
            })
        else:
            return jsonify({"success": False, "message": "配置不存在"}), 404
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/server-manager/password', methods=['PUT', 'OPTIONS'])
def update_server_manager_password():
    """修改服务器管理密码"""
    # 处理CORS预检请求
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    
    try:
        data = request.json
        new_password = data.get('password', '').strip()
        
        if not new_password:
            return jsonify({"success": False, "message": "新密码不能为空"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 检查配置是否存在
        cur.execute("SELECT config_key FROM server_manager_config WHERE config_key = 'password'")
        if not cur.fetchone():
            # 如果不存在，创建
            cur.execute("""
                INSERT INTO server_manager_config (config_key, config_value)
                VALUES ('password', %s)
            """, (hash_password(new_password),))
        else:
            # 如果存在，更新
            cur.execute("""
                UPDATE server_manager_config
                SET config_value = %s, last_updated = CURRENT_TIMESTAMP
                WHERE config_key = 'password'
            """, (hash_password(new_password),))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "message": "服务器管理密码已更新"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    """管理员登录"""
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        
        if not username or not password:
            return jsonify({"success": False, "message": "用户名和密码不能为空"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 从数据库验证管理员账户
        cur.execute("""
            SELECT admin_id, password_hash
            FROM admin_accounts
            WHERE admin_id = %s
        """, (username,))
        admin = cur.fetchone()
        
        cur.close()
        conn.close()
        
        if admin and admin['password_hash'] == hash_password(password):
            token = generate_token()
            return jsonify({
                "success": True,
                "message": "管理员登录成功",
                "admin_id": username,
                "token": token,
                "has_manager_access": True
            })
        
        return jsonify({"success": False, "message": "管理员登录失败"}), 401
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/admin/account', methods=['POST'])
def create_admin_account():
    """创建管理员账号"""
    try:
        data = request.json
        admin_id = data.get('admin_id')
        password = data.get('password')
        
        if not admin_id or not password:
            return jsonify({"success": False, "message": "管理员ID和密码不能为空"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 检查是否已存在
        cur.execute("SELECT admin_id FROM admin_accounts WHERE admin_id = %s", (admin_id,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "管理员ID已存在"}), 400
        
        # 创建管理员账户
        cur.execute("""
            INSERT INTO admin_accounts (admin_id, password_hash)
            VALUES (%s, %s)
        """, (admin_id, hash_password(password)))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "message": "管理员账号创建成功"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/admin/account/<admin_id>', methods=['PUT'])
def update_admin_account(admin_id):
    """更新管理员账号配置（包括修改密码）"""
    try:
        data = request.json
        new_password = data.get('password')
        
        if not new_password:
            return jsonify({"success": False, "message": "新密码不能为空"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 检查管理员是否存在
        cur.execute("SELECT admin_id FROM admin_accounts WHERE admin_id = %s", (admin_id,))
        if not cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "管理员账户不存在"}), 404
        
        # 更新密码
        cur.execute("""
            UPDATE admin_accounts
            SET password_hash = %s, last_updated = CURRENT_TIMESTAMP
            WHERE admin_id = %s
        """, (hash_password(new_password), admin_id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "message": "管理员密码已更新"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/admin/account/<admin_id>', methods=['GET'])
def get_admin_account(admin_id):
    """获取管理员账户信息（不包括密码）"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT admin_id, created_at, last_updated
            FROM admin_accounts
            WHERE admin_id = %s
        """, (admin_id,))
        admin = cur.fetchone()
        
        cur.close()
        conn.close()
        
        if admin:
            return jsonify({
                "success": True,
                "admin": {
                    "admin_id": admin['admin_id'],
                    "created_at": admin['created_at'].isoformat() if admin['created_at'] else None,
                    "last_updated": admin['last_updated'].isoformat() if admin['last_updated'] else None
                }
            })
        else:
            return jsonify({"success": False, "message": "管理员账户不存在"}), 404
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/admin/account/<admin_id>', methods=['DELETE'])
def delete_admin_account(admin_id):
    """删除管理员账号"""
    try:
        # 不允许删除默认管理员
        if admin_id == 'admin':
            return jsonify({"success": False, "message": "不能删除默认管理员账户"}), 400
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("DELETE FROM admin_accounts WHERE admin_id = %s", (admin_id,))
        
        if cur.rowcount == 0:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "管理员账户不存在"}), 404
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "message": "管理员账号已删除"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/', methods=['GET'])
def index():
    """根路由"""
    return jsonify({
        "service": "AutoSender API Server",
        "status": "running",
        "version": "2.0.0",
        "database": "PostgreSQL",
        "endpoints": {
            "health": "/api/health",
            "register": "/api/register",
            "login": "/api/login",
            "servers": "/api/servers",
            "available-servers": "/api/users/<user_id>/available-servers",
            "server-manager-verify": "/api/server-manager/verify",
            "server-manager-password": "/api/server-manager/password",
            "admin-login": "/api/admin/login",
            "admin-account": "/api/admin/account"
        }
    })

@app.route('/api/health', methods=['GET'])
def health():
    """健康检查"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        return jsonify({"status": "ok", "message": "API服务器和数据库运行正常"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    # 初始化数据库
    try:
        init_database()
        print("✅ 数据库初始化成功")
    except Exception as e:
        print(f"⚠️ 数据库初始化失败: {e}")
        print("   如果数据库已存在，可以忽略此错误")
    
    port = int(os.environ.get('PORT', 5000))
    print("=" * 50)
    print("API服务器启动中...")
    print(f"数据库: PostgreSQL")
    print(f"访问地址: http://0.0.0.0:{port}")
    print("=" * 50)
    
    debug_mode = os.environ.get('FLASK_ENV') == 'development'
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
