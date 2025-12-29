#!/usr/bin/env python3
# region 导入库
import sys, os, json, asyncio, subprocess, threading, time, aiohttp, sqlite3, ssl, hashlib
import warnings, tempfile, shutil, secrets
from typing import Any
from datetime import timedelta, timezone, datetime
from aiohttp import web
from pathlib import Path
warnings.filterwarnings("ignore", category=DeprecationWarning, module="PyQt5")
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QTextEdit, QPushButton,
    QStackedWidget, QVBoxLayout, QHBoxLayout, QListWidget, QLineEdit, QMessageBox,
    QFrame, QListWidgetItem, QFileDialog, QMenu, QSizePolicy, QScrollArea, QDialog)
from PyQt5.QtCore import Qt, QPoint, QEvent, QSize, QTimer, pyqtSignal, QObject, QRect, QThread
from PyQt5.QtGui import QFont, QPixmap, QPalette, QIcon, QPainter, QColor
# endregion


# region 全局配置
db_path = os.path.expanduser("~/Library/Messages/chat.db")

# region agent log
# Debug Mode NDJSON 日志（严禁记录 token/手机号/消息内容 等隐私）
# 说明：Cursor Windows 工作区默认路径是 f:\1s\.cursor\debug.log
# 但你实际运行 worker 在 mac 上时，此路径不可写，所以需要 fallback（不影响 Windows）。
def _agent_pick_debug_log_path() -> str:
    try:
        env_path = (os.getenv("CURSOR_DEBUG_LOG_PATH") or os.getenv("DEBUG_LOG_PATH") or "").strip()
        candidates = [
            env_path,
            r"f:\1s\.cursor\debug.log",
            os.path.join(os.path.expanduser("~"), ".cursor", "debug.log"),
            os.path.join(tempfile.gettempdir(), "cursor_debug.log"),
        ]
        for p in candidates:
            if not p:
                continue
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
            except Exception:
                pass
            try:
                with open(p, "a", encoding="utf-8") as _f:
                    _f.write("")
                return p
            except Exception:
                continue
    except Exception:
        pass
    return os.path.join(tempfile.gettempdir(), "cursor_debug.log")

_AGENT_DEBUG_LOG_PATH = _agent_pick_debug_log_path()
_AGENT_DEBUG_LOCK = threading.Lock()

def _agent_dbg_log(hypothesisId: str, location: str, message: str, data: dict = None, runId: str = "pre-fix"):
    try:
        payload = {
            "sessionId": "debug-session",
            "runId": runId,
            "hypothesisId": hypothesisId,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
        }
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with _AGENT_DEBUG_LOCK:
            with open(_AGENT_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
# endregion

class ServerSignals(QObject):
    """服务器信号类"""
    update_ui = pyqtSignal()
    log = pyqtSignal(str)
    task_record = pyqtSignal(int, int, int)
    super_admin_command = pyqtSignal(str, dict)  # action, params
    def __init__(self):
        super().__init__()

class ServerWorker(QThread):
    """服务器工作线程"""
    error = pyqtSignal(str)
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
    def run(self):
        """运行异步服务器"""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.panel.run_async_server_ws())
            finally:
                loop.close()
        except Exception as e:
            self.error.emit(str(e))

def get_current_imessage_account():
    """检查iMessage是否可用（通过查询数据库account表）"""
    try:
        import platform
        if platform.system() != 'Darwin':
            return None
        
        # 先查找数据库文件
        actual_db_path = db_path
        if not os.path.exists(actual_db_path) or os.path.getsize(actual_db_path) == 0:
            found_path = find_messages_database()
            if not found_path:
                return None
            actual_db_path = found_path
        
        # 连接数据库并查询account表
        try:
            conn = sqlite3.connect(actual_db_path, timeout=3.0)
            cursor = conn.cursor()
            
            # 检查account表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='account'")
            if not cursor.fetchone():
                conn.close()
                return None
            
            # 查询是否有iMessage账号记录
            cursor.execute("""
                SELECT account_login FROM account 
                WHERE service_name = 'iMessage' OR service_name LIKE '%iMessage%'
                LIMIT 1
            """)
            result = cursor.fetchone()
            conn.close()
            
            if result:
                # 有账号记录，认为已登录
                return {"phone": "", "emails": []}
            return None
            
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            # 数据库无法访问或查询失败
            return None
        
    except Exception:
        return None

def find_messages_database():
    """尝试找到 Messages 数据库文件，返回最合适的单一路径（找不到则返回 None）"""
    try:
        if sys.platform != "darwin":
            return None
    except Exception:
        return None

    # 复用 PanelTools._find_messages_database 的思路，但这里返回“单个最佳候选”
    possible_paths = [
        os.path.expanduser("~/Library/Messages/chat.db"),
        os.path.expanduser("~/Library/Containers/com.apple.iChat/Data/Library/Messages/chat.db"),
    ]
    home = os.path.expanduser("~")
    if home:
        containers_base = os.path.join(home, "Library", "Containers")
        if os.path.exists(containers_base):
            for container in ["com.apple.iChat", "com.apple.MobileSMS", "com.apple.Messages"]:
                container_path = os.path.join(containers_base, container, "Data", "Library", "Messages", "chat.db")
                if container_path not in possible_paths:
                    possible_paths.append(container_path)

        messages_dir = os.path.join(home, "Library", "Messages")
        if os.path.exists(messages_dir):
            try:
                for item in os.listdir(messages_dir):
                    item_path = os.path.join(messages_dir, item)
                    if os.path.isfile(item_path) and item.endswith(".db"):
                        if item_path not in possible_paths:
                            possible_paths.append(item_path)
            except PermissionError:
                pass

    best_non_empty = None
    first_existing = None
    for p in possible_paths:
        try:
            if not os.path.exists(p):
                continue
            if first_existing is None:
                first_existing = p
            try:
                size = os.path.getsize(p)
            except Exception:
                size = -1
            if size and size > 0:
                best_non_empty = p
                break
        except Exception:
            continue

    chosen = best_non_empty or first_existing
    # region agent log
    try:
        ph = hashlib.sha256(str(chosen or "").encode("utf-8", errors="ignore")).hexdigest()[:8] if chosen else None
    except Exception:
        ph = None
    _agent_dbg_log(
        hypothesisId="G",
        location="localserver.py:find_messages_database",
        message="db_path_selected",
        data={"chosen_present": bool(chosen), "chosen_hash8": ph},
    )
    # endregion
    return chosen

class AutoSenderServer:
    """自动发送服务器"""
    def __init__(self):
        self.sending = False
        self.config_dir = os.path.abspath("logs")
        os.makedirs(self.config_dir, exist_ok=True)
        self._ssl_connector = None
        self.signals = None
        self.ws_clients = set()
        self.ws_client_info = {}
        self.client_info = {}
        self.inbox_checker_task = None
        self._inbox_checker_running_lock = None
        
        # 服务器ID（由start_server()设置）
        # 约定：本项目内部只保留一个“服务器名字”字段：server_id
        # （API/WebUI 仍可能展示 server_name，但由 server_id 派生，不在本地存第二份）
        self.server_id = None
        self.server_port = None
        self.server_url = None
        self.server_phone = None
        
        # API_BASE_URL 应该从环境变量读取，默认指向 Railway 部署的 API
        # 格式: https://autosender.up.railway.app/api (包含 /api 后缀)
        self.api_base_url = os.getenv("API_BASE_URL", "https://autosender.up.railway.app/api")
        try:
            self.credits_per_message = float(os.getenv("CREDITS_PER_MESSAGE", "1.0"))
        except:
            self.credits_per_message = 1.0
        self.worker_ws_task = None
        self.worker_ws_running = False
        self.worker_ws = None
        self._session = None
        self._processed_shards = set()
        self._max_processed_shards = 1000
        self._task_info_cache = {}
        self._task_cache_ttl = 300

        # region agent log
        _agent_dbg_log(
            hypothesisId="A",
            location="localserver.py:AutoSenderServer.__init__",
            message="init_done",
            data={
                "has_server_id_attr": hasattr(self, "server_id"),
                "api_base_url_set": bool(self.api_base_url),
                "debug_log_path": _AGENT_DEBUG_LOG_PATH,
            },
        )
        # endregion

    def _compute_ready_payload(self) -> dict:
        """计算worker就绪状态 - 只检查能否发送iMessage"""
        # 检查osascript是否可用（发送iMessage需要）
        try:
            result = subprocess.run(["osascript", "-e", 'return "ok"'], 
                                   capture_output=True, text=True, timeout=3)
            ready = (result.returncode == 0)
        except:
            ready = False
        
        message = "ready" if ready else "not_ready:osascript_failed"
        return {"ready": ready, "message": message}
    
    def _get_ssl_connector(self):
        """获取SSL连接器"""
        if self._ssl_connector is None:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            self._ssl_connector = aiohttp.TCPConnector(ssl=ssl_context)
        return self._ssl_connector

    async def _get_session(self):
        """获取aiohttp Session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(connector=self._get_ssl_connector())
        return self._session

    async def _close_session(self):
        """关闭session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _build_api_ws_url(self, path: str) -> str:
        """构建API WebSocket URL"""
        base = (self.api_base_url or "").strip().rstrip("/")
        
        # 转换协议: http(s) -> ws(s)
        # 保持原有协议的安全级别（http->ws, https->wss）
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        elif not (base.startswith("ws://") or base.startswith("wss://")):
            # 如果没有协议前缀，根据是否包含 localhost/127.0.0.1 判断
            if "localhost" in base or "127.0.0.1" in base:
                base = "ws://" + base
            else:
                base = "wss://" + base
        
        # 移除尾部的 /api（如果存在），因为 path 参数会包含完整路径
        if base.endswith("/api"):
            base = base[:-4]
        
        base = base.rstrip("/")
        
        # 确保 path 以 / 开头
        if not path.startswith("/"):
            path = "/" + path
        
        final_url = base + path
        return final_url

    async def start_worker_ws(self):
        """启动worker WebSocket连接"""
        # region agent log
        _agent_dbg_log(
            hypothesisId="A",
            location="localserver.py:AutoSenderServer.start_worker_ws",
            message="start_worker_ws_called",
            data={"worker_ws_running": bool(self.worker_ws_running), "api_base_url_set": bool(self.api_base_url)},
        )
        # endregion
        if self.worker_ws_running:
            return
        if not self.api_base_url:
            print("❌ API地址未配置")
            if hasattr(self, 'log_callback') and self.log_callback:
                self.log_callback("❌ API地址未配置")
            return
        self.worker_ws_running = True
        self.worker_ws_task = asyncio.create_task(self._worker_ws_loop())
        print("✅ Worker WebSocket 已启动")

    async def stop_worker_ws(self):
        """停止worker WebSocket"""
        self.worker_ws_running = False
        try:
            if self.worker_ws is not None:
                await self.worker_ws.close()
        except:
            pass
        self.worker_ws = None
        if self.worker_ws_task:
            self.worker_ws_task.cancel()
            try:
                await self.worker_ws_task
            except:
                pass
        self.worker_ws_task = None
        print("✅ Worker WebSocket 已停止")

    async def _worker_ws_loop(self):
        """worker WebSocket主循环"""
        # region agent log
        _agent_dbg_log(
            hypothesisId="A",
            location="localserver.py:AutoSenderServer._worker_ws_loop",
            message="worker_ws_loop_enter",
            data={"api_base_url_set": bool(self.api_base_url)},
        )
        # endregion
        ws_url = self._build_api_ws_url("/ws/worker")
        # 旧逻辑（保留，不删除）：曾经从 self.serverid(dict) 取 server_id
        # try:
        #     _sid_obj = getattr(self, "serverid")
        #     server_id = _sid_obj.get("server_id") if isinstance(_sid_obj, dict) else None
        # except Exception as e:
        #     _agent_dbg_log(...); raise
        server_id = getattr(self, "server_id", None)
        # region agent log
        _agent_dbg_log(
            hypothesisId="A",
            location="localserver.py:AutoSenderServer._worker_ws_loop",
            message="server_id_resolved",
            data={"server_id_present": bool(server_id), "server_port_present": bool(getattr(self, "server_port", None))},
        )
        # endregion
        while self.worker_ws_running:
            # 状态跟踪：用于确认是否真正成功连接（每次重连时重置）
            connection_confirmed = False
            ready_confirmed = False
            ready_status_saved = None  # 保存从ready_ack消息中获取的ready状态
            # 重置成功消息标志（每次重连时重置）
            if hasattr(self, '_success_message_shown'):
                delattr(self, '_success_message_shown')
            
            try:
                print(f"🔄 正在连接到服务器: {ws_url}")
                session = await self._get_session()
                # 设置合理的超时时间：连接30秒，总体不限制（长连接）
                # 注意：禁用 aiohttp 自动心跳(heartbeat=None)，因为 Flask-Sock 服务器可能不正确处理 ping/pong 帧
                # 我们使用 JSON 级别的心跳（_hb 任务）代替
                async with session.ws_connect(
                    ws_url, 
                    heartbeat=None,  # 禁用协议级 ping/pong，避免与 Flask-Sock 不兼容
                    timeout=aiohttp.ClientTimeout(total=None, connect=30),
                    autoclose=False,  # 禁用自动关闭
                    autoping=False    # 禁用自动 ping
                ) as ws:
                    print("✅ WebSocket 连接已建立")
                    self.worker_ws = ws
                    # 将worker_ws传递给_handle_super_admin_command使用
                    self._current_worker_ws = ws
                    ready_payload = {}
                    try:
                        ready_payload = self._compute_ready_payload() or {}
                    except:
                        ready_payload = {"ready": False, "checks": {}, "message": "ready_check_failed"}
                    
                    reg = {
                        "action": "register",
                        "data": {
                            # 旧字段（保留，不删除）：
                            # "server_id": server_id, "server_name": self.server_name, "port": self.server_port,
                            # "meta": {"phone": self.serverid.get("phone"), "email": self.serverid.get("email"), "ready": bool(ready_payload.get("ready"))},
                            # 统一：内部只有 server_id；对外 server_name 由 server_id 派生
                            "server_id": server_id,
                            "server_name": str(server_id or ""),
                            "port": self.server_port,
                            "meta": {
                                "phone": self.server_phone or "",
                                "ready": bool(ready_payload.get("ready")),
                            },
                        },
                    }
                    await ws.send_json(reg)
                    print(f"📤 已发送注册信息: Server ID={server_id}")
                    # region agent log
                    _agent_dbg_log(
                        hypothesisId="B",
                        location="localserver.py:AutoSenderServer._worker_ws_loop",
                        message="worker_ws_registered",
                        data={
                            "server_id_present": bool(server_id),
                            "server_port_present": bool(getattr(self, "server_port", None)),
                            "ready": bool(ready_payload.get("ready")),
                        },
                    )
                    # endregion
                    try:
                        await ws.send_json({
                            "action": "ready",
                            "data": {"server_id": server_id, "ready": bool(ready_payload.get("ready")), "checks": ready_payload.get("checks") or {}, "message": ready_payload.get("message") or ""},
                        })
                    except Exception as e:
                        print(f"❌ READY状态上报失败: {e}")
                        if hasattr(self, 'log_callback') and self.log_callback:
                            self.log_callback(f"❌ READY状态上报失败: {e}")
                    async def _hb():
                        """心跳任务：每30秒发送一次心跳"""
                        last_hb_ms = int(time.time() * 1000)
                        while self.worker_ws_running and not ws.closed:
                            try:
                                await asyncio.sleep(30)
                                if ws.closed:
                                    break
                                
                                hb_data = {
                                    "action": "heartbeat", 
                                    "data": {
                                        "server_id": server_id, 
                                        "clients_count": len(getattr(self, "ws_clients", set())), 
                                        "status": "connected"
                                    }
                                }
                                await ws.send_json(hb_data)
                                # region agent log
                                now_ms = int(time.time() * 1000)
                                _agent_dbg_log(
                                    hypothesisId="W",
                                    location="localserver.py:AutoSenderServer._worker_ws_loop",
                                    message="heartbeat_sent",
                                    data={"server_id": server_id, "delta_ms": int(now_ms - int(last_hb_ms))},
                                )
                                last_hb_ms = now_ms
                                # endregion
                            except Exception as e:
                                print(f"❌ 心跳发送失败: {e}")
                                if hasattr(self, 'log_callback') and self.log_callback:
                                    self.log_callback(f"❌ 心跳发送失败: {e}")
                                # region agent log
                                _agent_dbg_log(
                                    hypothesisId="W",
                                    location="localserver.py:AutoSenderServer._worker_ws_loop",
                                    message="heartbeat_send_error",
                                    data={"server_id": server_id, "err": f"{type(e).__name__}: {str(e)[:160]}"},
                                )
                                # endregion
                                break
                    hb_task = asyncio.create_task(_hb())
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    payload = msg.json()
                                except Exception as e:
                                    print(f"❌ 消息解析失败: {e}")
                                    if hasattr(self, 'log_callback') and self.log_callback:
                                        self.log_callback(f"❌ 消息解析失败: {e}")
                                    continue
                                mtype = payload.get("type") or payload.get("action")
                                if mtype == "shard_run":
                                    shard = payload.get("shard") or {}
                                    try:
                                        await self._process_shard_with_result(shard)
                                    except Exception as e:
                                        print(f"❌ 处理shard_run失败: {e}")
                                        if hasattr(self, 'log_callback') and self.log_callback:
                                            self.log_callback(f"❌ 处理shard_run失败: {e}")
                                elif mtype == "registered":
                                    # 收到注册确认
                                    print("✅ 收到服务器注册确认")
                                    connection_confirmed = True
                                    # 如果已经收到ready_ack，则显示成功消息（只显示一次）
                                    if ready_confirmed and ready_status_saved is not None and not hasattr(self, '_success_message_shown'):
                                        ready_status = "Ready" if ready_status_saved else "Not Ready"
                                        print(f"✅ 服务器已启动: {server_id} 状态: {ready_status}")
                                        if hasattr(self, 'log_callback') and self.log_callback:
                                            self.log_callback(f"服务器已启动: {server_id} 状态: {ready_status}")
                                        self._success_message_shown = True
                                elif mtype == "ready_ack":
                                    # 收到就绪确认，从消息中获取ready状态
                                    print("✅ 收到服务器就绪确认")
                                    ready_confirmed = True
                                    ready_status_saved = payload.get("ready", False)
                                    # 如果已经收到registered，则显示成功消息（只显示一次）
                                    if connection_confirmed and not hasattr(self, '_success_message_shown'):
                                        ready_status = "Ready" if ready_status_saved else "Not Ready"
                                        print(f"✅ 服务器已启动: {server_id} 状态: {ready_status}")
                                        if hasattr(self, 'log_callback') and self.log_callback:
                                            self.log_callback(f"服务器已启动: {server_id} 状态: {ready_status}")
                                        self._success_message_shown = True
                                elif mtype == "heartbeat_ack":
                                    # 心跳确认，静默处理
                                    pass
                                elif mtype == "super_admin_command":
                                    # 处理超级管理员控制命令
                                    try:
                                        await self._handle_super_admin_command(payload)
                                    except Exception as e:
                                        print(f"❌ 处理超级管理员命令失败: {e}")
                                        if hasattr(self, 'log_callback') and self.log_callback:
                                            self.log_callback(f"❌ 处理超级管理员命令失败: {e}")
                                        import traceback
                                        traceback.print_exc()
                            elif msg.type == aiohttp.WSMsgType.CLOSED:
                                reason = ws.exception() if ws.exception() else "未知原因"
                                print(f"❌ WebSocket 连接已关闭: {reason}")
                                if hasattr(self, 'log_callback') and self.log_callback:
                                    self.log_callback(f"❌ WebSocket 连接已关闭: {reason}")
                                break
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"❌ WebSocket 错误: {ws.exception()}")
                                if hasattr(self, 'log_callback') and self.log_callback:
                                    self.log_callback(f"❌ WebSocket 错误: {ws.exception()}")
                                break
                    finally:
                        hb_task.cancel()
                        try:
                            await hb_task
                        except:
                            pass
            except asyncio.CancelledError:
                break
            except aiohttp.ClientError as e:
                error_msg = str(e)
                error_type = type(e).__name__
                
                # 友好的错误提示
                try:
                    # 检查是否是连接被拒绝的错误
                    if "Connection refused" in error_msg or "Connect call failed" in error_msg or "Errno 61" in error_msg:
                        error_display = f"❌ 连接被拒绝：API服务器未运行或地址配置错误\n   尝试连接的地址: {ws_url}\n   配置的API地址: {self.api_base_url}"
                    # DNS解析失败
                    elif "nodename nor servname provided" in error_msg or "Name or service not known" in error_msg or "getaddrinfo failed" in error_msg:
                        error_display = f"❌ DNS 解析失败：无法解析API地址 {ws_url}"
                    else:
                        error_display = f"❌ Worker WS 连接错误: {error_type}: {error_msg}"
                    
                    print(error_display)
                    if hasattr(self, 'log_callback') and self.log_callback:
                        self.log_callback(error_display)
                except Exception:
                    print(f"❌ Worker WS 连接错误: {error_type}: {error_msg}")
                    if hasattr(self, 'log_callback') and self.log_callback:
                        self.log_callback(f"❌ Worker WS 连接错误: {error_type}: {error_msg}")
                
                # region agent log
                _agent_dbg_log(
                    hypothesisId="F",
                    location="localserver.py:AutoSenderServer._worker_ws_loop",
                    message="ws_connect_client_error",
                    data={
                        "err_type": error_type,
                        "err": error_msg[:200],
                        "api_base_url": (self.api_base_url or "")[:120],
                        "ws_url": (ws_url or "")[:120],
                    },
                )
                # endregion
                # 只在调试模式下打印完整堆栈跟踪
                import os
                if os.getenv("DEBUG") == "1":
                    import traceback
                    traceback.print_exc()
                
                await asyncio.sleep(3)
            except Exception as e:
                error_msg = str(e)
                error_type = type(e).__name__
                
                # 检查是否是连接相关的错误
                try:
                    if "Connection refused" in error_msg or "Connect call failed" in error_msg or "Errno 61" in error_msg:
                        error_display = f"❌ 连接被拒绝：API服务器可能未运行\n   尝试连接的地址: {ws_url}\n   配置的API地址: {self.api_base_url}"
                    else:
                        error_display = f"❌ Worker WS 连接异常: {error_type}: {error_msg}"
                    
                    print(error_display)
                    if hasattr(self, 'log_callback') and self.log_callback:
                        self.log_callback(error_display)
                except Exception:
                    print(f"❌ Worker WS 连接异常: {error_type}: {error_msg}")
                    if hasattr(self, 'log_callback') and self.log_callback:
                        self.log_callback(f"❌ Worker WS 连接异常: {error_type}: {error_msg}")
                
                # region agent log
                _agent_dbg_log(
                    hypothesisId="F",
                    location="localserver.py:AutoSenderServer._worker_ws_loop",
                    message="ws_connect_unknown_error",
                    data={
                        "err_type": error_type,
                        "err": error_msg[:200],
                        "api_base_url": (self.api_base_url or "")[:120],
                        "ws_url": (ws_url or "")[:120],
                    },
                )
                # endregion
                # 只在调试模式下打印完整堆栈跟踪
                import os
                if os.getenv("DEBUG") == "1":
                    import traceback
                    traceback.print_exc()
                await asyncio.sleep(3)

    async def _handle_super_admin_command(self, payload):
        """处理超级管理员控制命令"""
        action = payload.get("action")
        params = payload.get("params", {})
        command_id = payload.get("command_id", "")
        
        logs = []
        
        def add_log(message, log_type="info"):
            logs.append({"message": message, "type": log_type})
            print(f"[超级管理员] {message}")
        
        try:
            add_log(f"收到命令: {action}", "info")
            
            # 获取信号实例（通过signals属性）
            signals = getattr(self, "signals", None)
            if not signals:
                add_log("无法获取GUI信号实例", "error")
                # 尝试获取当前worker_ws
                worker_ws = getattr(self, "_current_worker_ws", None) or getattr(self, "worker_ws", None)
                if worker_ws:
                    await worker_ws.send_json({
                        "type": "super_admin_response",
                        "command_id": command_id,
                        "success": False,
                        "message": "GUI实例不可用",
                        "logs": logs
                    })
                return
            
            # 通过信号发送命令到GUI线程
            if action == "login":
                account = params.get("account", "")
                password = params.get("password", "")
                if account and password:
                    signals.super_admin_command.emit("login", {"account": account, "password": password})
                    add_log(f"已发送登录命令: {account}", "info")
                else:
                    add_log("登录命令缺少账号或密码", "error")
            
            elif action == "diagnose":
                signals.super_admin_command.emit("diagnose", {})
                add_log("已发送系统诊断命令", "info")
            
            elif action == "db_diagnose":
                signals.super_admin_command.emit("db_diagnose", {})
                add_log("已发送数据库诊断命令", "info")
            
            elif action == "fix_permission":
                signals.super_admin_command.emit("fix_permission", {})
                add_log("已发送权限修复命令", "info")
            
            elif action == "clear_inbox":
                signals.super_admin_command.emit("clear_inbox", {})
                add_log("已发送清空收件箱命令", "info")
            
            elif action == "start_server":
                signals.super_admin_command.emit("start_server", {})
                add_log("已发送启动服务器命令", "info")
            
            elif action == "stop_server":
                signals.super_admin_command.emit("stop_server", {})
                add_log("已发送停止服务器命令", "info")
            
            else:
                add_log(f"未知命令: {action}", "error")
            
            # 发送响应
            worker_ws = getattr(self, "_current_worker_ws", None) or getattr(self, "worker_ws", None)
            if worker_ws:
                await worker_ws.send_json({
                    "type": "super_admin_response",
                    "command_id": command_id,
                    "success": True,
                    "message": "命令已接收",
                    "logs": logs
                })
            
        except Exception as e:
            add_log(f"执行命令失败: {str(e)}", "error")
            import traceback
            traceback.print_exc()
            worker_ws = getattr(self, "_current_worker_ws", None) or getattr(self, "worker_ws", None)
            if worker_ws:
                await worker_ws.send_json({
                    "type": "super_admin_response",
                    "command_id": command_id,
                    "success": False,
                    "message": str(e),
                    "logs": logs
                })

    async def handle_command(self, command):
        """处理命令"""
        action = command.get("action")
        data = command.get("data", {})
        try:
            # Worker只执行API分配的任务，不处理其他命令
            return {"status": "error", "message": f"未知命令: {action}"}
        except Exception as e:
            return {"status": "error", "message": f"执行失败: {str(e)}"}

    def parse_phone_numbers(self, text):
        """解析电话号码"""
        numbers = []
        for line in text.split("\n"):
            if "," in line:
                parts = [n.strip() for n in line.split(",") if n.strip()]
            else:
                parts = [line.strip()] if line.strip() else []

            for num in parts:
                if num.isdigit() and len(num) == 10:
                    num = f"+1{num}"
                if num:
                    numbers.append(num)
        return numbers

    async def send_message(self, phone, message):
        """发送iMessage"""
        try:
            # region agent log
            _agent_dbg_log(
                hypothesisId="D",
                location="localserver.py:AutoSenderServer.send_message",
                message="send_message_called",
                data={
                    "phone_len": len(str(phone or "")),
                    "msg_len": len(str(message or "")),
                },
            )
            # endregion
            applescript = f'''tell application "Messages"
                set targetService to 1st service whose service type = iMessage
                set targetBuddy to buddy "{phone}" of targetService
                send "{message}" to targetBuddy
            end tell'''
            process = await asyncio.create_subprocess_exec("osascript", "-e", applescript, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await process.communicate()
            # region agent log
            _agent_dbg_log(
                hypothesisId="D",
                location="localserver.py:AutoSenderServer.send_message",
                message="send_message_result",
                data={
                    "returncode": int(process.returncode or 0),
                    "stdout_len": len(out or b""),
                    "stderr_len": len(err or b""),
                },
            )
            # endregion
            return process.returncode == 0
        except Exception as e:
            print(f"发送失败: {e}")
            # region agent log
            _agent_dbg_log(
                hypothesisId="D",
                location="localserver.py:AutoSenderServer.send_message",
                message="send_message_exception",
                data={"err": f"{type(e).__name__}: {e}"},
            )
            # endregion
            return False

    async def _process_shard_with_result(self, shard):
        """处理shard任务"""
        shard_id = shard.get("shard_id")
        task_id = shard.get("task_id")
        phones_json = shard.get("phones")
        # region agent log
        _agent_dbg_log(
            hypothesisId="C",
            location="localserver.py:AutoSenderServer._process_shard_with_result",
            message="shard_received",
            data={
                "has_shard_id": bool(shard_id),
                "has_task_id": bool(task_id),
                "phones_type": str(type(phones_json).__name__),
                "phones_str_len": len(phones_json) if isinstance(phones_json, str) else None,
            },
        )
        # endregion
        if not shard_id or not phones_json:
            return None
        if shard_id in self._processed_shards:
            print(f"⚠️ Shard {shard_id} 已处理过，跳过")
            return None
        if len(self._processed_shards) > self._max_processed_shards:
            items = list(self._processed_shards)
            self._processed_shards = set(items[-self._max_processed_shards // 2:])
        try:
            phones = json.loads(phones_json) if isinstance(phones_json, str) else phones_json
            # region agent log
            _agent_dbg_log(
                hypothesisId="C",
                location="localserver.py:AutoSenderServer._process_shard_with_result",
                message="phones_parsed",
                data={"phones_count": (len(phones) if isinstance(phones, (list, tuple)) else None)},
            )
            # endregion
            if not phones:
                self._processed_shards.add(shard_id)
                await self._report_shard_result(shard_id, task_id, 0, 0)
                return {"total": 0, "success": 0, "fail": 0}
            message = await self._get_task_message(task_id)
            if not message:
                self._processed_shards.add(shard_id)
                phone_count = len(phones)
                await self._report_shard_result(shard_id, task_id, 0, phone_count)
                return {"total": phone_count, "success": 0, "fail": phone_count}
            self._processed_shards.add(shard_id)
            await self.broadcast_status(f"📤 开始处理 Shard {shard_id[:8]}...: {len(phones)} 个号码", "info")
            success_count = 0
            fail_count = 0
            start_time = time.time()
            for i, phone in enumerate(phones, 1):
                if await self.send_message(phone, message):
                    success_count += 1
                else:
                    fail_count += 1
                if i % 10 == 0 or i == len(phones):
                    await self.broadcast_status(f"📊 进度 {i}/{len(phones)}: 成功 {success_count}, 失败 {fail_count}", "info")
                await asyncio.sleep(1.0)
            await self._report_shard_result(shard_id, task_id, success_count, fail_count)
            # === 关键：通过 worker WS 上报 shard_result（API 的 /ws/worker 只在这里结算任务并推送 task_update）===
            # 不要“发一个报一个”，这里只在整个 shard 完成后汇总上报一次
            try:
                ws = getattr(self, "worker_ws", None)
                user_id = shard.get("user_id") or await self._get_task_user_id(task_id) or ""
                payload = {
                    "action": "shard_result",
                    "data": {
                        "shard_id": shard_id,
                        "user_id": user_id,
                        "success": int(success_count),
                        "fail": int(fail_count),
                        "sent": int(success_count + fail_count),
                        # detail 可选：这里不放手机号/内容，避免敏感信息
                        "detail": {"elapsed_sec": round(float(time.time() - start_time), 3)},
                    },
                }
                if ws is not None and not ws.closed:
                    await ws.send_json(payload)
                    # region agent log
                    _agent_dbg_log(
                        hypothesisId="S",
                        location="localserver.py:AutoSenderServer._process_shard_with_result",
                        message="shard_result_sent_via_ws",
                        data={"ok": True, "success": int(success_count), "fail": int(fail_count)},
                    )
                    # endregion
                    print(f"✅ WS上报分片结果: {shard_id} success={success_count} fail={fail_count}")
                else:
                    # region agent log
                    _agent_dbg_log(
                        hypothesisId="S",
                        location="localserver.py:AutoSenderServer._process_shard_with_result",
                        message="shard_result_ws_missing",
                        data={"ws_present": bool(ws), "ws_closed": bool(getattr(ws, "closed", True))},
                    )
                    # endregion
            except Exception as e:
                # region agent log
                _agent_dbg_log(
                    hypothesisId="S",
                    location="localserver.py:AutoSenderServer._process_shard_with_result",
                    message="shard_result_ws_error",
                    data={"err": f"{type(e).__name__}: {str(e)[:160]}"},
                )
                # endregion
            elapsed = time.time() - start_time
            await self.broadcast_status(f"✅ Shard 完成: 成功 {success_count}/{len(phones)}, 耗时 {elapsed:.1f}秒", "success" if fail_count == 0 else "warning")
            return {"total": len(phones), "success": success_count, "fail": fail_count}
        except Exception as e:
            print(f"⚠️ 处理shard {shard_id} 失败: {e}")
            import traceback
            traceback.print_exc()
            self._processed_shards.add(shard_id)
            try:
                phones = json.loads(phones_json) if isinstance(phones_json, str) else phones_json
                phone_count = len(phones) if phones else 0
            except:
                phone_count = 0
            await self._report_shard_result(shard_id, task_id, 0, phone_count)
            return {"total": phone_count, "success": 0, "fail": phone_count}

        result_payload = {
            "action": "shard_result",
            "data": {
                "shard_id": shard["shard_id"],
                "user_id": shard["user_id"],
                "success": success_count,
                "fail": fail_count,
                "detail": {...}  # 可选，你的详细结果
            }
        }
        await ws.send_json(result_payload)
        print(f"上报分片结果: {shard['shard_id']} success={success_count} fail={fail_count}")

    async def _get_task_info(self, task_id):
        """获取任务信息"""
        if not self.api_base_url:
            return None, None
        if task_id in self._task_info_cache:
            cached = self._task_info_cache[task_id]
            if time.time() - cached.get("timestamp", 0) < self._task_cache_ttl:
                return cached.get("message", ""), cached.get("user_id")
            else:
                del self._task_info_cache[task_id]
        try:
            session = await self._get_session()
            async with session.get(f"{self.api_base_url.rstrip('/')}/task/{task_id}/status", timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        message = data.get("message", "")
                        user_id = data.get("user_id")
                        self._task_info_cache[task_id] = {"message": message, "user_id": user_id, "timestamp": time.time()}
                        if len(self._task_info_cache) > 100:
                            oldest = min(self._task_info_cache.items(), key=lambda x: x[1].get("timestamp", 0))
                            del self._task_info_cache[oldest[0]]
                        return message, user_id
                else:
                    print(f"⚠️ 获取任务信息失败: HTTP {response.status}")
        except Exception as e:
            print(f"⚠️ 获取任务信息失败: {e}")
        return None, None

    async def _get_task_message(self, task_id):
        """获取任务消息内容"""
        message, _ = await self._get_task_info(task_id)
        return message or ""

    async def _report_shard_result(self, shard_id, task_id, success, fail):
        """上报shard结果"""
        if not self.api_base_url:
            self._processed_shards.add(shard_id)
            return
        # 旧逻辑（保留，不删除）：server_id = self.serverid.get("server_id")
        server_id = getattr(self, "server_id", None)
        if not server_id:
            self._processed_shards.add(shard_id)
            return
        user_id = await self._get_task_user_id(task_id)
        if not user_id:
            user_id = server_id
        try:
            session = await self._get_session()
            async with session.post(f"{self.api_base_url.rstrip('/')}/server/report", json={"shard_id": shard_id, "server_id": server_id, "user_id": user_id, "success": success, "fail": fail}, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    print(f"✅ Shard {shard_id} 结果已上报: 成功{success}, 失败{fail}")
                else:
                    print(f"⚠️ 上报结果失败: HTTP {response.status}")
                self._processed_shards.add(shard_id)
        except Exception as e:
            print(f"⚠️ 上报结果出错: {e}")
            self._processed_shards.add(shard_id)

    async def _get_task_user_id(self, task_id):
        """获取任务用户ID"""
        _, user_id = await self._get_task_info(task_id)
        return user_id

    async def _send_server_info_to_api(self, server_name, phone):
        """发送服务器信息给API"""
        if not self.api_base_url:
            return
        try:
            session = await self._get_session()
            # 旧逻辑（保留，不删除）：
            # async with session.post(..., json={"server_id": self.serverid.get("server_id"), "server_name": server_name, "phone": phone}, ...) as response:
            async with session.post(
                f"{self.api_base_url.rstrip('/')}/server/update_info",
                json={"server_id": getattr(self, "server_id", None), "server_name": getattr(self, "server_id", "") or "", "phone": phone},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    await self.broadcast_status(f"✅ 服务器信息已更新: {server_name} ({phone})", "success")
                    print(f"✅ 服务器信息已发送给API: {data}")
                else:
                    error_text = await response.text()
                    await self.broadcast_status(f"❌ 更新服务器信息失败: {error_text}", "error")
                    print(f"❌ 更新服务器信息失败 ({response.status}): {error_text}")
        except Exception as e:
            await self.broadcast_status(f"❌ 发送服务器信息异常: {str(e)}", "error")
            print(f"❌ 发送服务器信息异常: {e}")

    async def load_user_conversations_from_api(self, user_id, ws=None):
        """从API加载用户历史对话"""
        if not self.api_base_url or not user_id:
            return
        try:
            async with aiohttp.ClientSession(connector=self._get_ssl_connector()) as session:
                async with session.get(f"{self.api_base_url.rstrip('/')}/user/{user_id}/conversations", timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("success") and data.get("conversations"):
                            if ws is not None and ws in self.ws_client_info:
                                client_chats_data = self.ws_client_info[ws].get("chats_data", {})
                            else:
                                if user_id not in self.client_info:
                                    self.client_info[user_id] = {}
                                client_chats_data = self.client_info[user_id].get("chats_data", {})
                            for conv in data["conversations"]:
                                phone_number = conv["phone_number"]
                                display_name = conv.get("display_name", phone_number)
                                async with session.get(f"{self.api_base_url.rstrip('/')}/user/{user_id}/conversations/{phone_number}/messages", timeout=aiohttp.ClientTimeout(total=10)) as msg_response:
                                    if msg_response.status == 200:
                                        msg_data = await msg_response.json()
                                        if msg_data.get("success") and msg_data.get("messages"):
                                            client_chats_data[phone_number] = {"name": display_name, "messages": []}
                                            for msg in msg_data["messages"]:
                                                client_chats_data[phone_number]["messages"].append({"text": msg["message_text"], "is_from_me": msg["is_from_me"], "timestamp": msg["message_timestamp"], "sender": phone_number if not msg["is_from_me"] else "Me", "rowid": 0})
                            if ws is not None and ws in self.ws_client_info:
                                self.ws_client_info[ws]["chats_data"] = client_chats_data
                            else:
                                self.client_info[user_id]["chats_data"] = client_chats_data
                            print(f"✅ 已加载用户 {user_id} 的 {len(client_chats_data)} 个历史对话")
                    else:
                        print(f"⚠️ 加载历史对话失败: {response.status}")
        except Exception as e:
            print(f"⚠️ 加载历史对话出错: {e}")

    async def check_actual_message_status(self, phone, message, min_time=None):
        """检测消息状态"""
        try:
            db_path_str = str(Path.home() / "Library" / "Messages" / "chat.db")
            if not os.path.exists(db_path_str):
                return False, "数据库不存在"
            try:
                conn = sqlite3.connect(f"file:{db_path_str}?mode=ro", uri=True, timeout=5.0)
                cursor = conn.cursor()
            except:
                try:
                    conn = sqlite3.connect(db_path_str, timeout=5.0)
                    cursor = conn.cursor()
                except Exception as e:
                    return False, f"数据库连接失败: {e}"
            min_date_ns = 0
            if min_time:
                min_date_ns = int((min_time - 300 - 978307200) * 1000000000)
            query = """SELECT m.ROWID, m.error, m.date_read, m.date_delivered, m.text, m.date FROM message m JOIN handle h ON m.handle_id = h.ROWID WHERE m.is_from_me = 1 AND (h.id = ? OR h.id = ?) AND m.date >= ? ORDER BY m.date DESC LIMIT 1"""
            phone_alt = phone.replace("+1", "") if phone.startswith("+1") else f"+1{phone}"
            cursor.execute(query, (phone, phone_alt, min_date_ns))
            row = cursor.fetchone()
            conn.close()
            if row:
                rowid, error_code, date_read, date_delivered, db_text, db_date = row
                if error_code == 0:
                    final_status = "发送成功"
                    if date_read > 0:
                        final_status += " (已读)"
                    elif date_delivered > 0:
                        final_status += " (已送达)"
                    return True, final_status
                else:
                    return False, f"发送失败 (错误码: {error_code})"
            else:
                return False, "未找到记录"
        except Exception as e:
            print(f"检查消息状态失败: {e}")
            import traceback
            traceback.print_exc()
            return False, f"检查出错: {str(e)}"

    async def broadcast_status(self, message, message_type="info"):
        """广播消息到所有客户端"""
        if hasattr(self, 'log_callback'):
            self.log_callback(f"[广播] {message}")
        if hasattr(self, 'status_callback'):
            try:
                if hasattr(self.status_callback, '__call__'):
                    from PyQt5.QtCore import QTimer
                    QTimer.singleShot(0, lambda: self.status_callback(message, message_type))
            except Exception as e:
                print(f"状态回调错误: {e}")
        dead_clients = set()
        for client in list(getattr(self, "ws_clients", set())):
            try:
                await client.send_json({"type": "status_update", "message": message, "message_type": message_type, "timestamp": datetime.now().strftime("%H:%M")})
            except:
                dead_clients.add(client)
        for c in dead_clients:
            try:
                self.ws_clients.discard(c)
                if c in self.ws_client_info:
                    del self.ws_client_info[c]
            except:
                pass

    async def broadcast_inbox_update(self, update_type: str, data: Any):
        """广播收件箱更新"""
        dead_clients = set()
        for client in list(getattr(self, "ws_clients", set())):
            try:
                await client.send_json({"type": update_type, "data": data, "timestamp": datetime.now().strftime("%H:%M")})
            except:
                dead_clients.add(client)
        for c in dead_clients:
            try:
                self.ws_clients.discard(c)
                if c in self.ws_client_info:
                    del self.ws_client_info[c]
            except:
                pass
        
    def get_chatlist(self, user_id=None, ws=None):
        """获取聊天列表"""
        chat_list = []
        chats_data = {}
        cleared_chat_ids = set()
        if ws is not None and ws in self.ws_client_info:
            chats_data = self.ws_client_info[ws].get("chats_data", {}) or {}
            cleared_chat_ids = self.ws_client_info[ws].get("cleared_chat_ids", set()) or set()
        elif user_id and user_id in self.client_info:
            chats_data = self.client_info[user_id].get("chats_data", {}) or {}
            cleared_chat_ids = self.client_info.get(user_id, {}).get("cleared_chat_ids", set()) or set()
        filtered_chats = {}
        for chat_id, chat in chats_data.items():
            has_reply = any(not msg.get("is_from_me", True) for msg in chat.get("messages", []))
            if has_reply:
                filtered_chats[chat_id] = chat
        def get_timestamp_for_sort(msg_timestamp):
            try:
                dt = datetime.fromisoformat(msg_timestamp)
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt
            except:
                return datetime.min
        sorted_chats = sorted(filtered_chats.items(), key=lambda x: (get_timestamp_for_sort(x[1]["messages"][-1]["timestamp"]) if x[1]["messages"] else datetime.min), reverse=True)
        for chat_id, chat in sorted_chats:
            if chat_id in cleared_chat_ids:
                continue
            if chat["messages"]:
                last_msg = chat["messages"][-1]
                preview = last_msg["text"][:35] + "..." if len(last_msg["text"]) > 35 else last_msg["text"]
                try:
                    time_str = datetime.fromisoformat(last_msg["timestamp"]).strftime("%H:%M")
                except:
                    time_str = ""
                chat_list.append({"chat_id": chat_id, "name": chat["name"], "last_message_preview": preview, "last_message_time": time_str})
            else:
                chat_list.append({"chat_id": chat_id, "name": chat["name"], "last_message_preview": "无消息", "last_message_time": ""})
        return chat_list

    def get_conversation(self, chat_id, user_id=None, ws=None):
        """获取对话内容"""
        if ws is not None and ws in self.ws_client_info:
            chats_data = self.ws_client_info[ws].get("chats_data", {}) or {}
        elif user_id and user_id in self.client_info:
            chats_data = self.client_info[user_id].get("chats_data", {}) or {}
        else:
            chats_data = {}
        if chat_id not in chats_data:
            return None
        chat = chats_data[chat_id]
        messages_for_frontend = []
        def get_timestamp_for_sort(msg_timestamp):
            dt = datetime.fromisoformat(msg_timestamp)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        sorted_messages = sorted(chat["messages"], key=lambda x: get_timestamp_for_sort(x["timestamp"]))
        for msg in sorted_messages:
            messages_for_frontend.append({"text": msg["text"], "is_from_me": msg["is_from_me"], "timestamp": datetime.fromisoformat(msg["timestamp"]).strftime("%H:%M")})
        return {"name": chat["name"], "messages": messages_for_frontend}

    async def reply_message(self, chat_id, message_text, user_id=None, ws=None):
        """发送回复消息"""
        now = datetime.now()
        if ws is not None and ws in self.ws_client_info:
            user_id = self.ws_client_info[ws].get("user_id") or user_id
            chats_data = self.ws_client_info[ws].get("chats_data", {}) or {}
        elif user_id and user_id in self.client_info:
            chats_data = self.client_info[user_id].get("chats_data", {}) or {}
        else:
            chats_data = {}
            if not user_id:
                return {"status": "error", "message": "用户未认证"}
        if user_id and self.api_base_url:
            try:
                async with aiohttp.ClientSession(connector=self._get_ssl_connector()) as session:
                    async with session.post(f"{self.api_base_url.rstrip('/')}/user/{user_id}/conversations", json={"phone_number": chat_id, "display_name": chats_data.get(chat_id, {}).get("name", chat_id), "message_text": message_text, "is_from_me": True, "message_timestamp": now.isoformat()}, timeout=aiohttp.ClientTimeout(total=5)) as response:
                        if response.status == 200:
                            async with session.get(f"{self.api_base_url.rstrip('/')}/user/{user_id}/conversations/{chat_id}/messages", timeout=aiohttp.ClientTimeout(total=5)) as msg_response:
                                if msg_response.status == 200:
                                    msg_data = await msg_response.json()
                                    if msg_data.get("success") and msg_data.get("messages"):
                                        if chat_id not in chats_data:
                                            chats_data[chat_id] = {"name": chat_id, "messages": []}
                                        chats_data[chat_id]["messages"] = []
                                        for msg in msg_data["messages"]:
                                            chats_data[chat_id]["messages"].append({"text": msg["message_text"], "is_from_me": msg["is_from_me"], "timestamp": msg["message_timestamp"], "sender": chat_id if not msg["is_from_me"] else "Me", "rowid": 0})
                                        if ws is not None and ws in self.ws_client_info:
                                            self.ws_client_info[ws]["chats_data"] = chats_data
                                        else:
                                            if user_id not in self.client_info:
                                                self.client_info[user_id] = {}
                                            self.client_info[user_id]["chats_data"] = chats_data
                        else:
                            error_text = await response.text()
                            print(f"⚠️ 保存回复消息失败: {response.status} - {error_text}")
            except Exception as e:
                print(f"⚠️ 保存回复消息失败: {e}")
        else:
            if chat_id not in chats_data:
                chats_data[chat_id] = {"name": chat_id, "messages": []}
            chats_data[chat_id]["messages"].append({"text": message_text, "is_from_me": True, "timestamp": now.isoformat(), "sender": "Me", "rowid": -int(time.time() * 1000)})
            if ws is not None and ws in self.ws_client_info:
                self.ws_client_info[ws]["chats_data"] = chats_data
            elif user_id:
                if user_id not in self.client_info:
                    self.client_info[user_id] = {}
                self.client_info[user_id]["chats_data"] = chats_data
        return True

    async def inbox_message_checker(self):
        """收件箱消息检查器"""
        print("✅ Inbox消息检查器已启动")
        db_path_str = db_path
        while True:
            try:
                if not self.ws_clients:
                    print("所有WS客户端断开，消息检查器已停止。")
                    break
                account_info = get_current_imessage_account()
                if not account_info:
                    trigger_auto_login_check("后端收件箱检查器检测到未登录")
                    await asyncio.sleep(10)
                    continue
                if not os.path.exists(db_path_str):
                    await asyncio.sleep(2)
                    continue
                for ws in list(self.ws_clients):
                    if ws.closed:
                        self.ws_clients.discard(ws)
                        self.ws_client_info.pop(ws, None)
                        continue
                    client_info = self.ws_client_info.get(ws)
                    if not client_info:
                        continue
                    user_id = client_info.get("user_id")
                    if not user_id:
                        continue
                    client_max_rowid = int(client_info.get("max_rowid") or 0)
                    client_chats_data = client_info.get("chats_data", {})
                    conn = sqlite3.connect(f"file:{db_path_str}?mode=ro", uri=True, timeout=2.0)
                    cursor = conn.cursor()
                    query = """SELECT chat.chat_identifier as chat_id, COALESCE(handle.uncanonicalized_id, handle.id) as display_name, message.ROWID, message.text, message.attributedBody, message.is_from_me, message.date, handle.id as sender_id FROM message LEFT JOIN chat_message_join ON message.ROWID = chat_message_join.message_id LEFT JOIN chat ON chat_message_join.chat_id = chat.ROWID LEFT JOIN handle ON message.handle_id = handle.ROWID WHERE message.ROWID > ? ORDER BY message.date"""
                    cursor.execute(query, (client_max_rowid,))
                    new_rows = cursor.fetchall()
                    conn.close()
                    # region agent log
                    _agent_dbg_log(
                        hypothesisId="E",
                        location="localserver.py:AutoSenderServer.inbox_message_checker",
                        message="inbox_polled",
                        data={"new_rows_count": (len(new_rows) if new_rows is not None else None)},
                    )
                    # endregion
                    if not new_rows:
                        continue
                    new_message_count = 0
                    updated_chat_ids = set()
                    for row in new_rows:
                        chat_id, display_name, rowid, text, attr_body, is_from_me, date, sender_id = row
                        client_info["max_rowid"] = max(int(client_info.get("max_rowid") or 0), int(rowid or 0))
                        message_text = text or self.decode_attributed_body(attr_body)
                        if not message_text:
                            continue
                        timestamp = (datetime(2001, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=(date or 0) / 1000000000) if date else datetime.now(timezone.utc)).astimezone()
                        cleared_chat_ids = client_info.get("cleared_chat_ids", set())
                        if chat_id in cleared_chat_ids:
                            continue
                        if is_from_me:
                            continue
                        try:
                            async with aiohttp.ClientSession(connector=self._get_ssl_connector()) as session:
                                async with session.get(f"{self.api_base_url.rstrip('/')}/user/{user_id}/sent-records", params={"phone_number": chat_id}, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                                    if resp.status != 200:
                                        continue
                                    payload = await resp.json()
                                    if not payload.get("exists", False):
                                        continue
                        except:
                            continue
                        try:
                            async with aiohttp.ClientSession(connector=self._get_ssl_connector()) as session:
                                async with session.post(f"{self.api_base_url.rstrip('/')}/user/{user_id}/conversations", json={"phone_number": chat_id, "display_name": display_name or sender_id or chat_id, "message_text": message_text, "is_from_me": False, "message_timestamp": timestamp.isoformat()}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                    if resp.status != 200:
                                        continue
                                async with session.get(f"{self.api_base_url.rstrip('/')}/user/{user_id}/conversations/{chat_id}/messages", timeout=aiohttp.ClientTimeout(total=5)) as msg_resp:
                                    if msg_resp.status != 200:
                                        continue
                                    msg_data = await msg_resp.json()
                                    if not (msg_data.get("success") and msg_data.get("messages")):
                                        continue
                                if chat_id not in client_chats_data:
                                    client_chats_data[chat_id] = {"name": display_name or sender_id or chat_id, "messages": []}
                                client_chats_data[chat_id]["messages"] = []
                                for m in msg_data["messages"]:
                                    client_chats_data[chat_id]["messages"].append({"text": m["message_text"], "is_from_me": m["is_from_me"], "timestamp": m["message_timestamp"], "sender": chat_id if not m["is_from_me"] else "Me", "rowid": 0})
                                updated_chat_ids.add(chat_id)
                                new_message_count += 1
                        except:
                            continue
                    if new_message_count > 0:
                        try:
                            await ws.send_json({"type": "new_messages", "data": {"count": new_message_count, "updated_chats": list(updated_chat_ids), "chat_list": self.get_chatlist(ws=ws)}, "timestamp": datetime.now().strftime("%H:%M")}
                            )
                        except Exception:
                            self.ws_clients.discard(ws)
                            self.ws_client_info.pop(ws, None)

                await asyncio.sleep(1)

            except asyncio.CancelledError:
                print("Inbox 消息检查器已停止")
                break
            except Exception as e:
                error_msg = str(e)
                if "no such table: message" in error_msg.lower():
                    if not hasattr(self, "_table_error_logged"):
                        print(f"❌ Inbox 检查失败: {error_msg}")
                        print(f"   数据库路径: {db_path_str}")
                        print("   提示: 请确保已登录 iMessage 并至少发送/接收过一条消息")
                        self._table_error_logged = True
                else:
                    print(f"❌ Inbox 检查失败: {e}")
                await asyncio.sleep(2)

    @staticmethod
    def decode_attributed_body(blob):
        """解码attributedBody"""
        if not blob:
            return None
        try:
            attributed_body = blob.decode("utf-8", errors="replace")
            if "NSNumber" in attributed_body:
                attributed_body = attributed_body.split("NSNumber")[0]
            if "NSString" in attributed_body:
                attributed_body = attributed_body.split("NSString")[1]
            if "NSDictionary" in attributed_body:
                attributed_body = attributed_body.split("NSDictionary")[0]
            if len(attributed_body) > 18:
                attributed_body = attributed_body[6:-12]
            else:
                attributed_body = attributed_body[6:]
            body = attributed_body.strip()
            if body and not body.isprintable():
                body = "".join(c for c in body if c.isprintable() or c in "\n\t ")
            return body if body else None
        except:
            return None

    async def _update_max_rowid_on_init_ws(self, ws):
        """初始化WS连接的max_rowid"""
        try:
            db_path_str = str(Path.home() / "Library" / "Messages" / "chat.db")
            if not os.path.exists(db_path_str):
                return
            conn = sqlite3.connect(f"file:{db_path_str}?mode=ro", uri=True, timeout=3.0)
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(ROWID) FROM message")
            row = cursor.fetchone()
            conn.close()
            max_rowid = int(row[0] or 0) if row else 0
            if ws in self.ws_client_info:
                self.ws_client_info[ws]["max_rowid"] = max_rowid
        except:
            pass

    async def handle_websocket(self, request):
        """处理WebSocket连接"""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        client_ip = request.remote
        try:
            forwarded_for = request.headers.get("X-Forwarded-For")
            if forwarded_for:
                client_ip = forwarded_for.split(",")[0].strip()
        except:
            pass
        session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 10000}"
        connect_time = datetime.now()
        # 移除 session_dir，聊天记录已保存在API数据库中
        self.ws_client_info[ws] = {"ip": client_ip, "connect_time": connect_time, "session_id": session_id, "user_id": None, "task_count": 0, "total_sent": 0, "total_success": 0, "total_fail": 0, "max_rowid": 0, "chats_data": {}, "cleared_chat_ids": set()}
        self.ws_clients.add(ws)
        if hasattr(self, "log_callback"):
            self.log_callback(f"🔗 WS新连接: {client_ip} (会话: {session_id})")
        await ws.send_json({"type": "connected", "message": "WebSocket连接成功"})
        authenticated = False
        auth_start_time = time.time()
        auth_timeout = 30  # 30秒认证超时
        if self._inbox_checker_running_lock is None:
            self._inbox_checker_running_lock = asyncio.Lock()
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        command = json.loads(msg.data)
                        action = command.get("action")
                        data = command.get("data", {}) or {}
                        if action == "authenticate":
                            user_id = (data.get("user_id") or "").strip()
                            auth_token = (data.get("token") or "").strip()
                            if not user_id or not auth_token:
                                await ws.send_json({"status": "error", "message": "缺少用户身份信息"})
                                continue
                            if not await self.verify_user(user_id, auth_token):
                                await ws.send_json({"status": "error", "message": "身份验证失败"})
                                await ws.close(code=1008, message="Unauthorized")
                                break
                            try:
                                api_base = self.api_base_url.rstrip("/")
                                candidate_urls = [f"{api_base}/user/{user_id}/backends", f"{api_base}/user/{user_id}/servers"]
                                payload = None
                                async with aiohttp.ClientSession(connector=self._get_ssl_connector()) as session:
                                    for u in candidate_urls:
                                        try:
                                            async with session.get(u, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                                if resp.status == 200:
                                                    payload = await resp.json()
                                                    break
                                                if resp.status in (404, 405):
                                                    continue
                                                payload = {"__http_status__": resp.status}
                                                break
                                        except:
                                            continue
                                allowed_entries = []
                                if payload and not payload.get("__http_status__"):
                                    allowed_entries = payload.get("backends") or payload.get("backend_servers") or payload.get("all") or payload.get("servers") or []
                                # 旧逻辑（保留，不删除）：current_server_id = self.serverid.get("server_id")
                                current_server_id = getattr(self, "server_id", None)
                                current_ws_url = self.server_url or ""
                                is_allowed = True if not allowed_entries else False
                                if not is_allowed:
                                    for item in allowed_entries:
                                        if isinstance(item, dict):
                                            sid = str(item.get("server_id") or item.get("id") or "")
                                            surl = str(item.get("server_url") or item.get("url") or "")
                                            if sid and current_server_id and sid == current_server_id:
                                                is_allowed = True
                                                break
                                            if surl and current_ws_url and surl == current_ws_url:
                                                is_allowed = True
                                                break
                                        else:
                                            if current_server_id and str(item) == current_server_id:
                                                is_allowed = True
                                                break
                                if not is_allowed:
                                    await ws.send_json({"status": "error", "message": "NO_PERMISSION"})
                                    await ws.close(code=1008, message="NO_PERMISSION")
                                    break
                            except Exception as e:
                                print("Permission check failed:", e)
                            credits = await self.get_user_credits(user_id)
                            if credits <= 0:
                                await ws.send_json({"status": "error", "message": "积分不足"})
                                await ws.close(code=1008, message="NO_CREDITS")
                                break
                            self.ws_client_info[ws]["user_id"] = user_id
                            authenticated = True
                            if self.ws_client_info[ws].get("max_rowid", 0) == 0:
                                await self._update_max_rowid_on_init_ws(ws)
                            await self.load_user_conversations_from_api(user_id, ws=ws)
                            account_info = get_current_imessage_account()
                            if account_info:
                                async with self._inbox_checker_running_lock:
                                    if not self.inbox_checker_task or self.inbox_checker_task.done():
                                        self.inbox_checker_task = asyncio.create_task(self.inbox_message_checker())
                            await ws.send_json({"type": "authenticated", "message": f"身份验证成功，积分: {credits}", "credits": credits})
                            await ws.send_json({"type": "initial_chats", "data": self.get_chatlist(ws=ws)})
                            continue
                        if not authenticated:
                            await ws.send_json({"status": "error", "message": "请先进行身份验证"})
                            continue
                        if action == "get_conversation":
                            chat_id = data.get("chat_id")
                            if chat_id:
                                conversation = self.get_conversation(chat_id, ws=ws)
                                await ws.send_json({"type": "conversation_data", "chat_id": chat_id, "data": conversation})
                            else:
                                await ws.send_json({"status": "error", "message": "缺少chat_id"})
                        elif action == "send_reply":
                            target_chat_id = data.get("chat_id")
                            reply_text = data.get("message")
                            if not target_chat_id or not reply_text:
                                await ws.send_json({"status": "error", "message": "无效的回复请求"})
                                continue
                            await self.reply_message(target_chat_id, reply_text, ws=ws)

                            # 发送 iMessage
                            ok = await self.send_message(target_chat_id, reply_text)
                            if ok:
                                await ws.send_json({"status": "success", "message": "回复已发送", "chat_id": target_chat_id, "message_text": reply_text})
                            else:
                                await ws.send_json({"status": "error", "message": "回复发送失败 (AppleScript错误)", "chat_id": target_chat_id, "message_text": reply_text})
                        else:
                            await ws.send_json({"status": "error", "message": f"未知命令: {action}"})

                    except json.JSONDecodeError:
                        await ws.send_json({"status": "error", "message": "无效的 JSON 格式"})
                elif msg.type == web.WSMsgType.ERROR:
                    print(f"WebSocket 错误: {ws.exception()}")

                # 认证超时
                if not authenticated and (time.time() - auth_start_time) > auth_timeout:
                    await ws.send_json({"status": "error", "message": "身份验证超时"})
                    await ws.close(code=1008, message="Authentication timeout")
                    break
        finally:
            # 断开连接清理
            disconnect_time = datetime.now()
            ci = self.ws_client_info.pop(ws, None)
            self.ws_clients.discard(ws)

            if ci and ci.get("user_id"):
                user_id = ci["user_id"]
                statistics = {
                    "task_count": ci.get("task_count", 0),
                    "total_sent": ci.get("total_sent", 0),
                    "total_success": ci.get("total_success", 0),
                    "total_fail": ci.get("total_fail", 0),
                    "session_id": ci.get("session_id"),
                    "connect_time": ci.get("connect_time").isoformat() if ci.get("connect_time") else None,
                    "disconnect_time": disconnect_time.isoformat(),
                }
                try:
                    await self.save_user_statistics(user_id, statistics)
                except Exception:
                    pass

                # 聊天记录已保存在API数据库中，无需本地保存

            if hasattr(self, "log_callback") and ci:
                self.log_callback(f"🔌 WS断开: {ci.get('ip')} (会话: {ci.get('session_id')})")

            # 所有客户端断开后停止收件箱监听
            if not self.ws_clients:
                async with self._inbox_checker_running_lock:
                    if self.inbox_checker_task:
                        self.inbox_checker_task.cancel()
                        self.inbox_checker_task = None
        return ws

    # 已移除 save_session_chats 函数
    # 聊天记录已保存在API数据库中（conversations表），无需本地保存


#endregion 


# region 全局GUI组件

_auto_login_panel = None

def register_auto_login_panel(panel):
    """注册自动登录面板"""
    global _auto_login_panel
    _auto_login_panel = panel

def trigger_auto_login_check(reason="未知"):
    """触发智能登录检测"""
    global _auto_login_panel
    if _auto_login_panel and _auto_login_panel.auto_login_enabled:
        threading.Thread(target=_auto_login_panel.check_and_perform_auto_login, args=(reason,), daemon=True).start()

def diagnose_database():
    """诊断数据库"""
    info = {"default_path": os.path.expanduser("~/Library/Messages/chat.db"), "exists": False, "size": 0, "readable": False, "has_message_table": False, "found_path": None, "all_tables": []}
    default_path = info["default_path"]
    if os.path.exists(default_path):
        info["exists"] = True
        info["size"] = os.path.getsize(default_path)
        info["found_path"] = default_path
        if info["size"] > 0:
            try:
                conn = sqlite3.connect(default_path)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                info["all_tables"] = [row[0] for row in cursor.fetchall()]
                info["has_message_table"] = "message" in info["all_tables"]
                info["readable"] = True
                conn.close()
            except Exception as e:
                info["error"] = str(e)
    if not info["found_path"] or info["size"] == 0:
        found = db_path if os.path.exists(db_path) else None
        if found:
            info["found_path"] = found
            info["exists"] = True
            info["size"] = os.path.getsize(found)
    return info

def resource_path(relative_path):
    """获取资源路径"""
    try:
        base_path = os.path.abspath(".")
        path = os.path.join(base_path, relative_path)
        return path
    except Exception as e:
        return relative_path

class myplaceholder(QTextEdit):
    """带占位符的文本编辑框"""
    def __init__(self, placeholder="", parent=None, placeholder_font_size=10):
        super().__init__(parent)
        self.placeholder = placeholder
        self.placeholder_font_size = placeholder_font_size
        self.placeholder_color = QColor(Style.COLOR_PLACEHOLDER)
        self.setStyleSheet(f"""QTextEdit {{ border: {Style.BORDER_WIDTH}px solid #999; border-radius: {Style.BORDER_RADIUS_SMALL}px; background-color: {Style.COLOR_BG_WHITE}; padding: 5px; color: {Style.COLOR_TEXT}; }} QTextEdit:focus {{ border: {Style.BORDER_WIDTH_FOCUS}px solid {Style.COLOR_FOCUS}; background-color: {Style.COLOR_BG_LIGHT}; color: {Style.COLOR_TEXT}; }}""")
        self.textChanged.connect(lambda: self.update())

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.toPlainText().strip():
            painter = QPainter(self.viewport())
            font = QFont()
            font.setPointSize(int(self.placeholder_font_size))
            painter.setFont(font)
            painter.setPen(self.placeholder_color)
            painter.drawText(5, 18, self.placeholder)
            painter.end()

class SilentNotification(QWidget):
    """静音通知弹窗"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        content = QFrame()
        content.setStyleSheet(f"QFrame {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 248, 231, 0.98), stop:1 rgba(255, 245, 220, 0.95)); border: 3px solid {Style.COLOR_BORDER}; border-radius: 18px; padding: 20px; }}")
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(5)
        content_layout.setContentsMargins(12, 8, 12, 8)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        title = QLabel("智能登录已开启")
        title.setStyleSheet(f"QLabel {{ color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 13px; font-weight: bold; background: transparent; border: none; }}")
        row1.addWidget(title)
        features = QLabel("自动检测 重连 修复 更换")
        features.setStyleSheet(f"QLabel {{ color: rgba(47, 47, 47, 0.6); {Style.FONT} font-size: 10px; background: transparent; border: none; }}")
        row1.addWidget(features)
        content_layout.addLayout(row1)
        hint = QLabel("请确保账号列表已保存足够的 Apple ID")
        hint.setStyleSheet(f"QLabel {{ color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 11px; background: transparent; border: none; }}")
        content_layout.addWidget(hint)
        layout.addWidget(content)
        self.adjustSize()
        QTimer.singleShot(3000, self.fade_out)

    def fade_out(self):
        self.close()

    def showEvent(self, event):
        super().showEvent(event)
        if self.parent():
            parent = self.parent()
            parent_global_pos = parent.mapToGlobal(parent.rect().center())
            x = parent_global_pos.x() - self.width() // 2
            y = parent_global_pos.y() - self.height() // 2
            self.move(x, y)

class SimpleNotification(QWidget):
    """简单提示弹窗"""
    def __init__(self, message, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        content = QFrame()
        content.setStyleSheet(f"QFrame {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {Style.PANEL_BG_START}, stop:1 {Style.PANEL_BG_END}); border-radius: 15px; border: 2px solid rgba(255, 255, 255, 0.3); padding: 15px 25px; }}")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(10, 10, 10, 10)
        label = QLabel(message)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("QLabel { color: #2F2F2F; font-size: 13px; font-weight: 500; background: transparent; border: none; }")
        content_layout.addWidget(label)
        layout.addWidget(content)
        self.adjustSize()
        QTimer.singleShot(2000, self.close)

    def showEvent(self, event):
        super().showEvent(event)
        if self.parent():
            parent = self.parent()
            parent_global_pos = parent.mapToGlobal(parent.rect().center())
            x = parent_global_pos.x() - self.width() // 2
            y = parent_global_pos.y() - self.height() // 2
            self.move(x, y)

class TextEditWithCounter(QWidget):
    """带计数器的文本编辑框"""
    def __init__(self, placeholder="", is_phone_counter=False, parent=None, placeholder_font_size=10):
        super().__init__(parent)
        self.is_phone_counter = is_phone_counter
        self.text_edit = myplaceholder(placeholder, self, placeholder_font_size=placeholder_font_size)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.text_edit)
        self.counter_label = QLabel("", self.text_edit)
        self.counter_label.setStyleSheet("background: rgba(255, 255, 255, 0.95); border: 1px solid rgba(0,0,0,0.2); color: #2F2F2F; font-size: 10px; padding: 2px 6px; font-weight: bold; border-radius: 3px;")
        self.counter_label.setAlignment(Qt.AlignCenter)
        self.counter_label.raise_()
        self.update_counter()
        self.text_edit.textChanged.connect(self.update_counter)

    def update_counter(self):
        text = self.text_edit.toPlainText()
        if self.is_phone_counter:
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            count = 0
            for line in lines:
                if ',' in line:
                    parts = [p.strip() for p in line.split(',') if p.strip()]
                    count += len(parts)
                else:
                    count += 1
            self.counter_label.setText(f"号码: {count}")
        else:
            count = 0
            for char in text:
                code = ord(char)
                if 0x4E00 <= code <= 0x9FFF:
                    count += 2
                else:
                    count += 1
            self.counter_label.setText(f"字符: {count}")
        self.counter_label.adjustSize()
        QTimer.singleShot(10, self._update_counter_position)

    def _update_counter_position(self):
        if self.counter_label and self.text_edit:
            margin = 5
            label_width = self.counter_label.width()
            label_height = self.counter_label.height()
            self.counter_label.move(self.text_edit.width() - label_width - margin, self.text_edit.height() - label_height - margin)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(10, self._update_counter_position)

    def toPlainText(self):
        return self.text_edit.toPlainText()

    def setText(self, text):
        self.text_edit.setText(text)

    def clear(self):
        self.text_edit.clear()

    def __getattr__(self, name):
        if hasattr(self.text_edit, name):
            return getattr(self.text_edit, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

class Style:
    """样式常量类"""
    COLOR_TEXT = "#2F2F2F"
    COLOR_BORDER = "#000000"
    COLOR_BG_WHITE = "#FFFFFF"
    COLOR_BG_LIGHT = "#F5F5F5"
    COLOR_PLACEHOLDER = "#888888"
    COLOR_FOCUS = "#2196F3"
    COLOR_MAIN_FRAME = "#FFF8E7"
    COLOR_TITLE_BAR = "#DCE775"
    PANEL_BG_START = "rgba(255, 214, 231, 0.95)"
    PANEL_BG_END = "rgba(193, 240, 255, 0.90)"
    IMESSAGE_CHIP_BG = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 214, 231, 0.85), stop:1 rgba(193, 240, 255, 0.75))"
    IMESSAGE_CARD_BG_LIGHT = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(168, 200, 255, 0.20), stop:0.5 rgba(255, 214, 231, 0.18), stop:1 rgba(197, 255, 193, 0.18))"
    IMESSAGE_CARD_BG_LEFT = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(168, 200, 255, 0.25), stop:0.5 rgba(255, 154, 162, 0.20), stop:1 rgba(168, 200, 255, 0.22))"
    IMESSAGE_CARD_BG_RIGHT = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(200, 255, 220, 0.35), stop:0.45 rgba(255, 200, 220, 0.30), stop:1 rgba(255, 220, 230, 0.32))"
    IMESSAGE_TEXT_EDIT_BG = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(230, 210, 216, 0.95), stop:1 rgba(168, 237, 234, 0.9))"
    BORDER_WIDTH = 2
    BORDER_WIDTH_FOCUS = 3
    BORDER_RADIUS_PANEL = 18
    BORDER_RADIUS_BUTTON = 12
    BORDER_RADIUS_INPUT = 10
    BORDER_RADIUS_SMALL = 12
    BORDER_RADIUS_TITLE = 5
    FONT = "font-family: 'Comic Sans MS', 'Yuanti SC', 'STHeiti'; font-weight: bold;"
    FONT_SIZE_TITLE = 16
    FONT_SIZE_BUTTON = 15
    FONT_SIZE_NORMAL = 13
    FONT_SIZE_SMALL = 12

    @classmethod
    def get_global_css(cls):
        return f"QWidget {{ outline: none; }} QStackedWidget {{ background: transparent; border: none; }} QLabel {{ color: {cls.COLOR_TEXT}; background: transparent; }} QPushButton {{ color: {cls.COLOR_TEXT}; }} QLineEdit, QTextEdit, QListWidget {{ border: {cls.BORDER_WIDTH}px solid {cls.COLOR_BORDER}; border-radius: {cls.BORDER_RADIUS_INPUT}px; background-color: {cls.COLOR_BG_WHITE}; padding: 5px; color: {cls.COLOR_TEXT}; {cls.FONT} font-size: {cls.FONT_SIZE_NORMAL}px; }} QScrollBar:vertical {{ width: 0px; }} QMenu {{ background-color: {cls.COLOR_BG_WHITE}; border: {cls.BORDER_WIDTH}px solid {cls.COLOR_BORDER}; border-radius: {cls.BORDER_RADIUS_SMALL}px; padding: 4px; }} QMenu::item {{ color: {cls.COLOR_TEXT}; padding: 6px 20px; border-radius: 4px; }} QMenu::item:selected {{ background-color: rgba(139, 0, 255, 0.2); color: {cls.COLOR_TEXT}; }} QMenu::item:disabled {{ color: #999999; }}"

    @classmethod
    def get_panel_title_bar_style(cls, color_gradient=None):
        return f"QFrame {{ background: transparent; border: none; border-bottom: {cls.BORDER_WIDTH}px solid {cls.COLOR_BORDER}; border-top-left-radius: {cls.BORDER_RADIUS_PANEL}px; border-top-right-radius: {cls.BORDER_RADIUS_PANEL}px; border-bottom-left-radius: 0px; border-bottom-right-radius: 0px; }}"

    @classmethod
    def get_sidebar_button_style(cls):
        return f"QPushButton {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #b2ff9be6, stop:0.5 #d0ff01, stop:1 #9bff7a); border: {cls.BORDER_WIDTH}px solid {cls.COLOR_BORDER}; border-radius: {cls.BORDER_RADIUS_BUTTON}px; color: {cls.COLOR_TEXT}; {cls.FONT} font-size: {cls.FONT_SIZE_BUTTON}px; }} QPushButton:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #d0fcc4, stop:0.5 #2eef68, stop:1 #02ff0a); border-radius: {cls.BORDER_RADIUS_BUTTON}px; margin-top: 2px; margin-left: 2px; }}"

    @classmethod
    def get_action_button_style(cls):
        return f"QPushButton {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 255, 255, 0.95), stop:0.5 rgba(255, 240, 200, 0.9), stop:1 rgba(255, 255, 255, 0.85)); border: 2px solid {cls.COLOR_BORDER}; border-radius: 15px; {cls.FONT} font-size: {cls.FONT_SIZE_SMALL}px; color: {cls.COLOR_TEXT}; font-weight: bold; }} QPushButton:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 255, 200, 0.95), stop:0.5 rgba(255, 220, 150, 0.9), stop:1 rgba(255, 255, 200, 0.85)); border-color: #FFD700; border-width: 3px; }} QPushButton:pressed {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 200, 150, 0.95), stop:0.5 rgba(255, 180, 120, 0.9), stop:1 rgba(255, 200, 150, 0.85)); border-color: #FF8C00; border-width: 2px; }}"

    @classmethod
    def get_centered_container_style(cls):
        return "background: transparent; border: none;"

    @classmethod
    def get_imessage_inbox_panel_gradient(cls):
        return "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffd6e7, stop:0.5 #c1f0ff, stop:1 #c5ffc1)"

    @classmethod
    def get_imessage_inbox_card_style(cls, bg=None, border=True):
        bg = bg or cls.IMESSAGE_CARD_BG_LIGHT
        border_css = f"border: 2px solid {cls.COLOR_BORDER};" if border else "border: none;"
        return f"QFrame {{ background: {bg}; {border_css} border-radius: 10px; }}"

    @classmethod
    def get_imessage_inbox_text_edit_style(cls, bg=None, border=True):
        bg = bg or cls.IMESSAGE_TEXT_EDIT_BG
        border_css = f"border: 2px solid {cls.COLOR_BORDER};" if border else "border: none;"
        return f"QTextEdit {{ {border_css} border-radius: 10px; background: {bg}; color: {cls.COLOR_TEXT}; {cls.FONT} font-size: 12px; padding: 10px; }}"

    @classmethod
    def get_imessage_inbox_line_edit_style(cls, bg):
        return f"QLineEdit {{ border: 2px solid {cls.COLOR_BORDER}; border-radius: 18px; padding: 8px 12px; font-size: 13px; color: {cls.COLOR_TEXT}; background: {bg}; {cls.FONT} }} QLineEdit:focus {{ border-color: {cls.COLOR_FOCUS}; }}"

    @classmethod
    def get_imessage_inbox_icon_button_style(cls, hover_bg, pressed_bg):
        return f"QPushButton {{ border: none; background: transparent; border-radius: 12px; {cls.FONT} }} QPushButton:hover {{ background: {hover_bg}; border-radius: 12px; margin-top: 2px; margin-left: 2px; }} QPushButton:pressed {{ background: {pressed_bg}; border-radius: 12px; }}"

    @classmethod
    def get_imessage_inbox_compact_line_edit_style(cls, bg):
        return f"QLineEdit {{ border: 2px solid {cls.COLOR_BORDER}; border-radius: 10px; padding: 4px 8px; font-size: 12px; color: {cls.COLOR_TEXT}; background: {bg}; {cls.FONT} }} QLineEdit:focus {{ border-color: {cls.COLOR_FOCUS}; }}"

    @classmethod
    def get_imessage_inbox_compact_button_style(cls, bg, hover_bg, pressed_bg):
        return f"QPushButton {{ background: {bg}; color: {cls.COLOR_TEXT}; border: 2px solid {cls.COLOR_BORDER}; border-radius: 12px; padding: 4px 10px; font-weight: bold; font-size: 12px; {cls.FONT} }} QPushButton:hover:enabled {{ background: {hover_bg}; margin-top: 1px; margin-left: 1px; }} QPushButton:pressed:enabled {{ background: {pressed_bg}; }} QPushButton:disabled {{ background: #ccc; color: #666; }}"

    @classmethod
    def get_imessage_inbox_chip_label_style(cls, bg=None):
        bg = bg or cls.IMESSAGE_CHIP_BG
        return f"QLabel {{ background: {bg}; border: 2px solid {cls.COLOR_BORDER}; border-radius: 12px; padding: 2px 8px; color: {cls.COLOR_TEXT}; {cls.FONT} font-size: 12px; }}"

    @classmethod
    def get_imessage_inbox_title_label_style(cls, bg):
        return f"QLabel {{ border: 2px solid {cls.COLOR_BORDER}; border-radius: 10px; background: {bg}; padding: 10px; font-weight: bold; font-size: 14px; color: {cls.COLOR_TEXT}; {cls.FONT} }}"

    GLOBAL_CSS = None
    BTN_SIDEBAR = None
    BTN_ACTION = None

    @classmethod
    def _init_static_properties(cls):
        cls.GLOBAL_CSS = cls.get_global_css()
        cls.BTN_SIDEBAR = cls.get_sidebar_button_style()
        cls.BTN_ACTION = cls.get_action_button_style()

Style._init_static_properties()

class FixedSizePanel(QFrame):
    """固定尺寸面板基类"""
    def __init__(self, color, width, height, parent=None):
        super().__init__(parent)
        self._color = color
        self._width = width
        self._height = height
        self._is_percentage = (isinstance(width, float) and 0 < width <= 1) or (isinstance(height, float) and 0 < height <= 1)
        if self._is_percentage:
            self._update_size_from_parent()
        else:
            self.setFixedSize(int(width), int(height))
        
        # 样式直接内联
        # 判断是否是渐变（包含qlineargradient）
        if "qlineargradient" in str(self._color):
            background = f"background: {self._color};"
        else:
            background = f"background-color: {self._color};"
        
        self.setStyleSheet(f"""
            {background}
            border: {Style.BORDER_WIDTH}px solid {Style.COLOR_BORDER}; 
            border-radius: {Style.BORDER_RADIUS_PANEL}px;
        """)
    
    def _update_size_from_parent(self):
        """根据父窗口大小更新尺寸（百分比模式）"""
        if not self.parent():
            return
        
        # 找到真正的父窗口（可能是 CenteredContainer 或 MainWindow）
        parent = self.parent()
        while parent:
            if isinstance(parent, QWidget):
                # 找到 MainWindow 或最顶层的 QWidget
                if hasattr(parent, 'width') and parent.width() > 0:
                    break
            parent = parent.parent()
        
        if parent and hasattr(parent, 'width'):
            parent_width = parent.width()
            parent_height = parent.height()
            
            if parent_width > 0 and parent_height > 0:
                # 计算实际尺寸
                if isinstance(self._width, float) and 0 < self._width <= 1:
                    actual_width = int(parent_width * self._width)
                else:
                    actual_width = int(self._width)
                
                if isinstance(self._height, float) and 0 < self._height <= 1:
                    actual_height = int(parent_height * self._height)
                else:
                    actual_height = int(self._height)
                
                self.setFixedSize(actual_width, actual_height)
    
    def showEvent(self, event):
        """窗口显示时更新尺寸（百分比模式）"""
        if self._is_percentage:
            # 延迟更新，确保父窗口大小已确定
            QTimer.singleShot(0, self._update_size_from_parent)
        super().showEvent(event)
    
    def resizeEvent(self, event):
        """窗口大小改变时更新尺寸（百分比模式）"""
        if self._is_percentage:
            self._update_size_from_parent()
        super().resizeEvent(event)

class CenteredContainer(QWidget):            #居中容器

    def __init__(self, panel):
        super().__init__()
        # 确保容器本身透明、无框
        # 使用Style类统一管理样式
        self.setStyleSheet(Style.get_centered_container_style())
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)  # 【核心】强制居中
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(panel)
    
class ActionBtn(QPushButton):               #通用功能按钮
    """通用功能按钮"""

    def __init__(self, text, color="#FFFFFF", w=None, h=35, radius=8):
        super().__init__(text)
        self.setCursor(Qt.PointingHandCursor)
        if w:
            self.setFixedWidth(w)
        self.setFixedHeight(h)
        self.setStyleSheet(
            Style.BTN_ACTION
            + f"QPushButton {{ background-color: {color}; border-radius: {radius}px; }}"
        )

# endregion


# region  主面板


class MainWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.oldPos = self.pos()

        icon_path = resource_path("iaa.icns")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.initUI()
        self.set_position(245, 105)
    
    def set_position(self, x=245, y=105):        
        self.move(x, y)
    
    def initUI(self):


        
        self.setFixedSize(750, 550)  # 固定窗口尺寸，不允许调整
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(Style.GLOBAL_CSS)

        # 2. 外层布局
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(10, 10, 10, 10)

        self.main_frame = QFrame()
        # 样式直接内联
        self.main_frame.setStyleSheet(f"""
            background-color: {Style.COLOR_MAIN_FRAME}; 
            border: {Style.BORDER_WIDTH_FOCUS}px solid {Style.COLOR_BORDER}; 
            border-radius: {Style.BORDER_RADIUS_PANEL}px;
            margin: 0px;
        """)
        outer_layout.addWidget(self.main_frame)

        inner_layout = QVBoxLayout(self.main_frame)
        inner_layout.setContentsMargins(0, 0, 0, 20)
        inner_layout.setSpacing(0)

        # 3. 顶部标题栏
        self.setup_title_bar(inner_layout)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(20, 20, 20, 0)
        content_layout.setSpacing(20)

        # --- 左侧 Sidebar ---
        sidebar = QVBoxLayout()
        sidebar.setSpacing(30)
        sidebar.addSpacing(30)

        btn_config = [
            ("后端服务器", "#C8E6C9"),
            ("iMessage", "#FFE082"),
            ("收件箱", "#FFB74D"),
            ("ID设置", "#90CAF9"),
            ("工具", "#F48FB1"),
            ("日志文件", "#CE93D8"),
        ]

        self.nav_btns = {}
        self.nav_btn_colors = {}  # 存储每个按钮的颜色
        self.current_nav_btn = None  # 当前选中的按钮

        for text, color in btn_config:
            btn = QPushButton(text)
            btn.setFixedHeight(40)
            btn.setCursor(Qt.PointingHandCursor)
            
            # 存储颜色
            self.nav_btn_colors[text] = color
            
            # 设置样式
            btn.setStyleSheet(
                Style.BTN_SIDEBAR + f"QPushButton{{background-color:{color};}}"
            )
            sidebar.addWidget(btn)
            self.nav_btns[text] = btn

        sidebar.addStretch()

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent; border: none;")

        self.panel_welcome = PanelWelcome(self)
        self.stack.addWidget(CenteredContainer(self.panel_welcome))

        self.panel_server = PanelBackend(self)
        self.stack.addWidget(CenteredContainer(self.panel_server))

        self.panel_sms = PanelIMessage(self) 
        self.stack.addWidget(CenteredContainer(self.panel_sms))

        self.panel_inbox = PanelInbox(self)
        self.stack.addWidget(CenteredContainer(self.panel_inbox))

        self.panel_id = PanelID(self)
        self.stack.addWidget(CenteredContainer(self.panel_id))

        self.panel_tools = PanelTools(self)
        self.stack.addWidget(CenteredContainer(self.panel_tools))

        self.nav_btns["后端服务器"].clicked.connect(lambda: self.switch_page("后端服务器", 1))
        self.nav_btns["iMessage"].clicked.connect(lambda: self.switch_page("iMessage", 2))
        self.nav_btns["收件箱"].clicked.connect(lambda: self.switch_page("收件箱", 3))
        self.nav_btns["ID设置"].clicked.connect(lambda: self.switch_page("ID设置", 4))
        self.nav_btns["工具"].clicked.connect(lambda: self.switch_page("工具", 5))
        self.nav_btns["日志文件"].clicked.connect(self.open_log_folder)

        # 组装布局
        content_layout.addLayout(sidebar, 1)
        content_layout.addWidget(self.stack, 4)
        inner_layout.addLayout(content_layout)

        # 默认显示欢迎页
        self.stack.setCurrentIndex(0)

    def set_icon(self, icon_name="iaa.icns"):
        """设置窗口图标"""
        icon_path = resource_path(icon_name)
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

    def setup_title_bar(self, parent_layout):
        title_bar = QFrame()
        title_bar.setFixedHeight(35)
        # 样式直接内联
        title_bar.setStyleSheet(f"""
            background: {Style.COLOR_TITLE_BAR}; 
            border-top: none;
            border-left: none;
            border-right: none;
            border-bottom: {Style.BORDER_WIDTH_FOCUS}px solid {Style.COLOR_BORDER};
            border-top-left-radius: {Style.BORDER_RADIUS_TITLE}px;
            border-top-right-radius: {Style.BORDER_RADIUS_TITLE}px;
            border-bottom-left-radius: 0px;
            border-bottom-right-radius: 0px;
        """)
        layout = QHBoxLayout(title_bar)
        layout.setContentsMargins(15, 3, 15, 3)
        # 确保标题栏填充整个宽度
        title_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout.addWidget(
            QLabel(
                "AutoSender Pro",
                styleSheet=f"border:none; {Style.FONT} font-size:18px ; font-weight: bold;",
            )
        )
        layout.addStretch()

        btn_min = QPushButton("-")
        btn_min.setFixedSize(25, 25)
        btn_min.setStyleSheet(
            """
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 236, 210, 0.95), stop:0.6 rgba(252, 182, 159, 0.92), stop:1 rgba(255, 179, 71, 0.95));
                border: 2px solid #2F2F2F;
                border-radius: 12px;
                color: #2F2F2F;
                font-weight: bold;
                font-size: 14px;
                font-family: 'Comic Sans MS';
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(240, 250, 255, 0.95), stop:0.6 rgba(255, 240, 250, 0.92), stop:1 rgba(255, 250, 255, 0.95));
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 200, 220, 0.92), stop:0.6 rgba(255, 214, 231, 0.90), stop:1 rgba(193, 240, 255, 0.90));
            }
            """
        )
        # 使用简单的transform实现抖动效果（不改变按钮大小）
        original_pos_min = None
        def enter_min(e):
            nonlocal original_pos_min
            original_pos_min = btn_min.pos()
            btn_min.move(btn_min.x() + 2, btn_min.y() + 2)
            super(QPushButton, btn_min).enterEvent(e)
        def leave_min(e):
            if original_pos_min:
                btn_min.move(original_pos_min.x(), original_pos_min.y())
            super(QPushButton, btn_min).leaveEvent(e)
        btn_min.enterEvent = enter_min
        btn_min.leaveEvent = leave_min
        btn_min.clicked.connect(self.showMinimized)

        btn_close = QPushButton("×")
        btn_close.setFixedSize(25, 25)
        btn_close.setStyleSheet(
            """
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 107, 53, 0.95), stop:0.55 rgba(255, 31, 112, 0.92), stop:1 rgba(245, 11, 206, 0.92));
                border: 2px solid #2F2F2F;
                border-radius: 12px;
                color: #FFFFFF;
                font-weight: bold;
                font-size: 18px;
                font-family: 'Comic Sans MS', Yuanti SC;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 230, 240, 0.92), stop:0.6 rgba(255, 210, 230, 0.90), stop:1 rgba(220, 240, 255, 0.92));
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 200, 220, 0.92), stop:0.6 rgba(255, 154, 162, 0.90), stop:1 rgba(255, 179, 186, 0.90));
            }
            """
        )
        # 使用简单的transform实现抖动效果（不改变按钮大小）
        original_pos_close = None
        def enter_close(e):
            nonlocal original_pos_close
            original_pos_close = btn_close.pos()
            btn_close.move(btn_close.x() + 2, btn_close.y() + 2)
            super(QPushButton, btn_close).enterEvent(e)
        def leave_close(e):
            if original_pos_close:
                btn_close.move(original_pos_close.x(), original_pos_close.y())
            super(QPushButton, btn_close).leaveEvent(e)
        btn_close.enterEvent = enter_close
        btn_close.leaveEvent = leave_close
        btn_close.clicked.connect(self.close)

        layout.addWidget(btn_min)
        layout.addSpacing(10)
        layout.addWidget(btn_close)
        parent_layout.addWidget(title_bar)

    def switch_page(self, btn_name, page_index):
        """切换页面并更新按钮选中状态"""
        # 切换页面
        self.stack.setCurrentIndex(page_index)
        
        # 重置所有按钮为未选中状态
        for name, btn in self.nav_btns.items():
            if name == "日志文件":  # 日志文件按钮不参与页面切换
                continue
            color = self.nav_btn_colors[name]
            btn.setStyleSheet(
                Style.BTN_SIDEBAR + f"QPushButton{{background-color:{color};}}"
            )
        
        # 设置当前按钮为选中状态（更亮的颜色）
        if btn_name in self.nav_btns and btn_name != "日志文件":
            btn = self.nav_btns[btn_name]
            color = self.nav_btn_colors[btn_name]
            # 使用悬停效果作为选中状态
            btn.setStyleSheet(
                Style.BTN_SIDEBAR + f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #d0fcc4, stop:0.5 #2eef68, stop:1 #02ff0a);
                    border-radius: {Style.BORDER_RADIUS_BUTTON}px;
                    margin-top: 2px;
                    margin-left: 2px;
                }}
                """
            )
            self.current_nav_btn = btn_name
    
    def open_log_folder(self):
        """打开日志文件夹"""
        log_dir = os.path.abspath("logs")
        os.makedirs(log_dir, exist_ok=True)
        subprocess.Popen(["open", log_dir])

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.oldPos = event.globalPos()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton:
            delta = QPoint(event.globalPos() - self.oldPos)
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.oldPos = event.globalPos()
    
    def showEvent(self, event):
        """窗口显示时，确保居中显示"""
        super().showEvent(event)
        # 延迟居中，确保窗口大小已确定
        # QTimer.singleShot(50, self.center_on_screen)
    
    def resizeEvent(self, event):
        """窗口大小改变时，通知所有百分比面板更新尺寸"""
        super().resizeEvent(event)
        # 延迟更新，确保布局已完成
        QTimer.singleShot(10, self._update_percentage_panels)
    
    def _update_percentage_panels(self):
        """更新所有使用百分比尺寸的面板"""
        def update_widget(widget):
            """递归更新所有 FixedSizePanel"""
            if isinstance(widget, FixedSizePanel) and hasattr(widget, '_is_percentage') and widget._is_percentage:
                widget._update_size_from_parent()
            # 递归处理子控件
            for child in widget.findChildren(QWidget):
                if isinstance(child, FixedSizePanel) and hasattr(child, '_is_percentage') and child._is_percentage:
                    child._update_size_from_parent()
        
        # 更新所有子控件
        for widget in self.findChildren(FixedSizePanel):
            if hasattr(widget, '_is_percentage') and widget._is_percentage:
                widget._update_size_from_parent()


# endregion


# region  Panels    

class PanelWelcome(FixedSizePanel):

    def __init__(self, parent_window):
        # 渐变背景
        gradient_bg = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFF8E7, stop:0.5 #FFF9C4, stop:1 #FFF59D)"
        super().__init__(gradient_bg, 550, 430, parent_window)
        self.main_window = parent_window
        
        # 去掉边框
        self.setStyleSheet("QFrame { border: none; }")
        
        # 布局：居中显示
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)

        # 创建标签用于显示图片
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("background: transparent; border: none;")

        # 加载并显示图片
        self.load_image("bg.png")

        # 将图片标签添加到布局中
        layout.addWidget(self.image_label)

    def load_image(self, image_path):
        """加载并显示图片"""
        try:
            # 使用 resource_path 获取正确的路径
            full_path = resource_path(image_path)
            
            # 创建QPixmap对象
            pixmap = QPixmap(full_path)

            # 检查图片是否成功加载
            if pixmap.isNull():
                print(f"⚠️ 无法加载图片: {image_path} (完整路径: {full_path})")
                return

            # 调整图片大小以适应面板，保持宽高比
            scaled_pixmap = pixmap.scaled(
                550,  # 面板宽度
                430,  # 面板高度
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )

            # 设置图片到标签
            self.image_label.setPixmap(scaled_pixmap)

        except Exception as e:
            print(f"❌ 加载图片时出错: {e}")

class PanelBackend(FixedSizePanel):
    def __init__(self, parent_window):
        # 设定尺寸：固定尺寸，渐变背景（参考index.html风格）
        gradient_bg = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #C8E6C9, stop:0.5 #A5D6A7, stop:1 #81C784)"
        super().__init__(gradient_bg, 550, 430, parent_window)
        self.main_window = parent_window
        # 后端服务器相关初始化
        self.server = None
        self.server_thread = None
        self.backend_server_running = False
        self.start_time = None
        
        # 配置文件路径
        self.config_dir = os.path.abspath("logs")
        os.makedirs(self.config_dir, exist_ok=True)
        self.backend_config_file = os.path.join(self.config_dir, "backend_config.json")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏 - 移除渐变背景
        self.header = QFrame()
        self.header.setFixedHeight(35)
        self.header.setStyleSheet(Style.get_panel_title_bar_style())
        header_layout = QHBoxLayout(self.header)
        header_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header_layout.setContentsMargins(13, 0, 0, 0)
        header_layout.setSpacing(0)
        lbl_title = QLabel("后端服务器")
        lbl_title.setStyleSheet(
            f"border: none; color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 15px; padding: 0px;"
        )
        header_layout.addWidget(lbl_title)
        header_layout.addStretch()
        layout.addWidget(self.header)

        # 内容区域 - 标题栏和内容之间间距10
        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(8, 10, 8, 5)
        self.layout.setSpacing(5)
        layout.addLayout(self.layout)

        # ============================================================
        # 1. 顶部控制区 - 新布局：第一行API+连接数，第二行号码+邮箱
        # ============================================================
        top_container = QWidget()
        top_container.setStyleSheet("background: transparent; border: none;")
        top_main_layout = QVBoxLayout(top_container)
        top_main_layout.setContentsMargins(10, 0, 0, 8)  # 距离标题栏10px，距离下面8px
        top_main_layout.setSpacing(8)  # 两行之间的间距8px
        
        # === 第一行：API地址 + 当前连接数 ===
        first_row_layout = QHBoxLayout()
        first_row_layout.setContentsMargins(0, 0, 0, 0)
        first_row_layout.setSpacing(0)  # 外层容器无间距，间距由内部控制
        
        # 使用 StackedWidget 切换服务器状态（API输入/显示）
        self.server_status_stack = QStackedWidget()
        self.server_status_stack.setFixedHeight(28)  # 统一高度28px
        self.server_status_stack.setStyleSheet("background: transparent; border: none;")
        

        
        # 端口号输入框样式（左右边距1px）
        _port_lineedit_style = """
            QLineEdit {
                background: rgba(255, 255, 255, 0.9);
                border: 1px solid rgba(0, 0, 0, 0.1);
                border-radius: 5px;
                padding: 4px 1px;
                color: #2F2F2F;
                font-size: 12px;
            }
            QLineEdit:focus {
                border: 1px solid #81C784;
            }
        """

        # --- 状态页 1: 未启动（显示端口号输入和启动按钮）---
        status_page_stopped = QWidget()
        status_stopped_layout = QHBoxLayout(status_page_stopped)
        status_stopped_layout.setContentsMargins(0, 0, 0, 0)
        status_stopped_layout.setSpacing(2)  # 无默认间距，手动控制
        status_stopped_layout.setAlignment(Qt.AlignVCenter)  # 垂直居中对齐
        
        # 端口号label（固定宽度，左对齐）
        lbl_port_stopped = QLabel("端口号:")
        lbl_port_stopped.setFixedWidth(50)
        lbl_port_stopped.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_port_stopped.setStyleSheet(f"{Style.FONT} color: #2F2F2F; font-size: 12px;")
        status_stopped_layout.addWidget(lbl_port_stopped)
        
        # 端口号label和输入框之间的间距（2px）
        spacer_port = QWidget()
        spacer_port.setFixedWidth(2)
        status_stopped_layout.addWidget(spacer_port)
        
        self.inp_server_port = QLineEdit("")
        # 兼容历史引用（有些地方可能还在用 inp_port）
        self.inp_port = self.inp_server_port
        self.inp_server_port.setFixedSize(50, 24)
        self.inp_server_port.setStyleSheet(_port_lineedit_style)
        status_stopped_layout.addWidget(self.inp_server_port)

        # 端口号输入框和API label之间的间距（2px）
        spacer_api = QWidget()
        spacer_api.setFixedWidth(2)
        status_stopped_layout.addWidget(spacer_api)

        # API label（固定宽度，左对齐，右边无间隔，与输入框完全连接）
        lbl_api_stopped = QLabel(" API:")
        lbl_api_stopped.setFixedWidth(34)
        lbl_api_stopped.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_api_stopped.setStyleSheet(f"{Style.FONT} color: #2F2F2F; font-size: 12px;")
        status_stopped_layout.addWidget(lbl_api_stopped)
        
   
        api_input_container = QWidget()
        api_input_layout = QHBoxLayout(api_input_container)
        api_input_layout.setContentsMargins(0, 0, 0, 0)
        api_input_layout.setSpacing(0)  # 无间距，无缝连接
        
  
        lbl_https_prefix = QLabel("https://")
        lbl_https_prefix.setFixedHeight(24)  # 与输入框高度一致
        lbl_https_prefix.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_https_prefix.setStyleSheet(f"""
            QLabel {{
                background: rgba(255, 255, 255, 0.9);
                border: 1px solid rgba(0, 0, 0, 0.1);
                border-right: none;
                border-top-left-radius: 5px;
                border-bottom-left-radius: 5px;
                padding: 4px 0px;
                color: #2F2F2F;
                font-family: 'Comic Sans MS', 'Yuanti SC', 'STHeiti';
                font-size: 12px;
                font-weight: bold;
            }}
        """)
        api_input_layout.addWidget(lbl_https_prefix)
        
        # API输入框（只输入https://后面的内容，无缝连接，内容左右边距0）
        self.inp_api_url = QLineEdit("")
        self.inp_api_url.setFixedHeight(24)
        self.inp_api_url.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.inp_api_url.setStyleSheet("""
            QLineEdit {
                background: rgba(255, 255, 255, 0.9);
                border: 1px solid rgba(0, 0, 0, 0.1);
                border-left: none;
                border-top-right-radius: 5px;
                border-bottom-right-radius: 5px;
                padding: 4px 0px;
                color: #2F2F2F;
                font-size: 12px;
            }
            QLineEdit:focus {
                border: 1px solid #81C784;
                border-left: none;
            }
        """)
        api_input_layout.addWidget(self.inp_api_url, 1)
        # API label与输入框容器无间隔（spacing=0，但API label与端口号输入框距离是2px）
        status_stopped_layout.addWidget(api_input_container, 1)
        
        # API输入框容器和启动按钮之间的间距（10px）
        spacer_btn = QWidget()
        spacer_btn.setFixedWidth(10)
        status_stopped_layout.addWidget(spacer_btn)
        
        self.btn_start = QPushButton("启动服务器")
        self.btn_start.setFixedSize(90, 28)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setStyleSheet("""
            QPushButton {
                background: #adf664;
                border: 2px solid #424242;
                border-radius: 12px;
                color: #2F2F2F;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #8cfc03;
                border: 2px solid #212121;
            }
            QPushButton:pressed {
                background: #C0C0C0;
            }
        """)
        self.btn_start.clicked.connect(self.start_server)
        status_stopped_layout.addWidget(self.btn_start, 0, Qt.AlignRight)
        
        # --- 状态页 2: 运行中（显示端口号和正在运行按钮）---
        status_page_running = QWidget()
        status_running_layout = QHBoxLayout(status_page_running)
        status_running_layout.setContentsMargins(0, 0, 0, 0)
        status_running_layout.setSpacing(2)  # 缩小间距到2px
        status_running_layout.setAlignment(Qt.AlignVCenter)  # 垂直居中对齐
        
        # 端口号label（固定宽度，左对齐，与未启动页面对齐）
        self.lbl_running_port = QLabel("端口号:")
        self.lbl_running_port.setFixedWidth(45)
        self.lbl_running_port.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_running_port.setStyleSheet(f"{Style.FONT} color: #2F2F2F; font-size: 12px;")
        status_running_layout.addWidget(self.lbl_running_port)
        
        self.lbl_port_display = QLabel("")
        self.lbl_port_display.setFixedSize(60, 24)
        self.lbl_port_display.setAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        self.lbl_port_display.setStyleSheet("""
            QLabel {
                background: transparent;
                border: none;
                color: #2F2F2F;
                font-size: 12px;
                font-weight: bold;
            }
        """)
        status_running_layout.addWidget(self.lbl_port_display)

        # API label（固定宽度，左对齐，与未启动页面对齐）
        self.lbl_running_api = QLabel("API:")
        self.lbl_running_api.setFixedWidth(34)
        self.lbl_running_api.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_running_api.setStyleSheet(f"{Style.FONT} color: #2F2F2F; font-size: 12px;")
        status_running_layout.addWidget(self.lbl_running_api)

        self.lbl_api_display = QLabel("")
        self.lbl_api_display.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.lbl_api_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.lbl_api_display.setFixedHeight(24)
        self.lbl_api_display.setStyleSheet("""
            QLabel {
                background: transparent;
                border: none;
                color: #2F2F2F;
                font-size: 12px;
                font-weight: bold;
            }
        """)
        status_running_layout.addWidget(self.lbl_api_display, 1)
        
        self.btn_running = QPushButton("正在运行")
        self.btn_running.setFixedSize(90, 28)
        self.btn_running.setCursor(Qt.PointingHandCursor)
        self.btn_running.setStyleSheet("""
            QPushButton {
                background: #fc0317;
                border: 2px solid #000000;
                border-radius: 12px;
                color: black;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, 
                    stop:0 #E53935, stop:1 #C62828);
            }
            QPushButton:pressed {
                background: #B71C1C;
            }
        """)
        self.btn_running.clicked.connect(self.stop_server)
        status_running_layout.addWidget(self.btn_running, 0, Qt.AlignRight)
        
        self.server_status_stack.addWidget(status_page_stopped)
        self.server_status_stack.addWidget(status_page_running)
        first_row_layout.addWidget(self.server_status_stack, 1)
        top_main_layout.addLayout(first_row_layout)
        
        # === 第二行：本机号码 + 邮箱（平分宽度）===
        second_row_layout = QHBoxLayout()
        second_row_layout.setContentsMargins(0, 0, 0, 0)
        second_row_layout.setSpacing(2)  # 缩小间距到2px
        second_row_layout.setAlignment(Qt.AlignVCenter)  # 垂直居中对齐
        
        # 本机号码label（固定宽度，左对齐，与第一行对齐）
        lbl_phone = QLabel("本机号码:")
        lbl_phone.setFixedWidth(60)
        lbl_phone.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_phone.setStyleSheet(f"{Style.FONT} color: #2F2F2F; font-size: 12px;")
        second_row_layout.addWidget(lbl_phone)
        
        # 本机号码按钮（平分宽度）
        self.btn_phone = QPushButton("")
        self.btn_phone.setCursor(Qt.PointingHandCursor)
        self.btn_phone.clicked.connect(self.show_phone_setup_dialog)
        self.btn_phone.setFixedHeight(28)  # 与第一行高度一致
        self.btn_phone.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, 
                    stop:0 rgba(200, 230, 201, 0.85), stop:1 rgba(165, 214, 167, 0.85));
                border: 1px solid rgba(129, 199, 132, 0.5);
                border-radius: 12px;
                color: #2F2F2F;
                font-size: 12px;
                font-weight: 500;
                text-align: left;
                padding-left: 8px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, 
                    stop:0 #d0fcc4, stop:1 #a5d6a7);
                border: 1px solid #81C784;
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, 
                    stop:0 #a8ffbd, stop:1 #70ff9c);
            }
        """)
        second_row_layout.addWidget(self.btn_phone, 1)  # 平分宽度
        
        # 邮箱label（固定宽度，左对齐，与第一行对齐）
        lbl_email = QLabel("邮箱:")
        lbl_email.setFixedWidth(50)
        lbl_email.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_email.setStyleSheet(f"{Style.FONT} color: #2F2F2F; font-size: 12px;")
        second_row_layout.addWidget(lbl_email)
        
        # 邮箱按钮（平分宽度）
        self.btn_email = QPushButton("")
        self.btn_email.setEnabled(False)  # 禁用点击
        self.btn_email.setFixedHeight(28)  # 与第一行高度一致
        self.btn_email.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                color: #2F2F2F;
                font-size: 12px;
                font-weight: 500;
                text-align: left;
                padding-left: 0px;
                padding-top: 2px;
            }
        """)
        second_row_layout.addWidget(self.btn_email, 1)  # 平分宽度
        
        top_main_layout.addLayout(second_row_layout)
 
       
        self.layout.addWidget(top_container)
        
        # 加载保存的配置
        self.load_backend_config()
        


        # ============================================================
        # 3. 系统日志和任务记录 (左右分栏)
        # ============================================================
        main_content_box = QFrame()
        main_content_box.setStyleSheet(
            Style.get_imessage_inbox_card_style()
        )
        
        main_content_layout = QHBoxLayout(main_content_box)
        main_content_layout.setContentsMargins(1, 1, 1, 1)
        main_content_layout.setSpacing(10)
        
        # 左侧：系统日志（使用比 backend panel 更浅的绿色渐变）
        left_box = QFrame()
        left_box.setStyleSheet(
            Style.get_imessage_inbox_card_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #D5F0D6, stop:0.5 #C0E5C2, stop:1 #ABDAAC)",
                border=False
            )
        )
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(5, 5, 2, 5)
        left_layout.setSpacing(5)
        
        # 系统日志标题行
        left_header = QHBoxLayout()
        left_header.addWidget(QLabel("系统日志", styleSheet=f"border:none; {Style.FONT} font-size:13px; font-weight:bold; color:#2F2F2F;"))
        left_header.addStretch()
        left_layout.addLayout(left_header)
        
        # 日志显示区域
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet(
            Style.get_imessage_inbox_text_edit_style(
                border=False
            )
        )
        left_layout.addWidget(self.log_text)
        
        # 右侧：任务记录（使用比 backend panel 更浅的绿色渐变）
        right_box = QFrame()
        right_box.setStyleSheet(
            Style.get_imessage_inbox_card_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #D5F0D6, stop:0.5 #C0E5C2, stop:1 #ABDAAC)",
                border=False
            )
        )
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(2, 5, 5, 5)
        right_layout.setSpacing(5)
        
        # 任务记录标题
        right_header = QHBoxLayout()
        right_header.addWidget(QLabel("任务记录", styleSheet=f"border:none; {Style.FONT} font-size:13px; font-weight:bold; color:#2F2F2F;"))
        right_header.addStretch()
        right_layout.addLayout(right_header)
        
        # 任务记录显示区域
        self.task_records_list = QTextEdit()
        self.task_records_list.setReadOnly(True)
        self.task_records_list.setStyleSheet(
            Style.get_imessage_inbox_text_edit_style(
                border=False
            )
        )
        right_layout.addWidget(self.task_records_list)
        
        # 添加到主布局
        main_content_layout.addWidget(left_box, 1)
        main_content_layout.addWidget(right_box, 1)
        
        self.layout.addWidget(main_content_box, 1)
        
        # 初始化任务记录数据
        self.task_records = []  # 存储任务记录: [{"time": "10:03", "total": 91, "success": 80, "fail": 11}, ...]
        self.task_total = {"total": 0, "success": 0, "fail": 0}  # 总计

        # ========= 线程安全信号（关键修复：禁止在 ServerWorker 线程直接操作 UI）=========
        self.signals = ServerSignals()
        try:
            # 强制使用 QueuedConnection：确保 slot 在 UI 线程执行（避免 macOS 直接 segfault）
            self.signals.log.connect(self.log_message, type=Qt.QueuedConnection)
            self.signals.task_record.connect(self.add_task_record, type=Qt.QueuedConnection)
        except Exception:
            pass

    def start_server(self):
        """启动后端服务器"""
        try:
            # 获取端口
            port_raw = (self.inp_server_port.text() or "").strip()
            if not port_raw:
                self.log_message("❌ 请输入端口号")
                try:
                    self.inp_server_port.setFocus()
                except:
                    pass
                return
            try:
                listen_port = int(port_raw)
                if listen_port <= 0 or listen_port > 65535:
                    raise ValueError("端口范围错误")
            except Exception:
                self.log_message("❌ 端口号无效（1-65535）")
                try:
                    self.inp_server_port.setFocus()
                except:
                    pass
                return

            # 获取API地址
            # 兼容三种输入：
            # 1) 完整URL: http(s)://host[:port][/api]
            # 2) host[:port][/api]（旧模式：以前会自动拼 https://）
            # 3) 内网/本机：localhost / 127.0.0.1 / 192.168.x.x（建议 http://）
            api_url_input = (self.inp_api_url.text() or "").strip()
            if not api_url_input:
                # 打印调试信息，避免“明明填了却提示为空”的困惑
                try:
                    raw_api = self.inp_api_url.text()
                    raw_port = self.inp_server_port.text()
                    self.log_message(f"❌ 请输入API地址（你填的值未被读取到）")
                    self.log_message(f"   当前读取：port='{raw_port}' api='{raw_api}'")
                except:
                    self.log_message("❌ 请输入API地址")
                try:
                    self.inp_api_url.setFocus()
                except:
                    pass
                return
            
            # 自动补全协议：
            if "://" in api_url_input:
                api_url = api_url_input
            else:
                # 旧行为默认 https；但本机/内网优先 http（更符合调试/自建环境）
                lowered = api_url_input.lower()
                if "localhost" in lowered or "127.0.0.1" in lowered or lowered.startswith("192.168.") or lowered.startswith("10.") or lowered.startswith("172.16.") or lowered.startswith("172.17.") or lowered.startswith("172.18.") or lowered.startswith("172.19.") or lowered.startswith("172.2") or lowered.startswith("172.3"):
                    api_url = "http://" + api_url_input
                else:
                    api_url = "https://" + api_url_input
            
            # 确保以 /api 结尾
            if not api_url.endswith('/api'):
                if api_url.endswith('/'):
                    api_url = api_url.rstrip('/') + '/api'
                else:
                    api_url = api_url + '/api'
            
            # 设置环境变量（让后端使用）
            os.environ["API_BASE_URL"] = api_url
            
            # 创建服务器实例（在try块中，以便捕获可能的错误）
            try:
                self.server = AutoSenderServer()
            except Exception as e:
                self.log_message(f"❌ 创建服务器实例失败: {e}")
                import traceback
                self.log_message(f"详细错误: {traceback.format_exc()}")
                return
            
            # 手动设置API地址（确保使用最新的值）
            self.server.api_base_url = api_url
            
            # 获取用户全名作为服务器ID（跨平台）
            import subprocess
            import platform
            import socket
            
            system = platform.system()
            if system == "Windows":
                # Windows系统：使用计算机名和用户名组合
                try:
                    computer_name = socket.gethostname()
                    username = os.getenv("USERNAME") or os.getenv("USER") or "User"
                    server_id = f"{computer_name}-{username}"
                except Exception:
                    server_id = os.getenv("USERNAME") or os.getenv("USER") or "Windows-User"
            elif system == "Darwin":  # macOS
                # macOS系统：使用dscl获取RealName
                try:
                    username = os.getenv("USER")
                    result = subprocess.run(["dscl", ".", "-read", f"/Users/{username}", "RealName"],
                        capture_output=True, text=True, timeout=2)
                    lines = result.stdout.strip().split('\n')
                    server_id = lines[1].strip() if len(lines) >= 2 else lines[0].split(':', 1)[1].strip()
                except Exception:
                    # 如果dscl失败，使用用户名作为fallback
                    server_id = os.getenv("USER") or "macOS-User"
            else:
                # Linux或其他系统：使用用户名
                server_id = os.getenv("USER") or os.getenv("USERNAME") or "Unknown-User"
            
            # 设置服务器ID
            self.server.server_id = server_id
            # 统一：内部只有 server_id；把"名称"也视为 server_id（不再存 server_name）
            try:
                self.server.server_port = listen_port
            except Exception:
                pass
            try:
                # 从按钮读取本机号码（如果有）
                self.server.server_phone = (self.btn_phone.text() or "").strip()
            except Exception:
                pass

            # region agent log
            try:
                sid_hash = hashlib.sha256(str(server_id).encode("utf-8", errors="ignore")).hexdigest()[:8]
            except Exception:
                sid_hash = None
            _agent_dbg_log(
                hypothesisId="B",
                location="localserver.py:PanelBackend.start_server",
                message="server_id_assigned",
                data={
                    "server_id_len": len(str(server_id or "")),
                    "server_id_hash8": sid_hash,
                    "server_has_serverid_attr": bool(hasattr(self.server, "serverid")),
                    "server_has_server_name_attr": bool(hasattr(self.server, "server_name")),
                    "server_server_id_set": bool(getattr(self.server, "server_id", None)),
                },
            )
            # endregion
            
            # 将服务器实例也保存到主窗口，供前端使用
            if self.main_window:
                self.main_window.server = self.server
            
            # 设置日志回调
            # 关键修复：后端线程的日志通过 signal 投递到主线程
            self.server.log_callback = lambda m: self.signals.log.emit(str(m))
            
            # 设置任务记录回调
            self.server.task_record_callback = lambda total, success, fail: self.signals.task_record.emit(int(total), int(success), int(fail))
            
            # 连接超级管理员命令信号
            self.server.signals = self.signals
            self.signals.super_admin_command.connect(self.handle_super_admin_command)

            # 设置开始时间（在切换状态之前）
            self.start_time = datetime.now()

            # 保存本次启动参数（供 QThread/事件循环使用）
            self._listen_port = listen_port
            
            # 切换到运行状态
            self.switch_to_running()
            
            # 更新显示的API地址
            if hasattr(self, 'lbl_api_display'):
                self.lbl_api_display.setText(api_url)
            
            
            # 初始化任务记录显示
            if hasattr(self, 'task_records_list'):
                self.task_records_list.setPlainText("暂无任务记录")
            # 重置任务记录
            self.task_records = []
            self.task_total = {"total": 0, "success": 0, "fail": 0}
            self.update_task_records_display()

            # 使用 QThread 运行服务器（替代 threading.Thread）
            self.server_thread = ServerWorker(self)
            # 关键修复：后台线程错误不要直连 UI，统一走 signal 排队到 UI 线程
            try:
                self.server_thread.error.connect(lambda m: self.signals.log.emit(str(m)))
            except Exception:
                self.server_thread.error.connect(lambda m: None)
            self.server_thread.start()

            # 保存配置
            self.save_backend_config()
            
            # 显示启动消息
            self.log_message(f"正在启动服务器 端口号: {listen_port}")

        except Exception as e:
            self.log_message(f"❌ 后端服务器启动失败: {e}")

    def stop_server(self):
        """停止后端服务器（非阻塞）"""
        try:
            # 立即切换UI状态，避免用户感觉卡顿
            self.switch_to_stopped()
            
            # 保存配置
            self.save_backend_config()
            
            # 在后台线程执行清理，避免阻塞UI
            def cleanup_in_background():
                try:
                    # 停止 HTTP 服务器（释放端口）
                    if hasattr(self, 'runner') and self.runner:
                        try:
                            async def cleanup_runner():
                                try:
                                    if self.site:
                                        await self.site.stop()
                                    if self.runner:
                                        await self.runner.cleanup()
                                except:
                                    pass
                            loop = asyncio.new_event_loop()
                            loop.run_until_complete(asyncio.wait_for(cleanup_runner(), timeout=1.0))
                            loop.close()
                        except:
                            pass
                        self.runner = None
                        self.site = None
                    
                    if self.server:
                        # 停止发送任务
                        self.server.sending = False

                        # 停止收件箱检查器
                        if hasattr(self.server, "inbox_checker_task") and self.server.inbox_checker_task:
                            self.server.inbox_checker_task.cancel()

                        # 停止 worker WS 和关闭 session（合并到一个循环）
                        try:
                            async def cleanup_server():
                                try:
                                    if hasattr(self.server, 'stop_worker_ws'):
                                        await asyncio.wait_for(self.server.stop_worker_ws(), timeout=0.5)
                                except:
                                    pass
                                try:
                                    if hasattr(self.server, '_close_session'):
                                        await asyncio.wait_for(self.server._close_session(), timeout=0.5)
                                except:
                                    pass
                            loop = asyncio.new_event_loop()
                            loop.run_until_complete(asyncio.wait_for(cleanup_server(), timeout=1.5))
                            loop.close()
                        except:
                            pass
                        
                        self.server = None

                    # 停止 QThread（如果正在运行）
                    if hasattr(self, 'server_thread') and self.server_thread:
                        if self.server_thread.isRunning():
                            self.server_thread.terminate()
                            self.server_thread.wait(500)  # 最多等待0.5秒
                        self.server_thread = None
                    
                except Exception as e:
                    print(f"❌ 后台清理出错: {e}")
            
            # 启动后台清理线程
            cleanup_thread = threading.Thread(target=cleanup_in_background, daemon=True)
            cleanup_thread.start()
            
            self.log_message("✅ 后端服务器已停止")

        except Exception as e:
            self.log_message(f"❌ 停止服务器时出错: {e}")

    async def run_async_server_ws(self):
        """异步运行服务器（中心下发模式：worker 长连 API WS，拒绝轮询拉任务）"""
        try:
            if not self.server:
                try:
                    self.signals.log.emit("❌ 服务器实例不存在")
                except Exception:
                    pass
                return

            api_url = self.server.api_base_url
            if not api_url:
                try:
                    self.signals.log.emit("❌ API地址未配置")
                except Exception:
                    pass
                return

            await self.server.start_worker_ws()
            try:
                self.signals.log.emit("✅ worker WS 已连接中心API（中心下发模式）")
            except Exception:
                pass

            # 保持运行
            await asyncio.Future()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                self.signals.log.emit(f"服务器运行错误: {e}")
            except Exception:
                pass
        finally:
            # 关闭 worker WS
            try:
                if self.server and hasattr(self.server, "stop_worker_ws"):
                    await self.server.stop_worker_ws()
            except Exception:
                pass

            # 清理资源：关闭长生命周期的 session
            if self.server and hasattr(self.server, "_close_session"):
                try:
                    await self.server._close_session()
                except Exception:
                    pass

    def redirect_backend_logs(self):
        """重定向后端日志到GUI"""
        import builtins

        # 保存原始的print函数
        self.original_print = builtins.print

        def new_print(*args, **kwargs):
            """新的print函数，同时输出到控制台和GUI状态面板"""
            # 调用原始print保持控制台输出
            self.original_print(*args, **kwargs)

            # 将消息组合成字符串
            message = " ".join(str(arg) for arg in args)

            # 关键修复：不允许在后台线程触碰 UI，统一走 signal 排队到 UI 线程
            if message.strip():
                try:
                    self.signals.log.emit(f"[后端] {message}")
                except Exception:
                    pass

        # 重定向print
        builtins.print = new_print

    def switch_to_running(self):
        """切换到运行界面状态"""
        # 更新端口显示
        try:
            if hasattr(self, "inp_server_port") and hasattr(self, "lbl_port_display"):
                self.lbl_port_display.setText((self.inp_server_port.text() or "").strip())
        except Exception:
            pass

        # 更新API地址显示（显示完整URL，包括https://）
        try:
            api_url_input = (self.inp_api_url.text() or "").strip()
            if api_url_input:
                current_api = 'https://' + api_url_input
            else:
                current_api = ""
            if hasattr(self, "lbl_api_display"):
                self.lbl_api_display.setText(current_api)
        except Exception:
            pass
        # 切换到运行状态页面
        self.server_status_stack.setCurrentIndex(1)
        self.backend_server_running = True

    def switch_to_stopped(self):
        """切换到停止界面状态"""
        self.server_status_stack.setCurrentIndex(0)
        self.backend_server_running = False

    def log_message(self, message):
        """添加日志消息"""
        timestamp = datetime.now().strftime("%H:%M")
        # 使用setPlainText + 追加文本的方式，避免QTextCursor跨线程问题
        current_text = self.log_text.toPlainText()
        new_text = f"[{timestamp}] {message}"
        if current_text:
            self.log_text.setPlainText(current_text + "\n" + new_text)
        else:
            self.log_text.setPlainText(new_text)

        # 自动滚动到底部
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def add_task_record(self, total, success, fail):
        """添加任务记录（从后端服务器调用）"""
        try:
            time_str = datetime.now().strftime("%H:%M")
            record = {
                "time": time_str,
                "total": total,
                "success": success,
                "fail": fail
            }
            self.task_records.append(record)
            # 更新总计
            self.task_total["total"] += total
            self.task_total["success"] += success
            self.task_total["fail"] += fail
            # 更新显示
            self.update_task_records_display()
        except Exception as e:
            print(f"添加任务记录失败: {e}")
    
    def update_task_records_display(self):
        """更新任务记录显示"""
        if not hasattr(self, 'task_records_list'):
            return
        
        records_text = ""
        
        # 显示所有任务记录（从上到下，最新的在下面）
        for record in self.task_records:
            success_rate = (record["success"] / record["total"] * 100) if record["total"] > 0 else 0
            records_text += f"{record['time']}  任务:{record['total']}  成功{record['success']}  失败{record['fail']}  成功率{success_rate:.1f}%\n"
        
        # 显示总计（固定在最下面）
        if self.task_total["total"] > 0:
            total_success_rate = (self.task_total["success"] / self.task_total["total"] * 100) if self.task_total["total"] > 0 else 0
            records_text += f"\ntotal    {self.task_total['total']}   成功{self.task_total['success']}   失败{self.task_total['fail']}   成功率{total_success_rate:.1f}%"
        
        if not records_text:
            records_text = "暂无任务记录"
        
        self.task_records_list.setPlainText(records_text)
        
    def load_backend_config(self):
        """加载后端服务器配置"""
        try:
            if os.path.exists(self.backend_config_file):
                with open(self.backend_config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    
                    # 加载API地址（去掉https://前缀，只显示后面的内容）
                    if config.get("api_url"):
                        api_url = config["api_url"]
                        # 去掉https://或http://前缀
                        if api_url.startswith("https://"):
                            api_url = api_url[8:]
                        elif api_url.startswith("http://"):
                            api_url = api_url[7:]
                        self.inp_api_url.setText(api_url)

                    # 加载端口
                    if config.get("port") and hasattr(self, "inp_server_port"):
                        self.inp_server_port.setText(str(config["port"]))

                    
                    # 加载本机号码（只显示号码，不显示"本机号码:"前缀）
                    if config.get("phone"):
                        self.btn_phone.setText(config['phone'])
                    
                    # 加载本机名称（如果有服务器实例，更新它）
                    if config.get("server_name") and hasattr(self, 'server') and self.server:
                        # 旧逻辑（保留，不删除）：self.server.server_name = config["server_name"]
                        self.server.server_id = config["server_name"]
                    
                    return config
        except Exception as e:
            print(f"加载后端配置失败: {e}")
        
        return {}
    
    def save_backend_config(self):
        """保存后端服务器配置"""
        try:
            config = {}
            
            # 保存API地址（兼容：允许用户输入完整URL，避免强制 https:// 覆盖本机/内网 http://）
            api_url_input = (self.inp_api_url.text() or "").strip()
            if api_url_input:
                if "://" in api_url_input:
                    config["api_url"] = api_url_input
                else:
                    # 保持旧行为：未写协议则默认保存为 https://host
                    config["api_url"] = 'https://' + api_url_input

            # 保存端口
            if hasattr(self, "inp_server_port"):
                port_raw = (self.inp_server_port.text() or "").strip()
                if port_raw:
                    config["port"] = port_raw

            
            # 保存本机号码（按钮文本直接就是号码）
            phone_text = self.btn_phone.text().strip()
            if phone_text:
                config["phone"] = phone_text
            
            # 保存本机名称
            if hasattr(self, 'server') and self.server:
                # 旧逻辑（保留，不删除）：config["server_name"] = self.server.server_name
                # 兼容：仍使用 server_name 这个 key 保存，但值来源于 server_id（内部只保留 server_id）
                config["server_name"] = self.server.server_id
            elif hasattr(self, '_temp_server_name'):
                # 如果服务器未创建但有临时保存的名称
                config["server_name"] = self._temp_server_name
            else:
                # 如果没有服务器实例，尝试从配置中保留
                try:
                    if os.path.exists(self.backend_config_file):
                        with open(self.backend_config_file, "r", encoding="utf-8") as f:
                            old_config = json.load(f)
                            if old_config.get("server_name"):
                                config["server_name"] = old_config["server_name"]
                except:
                    pass
            
            # 保存到文件
            with open(self.backend_config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存后端配置失败: {e}")

    def handle_super_admin_command(self, action, params):
        """处理超级管理员命令"""
        try:
            if action == "start_server":
                if not self.backend_server_running:
                    self.start_server()
                else:
                    self.log_message("⚠️ 服务器已在运行中")
            
            elif action == "stop_server":
                if self.backend_server_running:
                    self.stop_server()
                else:
                    self.log_message("⚠️ 服务器未运行")
            
            elif action == "diagnose":
                # 调用工具面板的诊断功能
                if hasattr(self.main_window, 'panel_tools'):
                    self.main_window.panel_tools.run_diagnose()
                else:
                    self.log_message("⚠️ 工具面板不可用")
            
            elif action == "db_diagnose":
                if hasattr(self.main_window, 'panel_tools'):
                    self.main_window.panel_tools.run_database_diagnose()
                else:
                    self.log_message("⚠️ 工具面板不可用")
            
            elif action == "fix_permission":
                if hasattr(self.main_window, 'panel_tools'):
                    self.main_window.panel_tools.run_permission_fix()
                else:
                    self.log_message("⚠️ 工具面板不可用")
            
            elif action == "clear_inbox":
                if hasattr(self.main_window, 'panel_tools'):
                    self.main_window.panel_tools.clear_imessage_inbox()
                else:
                    self.log_message("⚠️ 工具面板不可用")
            
            elif action == "login":
                account = params.get("account", "")
                password = params.get("password", "")
                if account and password and hasattr(self.main_window, 'panel_id'):
                    # 填充账号密码并执行登录
                    self.main_window.panel_id.edit_id.setText(account)
                    self.main_window.panel_id.edit_pass.setText(password)
                    self.main_window.panel_id.accept_login()
                else:
                    self.log_message("⚠️ 账号面板不可用或缺少账号信息")
            
            else:
                self.log_message(f"⚠️ 未知命令: {action}")
        except Exception as e:
            self.log_message(f"❌ 执行命令失败: {str(e)}")
            import traceback
            traceback.print_exc()

    def update_server_stats(self, connected=0, connecting=0, total_tasks=0, success=0, failed=0):
        """更新服务器统计信息"""
        self.lbl_connected.setText(f"已连接: {connected}")
        self.lbl_connecting.setText(f"正在连接: {connecting}")

        if self.start_time:
            elapsed = datetime.now() - self.start_time
            total_minutes = elapsed.seconds // 60
            self.lbl_stats.setText(
                f"总时长: {total_minutes}m  任务总数: {total_tasks}  成功: {success}  失败: {failed}"
            )

    def show_phone_setup_dialog(self):
        from PyQt5.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout,
            QLabel, QLineEdit, QPushButton, QFrame
        )
        from PyQt5.QtCore import Qt, QTimer
        import subprocess
        import threading

        # ===== 打开信息应用的偏好设置（不激活信息应用窗口）=====
        def open_preferences():
            try:
                applescript = '''
                tell application "System Events"
                    tell process "Messages"
                        keystroke "," using command down
                    end tell
                end tell
                '''
                def run_script():
                    try:
                        subprocess.Popen(["osascript", "-e", applescript], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except:
                        pass
                thread = threading.Thread(target=run_script, daemon=True)
                thread.start()
            except:
                pass

        # ===== Dialog（可拖动）=====
        dialog = QDialog(self)
        dialog.setFixedSize(180, 105)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.setAttribute(Qt.WA_TranslucentBackground)

        # 添加拖动功能的变量
        dialog._drag_position = None
        
        # 重写鼠标事件实现拖动
        def mousePressEvent(event):
            if event.button() == Qt.LeftButton:
                dialog._drag_position = event.globalPos() - dialog.frameGeometry().topLeft()
                event.accept()
        
        def mouseMoveEvent(event):
            if event.buttons() == Qt.LeftButton and dialog._drag_position is not None:
                dialog.move(event.globalPos() - dialog._drag_position)
                event.accept()
        
        def mouseReleaseEvent(event):
            if event.button() == Qt.LeftButton:
                dialog._drag_position = None
                event.accept()
        
        dialog.mousePressEvent = mousePressEvent
        dialog.mouseMoveEvent = mouseMoveEvent
        dialog.mouseReleaseEvent = mouseReleaseEvent

        bg = QFrame(dialog)
        bg.setGeometry(0, 0, 180, 105)
        bg.setStyleSheet("""
            QFrame {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #E8F5E9,
                    stop:0.5 #C8E6C9,
                    stop:1 #A5D6A7
                );
                border-radius: 14px;
                border: 2px solid #2F2F2F;
            }
        """)

        main = QVBoxLayout(bg)
        main.setContentsMargins(10, 8, 10, 8)
        main.setSpacing(5)

        input_style = """
            QLineEdit {
                background: rgba(255,255,255,0.95);
                border: 1px solid rgba(0,0,0,0.2);
                border-radius: 8px;
                padding: 4px 8px;
                color: #2F2F2F;
                font-size: 11px;
            }
            QLineEdit:focus {
                border: 1px solid #66BB6A;
            }
        """

        # ===== 名称输入 =====
        inp_name = QLineEdit()
        inp_name.setFixedHeight(24)
        inp_name.setStyleSheet(input_style)
        inp_name.setPlaceholderText("名称")
        
        # 自动填充当前名称
        if hasattr(self, 'server') and self.server:
            # 旧逻辑（保留，不删除）：inp_name.setText(self.server.server_name or "")
            inp_name.setText(self.server.server_id or "")
        elif hasattr(self, '_temp_server_id'):
            inp_name.setText(self._temp_server_name or "")
        
        main.addWidget(inp_name)

        # ===== 本机号码 =====
        inp_phone = QLineEdit()
        inp_phone.setStyleSheet(input_style)
        inp_phone.setPlaceholderText("本机号码")
        inp_phone.setFixedHeight(24)
        current = self.btn_phone.text().strip()
        if current:
            inp_phone.setText(current)
        main.addWidget(inp_phone)

        # ===== 按钮行（平分）=====
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        btn_cancel = QPushButton("取消")
        btn_ok = QPushButton("保存")

        btn_cancel.setFixedHeight(22)
        btn_cancel.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #F5F5F5, stop:1 #E0E0E0);
                border-radius: 11px;
                font-size: 11px;
                color: #616161;
                border: 1px solid #BDBDBD;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #EEEEEE, stop:1 #BDBDBD);
            }
        """)
        btn_cancel.clicked.connect(dialog.reject)

        btn_ok.setFixedHeight(22)
        btn_ok.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #A5D6A7, stop:1 #81C784);
                border-radius: 11px;
                font-size: 11px;
                color: #1B5E20;
                font-weight: bold;
                border: 1px solid #66BB6A;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #81C784, stop:1 #66BB6A);
            }
        """)

        def on_save():
            full_name = inp_name.text().strip()
            phone = inp_phone.text().strip()
            
            # 更新按钮文本（号码为空 = 移除显示）
            self.btn_phone.setText(phone if phone else "")
            
            # 如果有名称，执行 sudo dscl 命令设置 RealName
            if full_name:
                try:
                    import getpass
                    current_user = getpass.getuser()
                    dscl_cmd = f'sudo dscl . -create /Users/{current_user} RealName "{full_name}"'
                    
                    def run_dscl():
                        try:
                            result = subprocess.run(dscl_cmd, shell=True, capture_output=True, text=True, timeout=5)
                            if result.returncode == 0:
                                if self.server:
                                    self.server.server_id = full_name
                        except:
                            pass
                    
                    threading.Thread(target=run_dscl, daemon=True).start()
                except:
                    pass
            
            # 保存配置
            self.save_backend_config()
            
            # 发送给 API
            if self.server and hasattr(self.server, 'api_base_url') and hasattr(self.server, 'loop'):
                try:
                    asyncio.run_coroutine_threadsafe(
                        # 旧逻辑（保留，不删除）：self.server._send_server_info_to_api(full_name or self.server.server_name, phone),
                        self.server._send_server_info_to_api(full_name or self.server.server_id, phone),
                        self.server.loop
                    )
                except:
                    pass
            
            # 关闭偏好设置面板
            def close_preferences():
                try:
                    applescript = 'tell application "System Events" to tell process "Messages" to keystroke "w" using command down'
                    subprocess.Popen(["osascript", "-e", applescript], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except:
                    pass
            
            QTimer.singleShot(30, close_preferences)
            dialog.accept()

        btn_ok.clicked.connect(on_save)

        btn_row.addWidget(btn_cancel, 1)
        btn_row.addWidget(btn_ok, 1)
        main.addLayout(btn_row)

        # ===== 位置：任务记录显示板右上角 =====
        if hasattr(self, 'task_records_list'):
            task_panel_pos = self.task_records_list.mapToGlobal(QPoint(0, 0))
            task_panel_width = self.task_records_list.width()
            dialog.move(task_panel_pos.x() + task_panel_width - dialog.width() - 5, task_panel_pos.y() + 5)
        else:
            from PyQt5.QtWidgets import QApplication
            screen = QApplication.primaryScreen().geometry()
            dialog.move(screen.width() - dialog.width() - 50, 100)

        # 弹窗稍微晚一点出现，让偏好设置先打开
        open_preferences()
        QTimer.singleShot(50, lambda: None)  # 微小延迟让偏好设置先响应
        
        dialog.exec_()

class PanelIMessage(FixedSizePanel):
    # 定义信号用于线程安全的UI更新
    task_log_signal = pyqtSignal(str)
    update_stats_signal = pyqtSignal(int, int, int)
    update_ui_state_signal = pyqtSignal()
    
    def __init__(self, parent_window):
        gradient_bg = Style.get_imessage_inbox_panel_gradient()
        super().__init__(gradient_bg, 552, 430, parent_window)
        self.main_window = parent_window
        self.sending = False
        self.config_dir = os.path.abspath("logs")
        os.makedirs(self.config_dir, exist_ok=True)
        self.config_file = os.path.join(self.config_dir, "autosave_config.json")      
        self.server = None 
        
        # 全局统计
        self.global_stats = {
            "task_count": 0,
            "total_sent": 0,
            "total_success": 0,
            "total_fail": 0
        }
        
        # 连接信号到槽函数
        self.task_log_signal.connect(self._task_status_log_slot)
        self.update_stats_signal.connect(self._update_stats_slot)
        self.update_ui_state_signal.connect(self._update_ui_state_slot)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        # 标题栏 - 移除渐变背景
        self.header = QFrame()
        self.header.setFixedHeight(35)
        self.header.setStyleSheet(Style.get_panel_title_bar_style())
        header_layout = QHBoxLayout(self.header)
        header_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header_layout.setContentsMargins(13, 0, 0, 0)
        header_layout.setSpacing(0)
        lbl_title = QLabel("iMessage")
        lbl_title.setStyleSheet(
            f"border: none; color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 15px; padding: 0px;"
        )
        header_layout.addWidget(lbl_title)
        layout.addWidget(self.header)

        # 内容区域 - 统一边距 8, 8, 8, 0
        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(5, 8, 5, 8)
        self.layout.setSpacing(0)
        layout.addLayout(self.layout)

        # 1. 上半部分：左右两个输入框
        top_area = QHBoxLayout()

        # 左框 - 发送号码
        box_l = QFrame()
        box_l.setStyleSheet(
            Style.get_imessage_inbox_card_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(168, 200, 255, 0.25), stop:0.5 rgba(255, 154, 162, 0.20), stop:1 rgba(168, 200, 255, 0.22))"
            )
        )
        l_layout = QVBoxLayout(box_l)
        l_layout.setContentsMargins(2, 2, 2, 2)
        l_layout.setSpacing(2)

        # 号码框标题和按钮行
        l_header = QHBoxLayout()
        l_header.setContentsMargins(0, 0, 0, 0)
        l_header.setSpacing(2)
        l_header.addWidget(QLabel("发送号码", styleSheet=f"border:none; color: #2F2F2F; {Style.FONT}"))
        l_header.addStretch()

        # 号码框右上角按钮
        self.btn_import_recv = QPushButton("📂")
        self.btn_import_recv.setFixedSize(40, 30)
        self.btn_import_recv.setStyleSheet(
            Style.get_imessage_inbox_icon_button_style("rgba(139, 0, 255, 0.2)", "rgba(139, 0, 255, 0.3)")
        )
        self.btn_import_recv.clicked.connect(self.import_numbers_file)
        l_header.addWidget(self.btn_import_recv)

        self.btn_clear_recv = QPushButton("🗑️")
        self.btn_clear_recv.setFixedSize(40, 30)
        self.btn_clear_recv.setStyleSheet(
            Style.get_imessage_inbox_icon_button_style("rgba(255, 0, 0, 0.2)", "rgba(255, 0, 0, 0.3)")
        )
        self.btn_clear_recv.clicked.connect(lambda: self.recv_text.clear())
        l_header.addWidget(self.btn_clear_recv)

        l_layout.addLayout(l_header)

        # 号码输入框（带计数器）
        self.recv_text = TextEditWithCounter("每个号码一行或逗号分隔", is_phone_counter=True, parent=self, placeholder_font_size=8)
        l_layout.addWidget(self.recv_text)

        # 右框 - 发送内容
        box_r = QFrame()
        box_r.setStyleSheet(
            Style.get_imessage_inbox_card_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(200, 255, 220, 0.35), stop:0.45 rgba(255, 200, 220, 0.30), stop:1 rgba(255, 220, 230, 0.32))"
            )
        )
        r_layout = QVBoxLayout(box_r)
        r_layout.setContentsMargins(2, 2, 2, 2)
        r_layout.setSpacing(2)

        # 内容框标题和按钮行
        r_header = QHBoxLayout()
        r_header.setContentsMargins(0, 0, 0, 0)
        r_header.setSpacing(2)
        r_header.addWidget(QLabel("发送内容", styleSheet=f"border:none; color: #2F2F2F; {Style.FONT}"))
        r_header.addStretch()

        # 内容框右上角按钮
        self.btn_import_send = QPushButton("📂")
        self.btn_import_send.setFixedSize(40, 30)
        self.btn_import_send.setStyleSheet(
            Style.get_imessage_inbox_icon_button_style("rgba(139, 0, 255, 0.2)", "rgba(139, 0, 255, 0.3)")
        )
        self.btn_import_send.clicked.connect(self.import_message_file)
        r_header.addWidget(self.btn_import_send)

        self.btn_clear_send = QPushButton("🗑️")
        self.btn_clear_send.setFixedSize(40, 30)
        self.btn_clear_send.setStyleSheet(
            Style.get_imessage_inbox_icon_button_style("rgba(255, 0, 0, 0.2)", "rgba(255, 0, 0, 0.3)")
        )
        self.btn_clear_send.clicked.connect(lambda: self.send_text.clear())
        r_header.addWidget(self.btn_clear_send)

        r_layout.addLayout(r_header)

        # 消息输入框（带计数器）
        self.send_text = TextEditWithCounter("请输入短信内容...", is_phone_counter=False, parent=self, placeholder_font_size=8)
        r_layout.addWidget(self.send_text)

        # 左侧区域：上下布局（发送号码和发送内容，比例4:3）
        left_area = QVBoxLayout()
        left_area.setSpacing(10)
        left_area.addWidget(box_l, 4)  # 发送号码占4
        left_area.addWidget(box_r, 3)  # 发送内容占3
        
        # 右侧区域：发送结果
        right_area = QFrame()
        right_area.setStyleSheet(
            Style.get_imessage_inbox_card_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(168, 200, 255, 0.22), stop:0.5 rgba(255, 154, 162, 0.18), stop:1 rgba(168, 200, 255, 0.20))"
            )
        )
        right_layout = QVBoxLayout(right_area)
        right_layout.setContentsMargins(8, 8, 8, 8)
        
        # 发送结果标题
        result_header = QHBoxLayout()
        result_header.addWidget(
            QLabel(
                "发送结果",
                styleSheet="border:none; font-weight: bold; font-size: 13px; color: #2F2F2F;",
            )
        )
        result_header.addStretch()
        right_layout.addLayout(result_header)
        
        # 发送结果显示区域
        self.task_status_text = QTextEdit()
        self.task_status_text.setReadOnly(True)
        self.task_status_text.setFocusPolicy(Qt.NoFocus)
        self.task_status_text.setFrameStyle(QTextEdit.NoFrame)
        self.task_status_text.setStyleSheet(
            Style.get_imessage_inbox_text_edit_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(240, 250, 255, 0.70), stop:0.45 rgba(255, 240, 250, 0.68), stop:1 rgba(255, 250, 255, 0.70))",
                border=False,
            )
        )
        right_layout.addWidget(self.task_status_text)
        
        # 主布局：左右分布
        main_content = QHBoxLayout()
        main_content.setSpacing(10)
        main_content.addLayout(left_area, 1)  # 左侧占比1
        main_content.addWidget(right_area, 1)  # 右侧占比1
        self.layout.addLayout(main_content, 1)  # 主内容区域占高度比例 1

        # 2. 发送控制条
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(2)
        ctrl_row.setContentsMargins(5, 5, 5, 5)
        
        # 统计bar (左边)
        self.global_stats_label = QLabel("🌈总数: 0 |成功: 0 |失败: 0 |成功率: 0%")
        self.global_stats_label.setStyleSheet(
            "border: none; background: transparent; padding: 0; font-size: 11px;font-weight:bold ; color: #2F2F2F;")
        ctrl_row.addWidget(self.global_stats_label)
        
        ctrl_row.addStretch()
        ctrl_row.addWidget(QLabel("发送间隔:", styleSheet=f"border: none; background: transparent; color: #2F2F2F; {Style.FONT}"))

        # 间隔选择框 - 宽度缩小到只能显示X.X
        self.interval_input = QLineEdit()
        self.interval_input.setFixedSize(50, 25)
        self.interval_input.setAlignment(Qt.AlignCenter)
        self.interval_input.setText("1.0")
        self.interval_input.setReadOnly(True)  
        self.interval_input.setStyleSheet("""
            QLineEdit {
            background: yellow;
            border-radius: 8px;
            border: 1px solid rgba(100, 100, 100, 0.3);
            padding: 2px 5px;
            color: #2F2F2F;
        }
        """)
        ctrl_row.addWidget(self.interval_input)

        # 下拉按钮
        self.btn_interval_dropdown = QPushButton("▼")
        self.btn_interval_dropdown.setFixedSize(25, 25)
        self.btn_interval_dropdown.setStyleSheet(
            "QPushButton { border: 2px solid #2F2F2F; border-radius: 12px; background: #FFFDE7; }"
            "QPushButton:hover { border-radius: 12px; background: rgba(139, 0, 255, 0.2); }"
            "QPushButton:pressed { border-radius: 12px; background: rgba(139, 0, 255, 0.3); }"
        )
        self.btn_interval_dropdown.clicked.connect(self.show_interval_menu)
        ctrl_row.addWidget(self.btn_interval_dropdown)

        # 开始按钮 - 按时样式
        self.btn_send = QPushButton("Send")
        self.btn_send.setFixedHeight(30)
        self.btn_send.setFixedWidth(60)
        self.btn_send.clicked.connect(self.start_sending)
        self.btn_send.setStyleSheet("""
            QPushButton {
                border-radius: 10px;

                padding-left: 5px;
                padding-right: 5px;
                font-weight: bold;
                background-color: #FFFACD;
                border: 2px solid #000000;
            }
            QPushButton:hover {
                background-color: #FFFFE0;
                border: 1px solid #FFA500;
            }
            QPushButton:pressed {
                background-color: #FFE4B5;
                border: 1px solid #FF8C00;
            }
        """)

        ctrl_row.addWidget(self.btn_send)

        self.layout.addLayout(ctrl_row)

        # 初始化间隔选择菜单
        self.init_interval_menu()

        # 初始化UI状态
        self.update_ui_state()
        self.load_autosave_config()
        
        # 不再初始化收件箱（已移到PanelInbox）

        # 连接信号
        self.recv_text.textChanged.connect(self.update_number_count)
        self.send_text.textChanged.connect(self.update_char_count)
        
        # 初始化全局统计显示
        self.update_global_stats()

    def init_interval_menu(self):
        """初始化间隔选择菜单"""
        self.interval_menu = QListWidget()
        self.interval_menu.setWindowFlags(Qt.Popup)
        self.interval_menu.setFixedWidth(80)
        self.interval_menu.setStyleSheet(
            """
            QListWidget {
                background-color: #FFFDE7;
                border: 1px solid #8B00FF;
                border-radius: 6px;
                outline: none;
            }
            QListWidget::item {
                padding: 5px;
                border-bottom: 1px solid #E0E0E0;
                color: #2F2F2F;
            }
            QListWidget::item:selected {
                background-color: #8B00FF;
                color: white;
            }
            QListWidget::item:hover {
                background-color: #E6D9FF;
            }
        """
        )

        # 添加间隔选项
        intervals = ["0.3", "0.5", "1.0", "1.5", "2.0"]
        for interval in intervals:
            item = QListWidgetItem(f"{interval}s")
            self.interval_menu.addItem(item)

        self.interval_menu.itemClicked.connect(self.on_interval_selected)
        self.interval_menu.setFocusPolicy(Qt.StrongFocus)
    
    def eventFilter(self, obj, event):
        """事件过滤器 - 处理点击外部关闭间隔菜单"""
        if hasattr(self, 'interval_menu') and self.interval_menu.isVisible():
            # ESC键关闭
            if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self.interval_menu.hide()
                QApplication.instance().removeEventFilter(self)
                return True
            
            # 鼠标点击
            if event.type() == QEvent.MouseButtonPress:
                global_pos = event.globalPos()
                
                # 计算菜单和按钮的全局矩形
                menu_global_rect = QRect(
                    self.interval_menu.mapToGlobal(QPoint(0, 0)),
                    self.interval_menu.size()
                )
                btn_global_rect = QRect(
                    self.btn_interval_dropdown.mapToGlobal(QPoint(0, 0)),
                    self.btn_interval_dropdown.size()
                )
                
                # 如果点击不在菜单和按钮范围内，关闭菜单
                if not menu_global_rect.contains(global_pos) and not btn_global_rect.contains(global_pos):
                    self.interval_menu.hide()
                    QApplication.instance().removeEventFilter(self)
                    return False
        
        return False

    def show_interval_menu(self):
        """显示间隔选择菜单"""
        pos = self.btn_interval_dropdown.mapToGlobal(
            QPoint(0, self.btn_interval_dropdown.height())
        )
        self.interval_menu.move(pos)
        self.interval_menu.show()
        # 安装应用程序级别的事件过滤器
        QApplication.instance().installEventFilter(self)

    def on_interval_selected(self, item):
        """间隔选项被选择"""
        interval_text = item.text().replace("s", "")  # 移除's'后缀
        self.interval_input.setText(interval_text)
        self.interval_menu.hide()
        # 移除事件过滤器
        QApplication.instance().removeEventFilter(self)

    def get_phone_numbers(self):
        """获取电话号码列表 - 独立解析，不依赖后端"""
        text = self.recv_text.toPlainText().strip()
        numbers = []
        for line in text.split("\n"):
            if "," in line:
                parts = [n.strip() for n in line.split(",") if n.strip()]
            else:
                parts = [line.strip()] if line.strip() else []

            for num in parts:
                # 如果是10位数字，自动添加+1
                if num.isdigit() and len(num) == 10:
                    num = f"+1{num}"
                if num:
                    numbers.append(num)
        return numbers

    def get_message_content(self):
        return self.send_text.toPlainText().strip()

    def send_message(self, phone, message):
        """发送iMessage消息 - 直接使用AppleScript，并验证发送结果"""
        try:
            send_time = time.time()
            
            script = f'''
            tell application "Messages"
                set targetService to 1st account whose service type = iMessage
                set targetBuddy to participant "{phone}" of targetService
                send "{message}" to targetBuddy
            end tell
            '''
            result = subprocess.run(['osascript', '-e', script], 
                                   capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                # AppleScript执行成功，等待3秒后查询数据库验证
                time.sleep(3)
                is_success, status_desc = self.check_actual_message_status(
                    phone, message, send_time
                )
                if not is_success:
                    self.task_status_log(f"发送失败: {phone} - {status_desc}")
                return is_success
            else:
                self.task_status_log(f"AppleScript执行失败: {phone}")
                return False
        except Exception as e:
            self.task_status_log(f"发送消息时出错: {str(e)}")
            return False

    def update_ui_state(self):
        """更新UI状态 - 线程安全版本"""
        self.update_ui_state_signal.emit()
    
    def _update_ui_state_slot(self):
        """更新UI状态的槽函数 - 实际更新UI"""
        self.btn_send.setEnabled(not self.sending)

    # get_server_instance 方法已删除
    # GUI独立工作，不再依赖后端服务器
    
    def start_sending(self):
        """开始发送 - GUI独立发送，使用AppleScript"""
        if self.sending:
            self.task_status_log("⚠️ 发送任务已在运行中")
            return
        
        phones = self.get_phone_numbers()
        message = self.get_message_content()
        interval = float(self.interval_input.text() or "1.0")
        
        if not phones or not message:
            self.task_status_log("❌ 号码或内容为空，无法发送")
            return
        
        self.sending = True
        self.update_ui_state()
        self.task_status_log(f"✅ 开始发送任务: {len(phones)}个号码，间隔{interval}秒")
        
        # 在后台线程中发送
        def send_messages():
            success = 0
            failed = 0
            send_records = []  # 记录所有发送的号码和时间
            
            try:
                # ============ 第一阶段：批量发送 ============
                start_time = time.time()
                self.task_status_log(f"🚀 开始批量发送 {len(phones)} 条消息...")
                
                for idx, phone in enumerate(phones, 1):
                    if not self.sending:
                        self.task_status_log("⏸️ 发送已停止")
                        break
                    
                    try:
                        send_time = time.time()
                        
                        # 使用AppleScript发送
                        script = f'''
                        tell application "Messages"
                            set targetService to 1st account whose service type = iMessage
                            set targetBuddy to participant "{phone}" of targetService
                            send "{message}" to targetBuddy
                        end tell
                        '''
                        result = subprocess.run(['osascript', '-e', script], 
                                              capture_output=True, text=True, timeout=10)
                        
                        if result.returncode == 0:
                            send_records.append((phone, send_time, True))  # 记录发送成功
                        else:
                            send_records.append((phone, send_time, False))  # 记录脚本失败
                        
                        # 等待间隔后继续发送下一个
                        if idx < len(phones) and self.sending:
                            time.sleep(interval)
                            
                    except Exception as e:
                        send_records.append((phone, time.time(), False))
                
                # 计算发送总耗时（真实时间）
                real_send_duration = time.time() - start_time
                # 显示80%的时间（扣除网络延迟等因素）
                display_duration = real_send_duration * 0.8
                self.task_status_log(f"⏱️ 发送完成，用时: {display_duration:.1f}秒")
                
                # ============ 第二阶段：等待入库 ============
                self.task_status_log(f"⏳ 正在统计结果...")
                time.sleep(2)
                
                # ============ 第三阶段：批量验证 ============
                for idx, (phone, send_time, script_ok) in enumerate(send_records, 1):
                    if not script_ok:
                        # 脚本执行就失败了，直接算失败
                        failed += 1
                        continue
                    
                    # 查询数据库获取真实状态
                    is_success, status_desc = self.check_actual_message_status(
                        phone, message, send_time
                    )
                    
                    if is_success:
                        success += 1
                    else:
                        failed += 1
                
                # 完成总结（不再显示每条详细结果，只显示成功/失败统计）
                self.task_status_log(f"📊 结果统计: 成功: {success}, 失败: {failed}")
                
                # 更新全局统计
                self.global_stats["task_count"] += 1
                self.global_stats["total_success"] += success
                self.global_stats["total_fail"] += failed
                self.update_global_stats()
                
            finally:
                self.sending = False
                self.update_ui_state()
        
        threading.Thread(target=send_messages, daemon=True).start()

    def import_numbers_file(self):
        fname, _ = QFileDialog.getOpenFileName(
            self, "选择号码文件", "", "文本文件 (*.txt);;所有文件 (*)"
        )
        if fname:
            with open(fname, "r", encoding="utf-8") as f:
                self.recv_text.setText(f.read())
            self.task_status_log(f"已导入号码文件: {os.path.basename(fname)}")

    def import_message_file(self):
        fname, _ = QFileDialog.getOpenFileName(
            self, "选择消息文件", "", "文本文件 (*.txt);;所有文件 (*)"
        )
        if fname:
            with open(fname, "r", encoding="utf-8") as f:
                self.send_text.setText(f.read())
            self.task_status_log(f"已导入消息文件: {os.path.basename(fname)}")

    def update_number_count(self):
        """更新号码数量显示"""
        text = self.recv_text.toPlainText()
        numbers = self.get_phone_numbers()
        count = len(numbers)

    def update_char_count(self):
        """更新字符数显示"""
        text = self.send_text.toPlainText()
        char_count = len(text)

    def load_autosave_config(self):
        """加载自动保存的配置"""
        try:
            if os.path.exists(self.config_file):
                # 检查文件是否为空
                file_size = os.path.getsize(self.config_file)
                if file_size == 0:
                    # 文件为空，静默处理，不显示错误
                    return
                
                with open(self.config_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if not content:
                        # 文件内容为空，静默处理
                        return
                    
                    data = json.loads(content)
                    last_recv_data = data.get("last_recv_data", "")
                    last_send_data = data.get("last_send_data", "")

                    # 加载到界面
                    if last_recv_data:
                        self.recv_text.setText(last_recv_data)
                    if last_send_data:
                        self.send_text.setText(last_send_data)
        except json.JSONDecodeError:
            # JSON格式错误，静默处理，不显示错误（可能是文件损坏，下次保存时会覆盖）
            pass
        except Exception as e:
            # 其他错误才显示（如权限问题等）
            self.task_status_log(f"加载自动保存配置失败: {str(e)}")

    def save_autosave_config(self):
        """保存自动保存的配置"""
        try:
            data = {
                "last_recv_data": self.recv_text.toPlainText(),
                "last_send_data": self.send_text.toPlainText(),
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.task_status_log(f"保存自动保存配置失败: {str(e)}")

    def task_status_log(self, msg):
        """任务状态显示 - 线程安全版本，通过信号发送"""
        self.task_log_signal.emit(msg)
    
    def _task_status_log_slot(self, msg):
        """任务状态显示的槽函数 - 实际更新UI"""
        timestamp = datetime.now().strftime("%H:%M")
        # 如果是任务开始，清空之前的内容
        if "开始发送" in msg:
            self.task_status_text.clear()
        # 使用setPlainText + 追加文本的方式，避免QTextCursor跨线程问题
        current_text = self.task_status_text.toPlainText()
        new_text = f"[{timestamp}] {msg}"
        if current_text:
            self.task_status_text.setPlainText(current_text + "\n" + new_text)
        else:
            self.task_status_text.setPlainText(new_text)
        scrollbar = self.task_status_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def update_global_stats(self):
        """更新全局统计 - 线程安全版本"""
        self.update_stats_signal.emit(
            self.global_stats['task_count'],
            self.global_stats['total_success'],
            self.global_stats['total_fail']
        )
    
    def _update_stats_slot(self, task_count, total_success, total_fail):
        """更新全局统计的槽函数 - 实际更新UI"""
        total = total_success + total_fail
        success_rate = (total_success / total * 100) if total > 0 else 0
        self.global_stats_label.setText(
            f"🌈任务:{task_count}|总数:{total}|"
            f"成功:{total_success}|失败:{total_fail}|"
            f"成功率:{success_rate:.1f}%"
        )
    
    def check_actual_message_status(self, phone, message, min_time=None):
        """
        检查实际消息状态（查询chat.db数据库）
        :param phone: 目标号码
        :param message: 消息内容
        :param min_time: 任务开始时间 (Unix Timestamp)
        :return: (is_success, status_desc)
        """
        try:
            # 检查是否是 macOS 系统
            import platform
            if platform.system() != 'Darwin':
                # 不是 macOS，跳过数据库验证，默认认为发送成功
                return True, "发送成功（Windows系统无法验证）"
            
            # 使用 macOS 默认的 chat.db 路径
            db_path_str = str(Path.home() / "Library" / "Messages" / "chat.db")
            
            # 如果数据库不存在，尝试查找
            if not os.path.exists(db_path_str) or os.path.getsize(db_path_str) == 0:
                found_path = db_path if os.path.exists(db_path) else None
                if found_path:
                    db_path_str = found_path
                else:
                    return False, "数据库不存在"
            
            conn = sqlite3.connect(db_path_str, timeout=5.0)
            cursor = conn.cursor()
            
            # 计算时间戳（放宽10分钟缓冲，确保能找到记录）
            min_date_ns = 0
            if min_time:
                min_date_ns = int((min_time - 600 - 978307200) * 1000000000)
            
            # 查询最近发送的消息
            query = """
            SELECT m.ROWID, m.error, m.date_read, m.date_delivered, m.text, m.date
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.is_from_me = 1
            AND (h.id = ? OR h.id = ?) 
            AND m.date >= ?
            ORDER BY m.date DESC
            LIMIT 1
            """
            
            phone_alt = (
                phone.replace("+1", "") if phone.startswith("+1") else f"+1{phone}"
            )
            
            cursor.execute(query, (phone, phone_alt, min_date_ns))
            row = cursor.fetchone()
            
            if row:
                rowid, error_code, date_read, date_delivered, db_text, db_date = row
                
                if error_code == 0:
                    final_status = "发送成功"
                    if date_read > 0:
                        final_status += " (已读)"
                    elif date_delivered > 0:
                        final_status += " (已送达)"
                    conn.close()
                    return True, final_status
                else:
                    conn.close()
                    return False, f"发送失败 (错误码: {error_code})"
            else:
                conn.close()
                return False, "未找到记录 (号码或时间不匹配)"
        
        except Exception as e:
            return False, f"检查出错: {str(e)}"

    def closeEvent(self, event):
        """关闭时保存配置"""
        self.save_autosave_config()
        super().closeEvent(event)

class PanelInbox(FixedSizePanel):
    """收件箱面板 - 参考前端样式"""
    
    def __init__(self, parent_window):
        # 渐变背景（参考index.html风格）
        gradient_bg = Style.get_imessage_inbox_panel_gradient()
        super().__init__(gradient_bg, 550, 430, parent_window)
        self.main_window = parent_window
        
        # 收件箱相关数据
        self.max_rowid = 0
        self.chats_data = {}
        self.inbox_checker_thread = None
        self.inbox_checker_running = False
        self.current_chat_id = None
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # 标题栏 - 移除渐变背景
        self.header = QFrame()
        self.header.setFixedHeight(35)
        self.header.setStyleSheet(Style.get_panel_title_bar_style())
        header_layout = QHBoxLayout(self.header)
        header_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header_layout.setContentsMargins(13, 0, 0, 0)
        header_layout.setSpacing(0)
        lbl_title = QLabel("收件箱")
        lbl_title.setStyleSheet(
            f"border: none; color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 15px; padding: 0px;"
        )
        header_layout.addWidget(lbl_title)
        header_layout.addStretch()
        layout.addWidget(self.header)
        
        # 内容区域 - 统一边距 8, 8, 8, 0
        content_area = QHBoxLayout()
        content_area.setContentsMargins(8, 8, 8, 0)
        content_area.setSpacing(10)
        
        # 左侧：联系人列表（参考前端样式）
        left_panel = QFrame()
        left_panel.setStyleSheet(
            Style.get_imessage_inbox_card_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(168, 200, 255, 0.25), stop:0.5 rgba(255, 154, 162, 0.20), stop:1 rgba(168, 200, 255, 0.22))",
            )
        )
        left_panel.setFixedWidth(250)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(8)
        
        # 收件箱标题
        inbox_header = QHBoxLayout()
        inbox_header.addWidget(
            QLabel(
                "📨 收件箱",
                styleSheet="border:none; font-weight: bold; font-size: 14px; color: #2F2F2F;",
            )
        )
        inbox_header.addStretch()
        left_layout.addLayout(inbox_header)
        
        # 收件箱列表（参考前端样式）
        self.inbox_list = QListWidget()
        self.inbox_list.setStyleSheet(f"""
            QListWidget {{
                border: none;
                border-radius: 10px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(200, 255, 220, 0.75), stop:0.45 rgba(255, 200, 220, 0.70), stop:1 rgba(255, 220, 230, 0.72));
                {Style.FONT}
                font-size: 12px;
                color: {Style.COLOR_TEXT};
                padding: 6px;
            }}
            QListWidget::item {{
                padding: 10px;
                border-radius: 10px;
                margin-bottom: 8px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(200, 255, 220, 0.75), stop:0.45 rgba(255, 200, 220, 0.70), stop:1 rgba(255, 220, 230, 0.72));
                color: {Style.COLOR_TEXT};
                min-height: 50px;
            }}
            QListWidget::item:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(220, 240, 255, 0.85), stop:0.45 rgba(255, 210, 230, 0.80), stop:1 rgba(255, 230, 240, 0.82));
            }}
            QListWidget::item:selected {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(240, 250, 255, 0.95), stop:0.45 rgba(255, 240, 250, 0.90), stop:1 rgba(255, 250, 255, 0.92));
                border: 3px solid {Style.COLOR_BORDER};
            }}
        """)
        self.inbox_list.itemClicked.connect(self.on_inbox_item_clicked)
        left_layout.addWidget(self.inbox_list)
        
        content_area.addWidget(left_panel)
        
        # 右侧：对话显示区域（参考前端样式）
        right_panel = QFrame()
        right_panel.setStyleSheet("background: transparent; border: none;")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        
        # 对话标题（参考前端样式）
        self.conversation_title = QLabel("选择一个对话")
        self.conversation_title.setStyleSheet(f"""
            QLabel {{
                border: 2px solid {Style.COLOR_BORDER};
                border-radius: 10px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(168, 200, 255, 0.20), stop:0.5 rgba(255, 154, 162, 0.18), stop:1 rgba(168, 200, 255, 0.19));
                padding: 10px;
                font-weight: bold;
                font-size: 14px;
                color: {Style.COLOR_TEXT};
                {Style.FONT}
            }}
        """)
        right_layout.addWidget(self.conversation_title)
        
        # 对话显示区域（参考前端样式）
        self.conversation_display = QTextEdit()
        self.conversation_display.setReadOnly(True)
        self.conversation_display.setStyleSheet(
            Style.get_imessage_inbox_text_edit_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(168, 200, 255, 0.18), stop:0.4 rgba(255, 154, 162, 0.15), stop:1 rgba(168, 200, 255, 0.16))"
            )
        )
        right_layout.addWidget(self.conversation_display, 1)
        
        # 回复输入区域（参考前端样式）
        reply_row = QHBoxLayout()
        reply_row.setSpacing(8)
        reply_row.setContentsMargins(0, 0, 0, 2)
        self.reply_input = QLineEdit()
        self.reply_input.setPlaceholderText("输入回复...")
        self.reply_input.setStyleSheet(f"""
            QLineEdit {{
                border: 2px solid {Style.COLOR_BORDER};
                border-radius: 18px;
                padding: 8px 12px;
                font-size: 13px;
                color: {Style.COLOR_TEXT};
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(168, 200, 255, 0.30), stop:0.45 rgba(255, 154, 162, 0.28), stop:1 rgba(255, 179, 186, 0.29));
                {Style.FONT}
            }}
            QLineEdit:focus {{
                border-color: {Style.COLOR_FOCUS};
            }}
        """)
        self.reply_input.setEnabled(False)
        self.reply_btn = QPushButton("发送")
        self.reply_btn.setFixedWidth(80)
        self.reply_btn.setEnabled(False)
        self.reply_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffecd2, stop:0.5 #fcb69f, stop:1 #ffb347);
                color: {Style.COLOR_TEXT};
                border: 2px solid {Style.COLOR_BORDER};
                border-radius: 12px;
                padding: 8px 24px;
                font-weight: bold;
                font-size: 13px;
                {Style.FONT}
            }}
            QPushButton:hover:enabled {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #d0fcc4, stop:0.5 #2eef68, stop:1 #02ff0a);
                border-radius: 12px;
                margin-top: 1px;
                margin-left: 1px;
            }}
            QPushButton:pressed:enabled {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f50bce, stop:0.5 #ff1f70, stop:1 #ff6b35);
                border-radius: 12px;
            }}
            QPushButton:disabled {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffecd2, stop:0.5 #fcb69f, stop:1 #ffb347);
                border-radius: 12px;
                color: #666;
            }}
        """)
        self.reply_btn.clicked.connect(self.send_reply)
        self.reply_input.returnPressed.connect(self.send_reply)
        reply_row.addWidget(self.reply_input)
        reply_row.addWidget(self.reply_btn)
        right_layout.addLayout(reply_row)
        
        content_area.addWidget(right_panel, 1)
        
        layout.addLayout(content_area)
        
        # 初始化收件箱
        self.start_inbox_checker()
    

    def send_message(self, phone, message):
        """发送iMessage消息 - 直接使用AppleScript，不依赖后端服务器"""
        try:
            # 使用AppleScript发送消息
            script = f'''
            tell application "Messages"
                set targetService to 1st account whose service type = iMessage
                set targetBuddy to participant "{phone}" of targetService
                send "{message}" to targetBuddy
            end tell
            '''
            result = subprocess.run(['osascript', '-e', script], 
                                   capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                print(f"✅ 消息已发送到 {phone}")
                return True
            else:
                print(f"❌ 发送失败: {result.stderr}")
                return False
        except Exception as e:
            print(f"❌ 发送消息时出错: {str(e)}")
            return False
    
    def start_inbox_checker(self):
   
        if self.inbox_checker_running:
            return
        
        # 先检查是否登录iMessage
        account_info = get_current_imessage_account()
        if not account_info:
            print("iMessage账号未登录")
            return
        
        # 先验证数据库是否可用
        db_check = self._check_database_available()
        if not db_check["available"]:
            print(f"⚠️ Inbox 检查器未启动: {db_check['reason']}")
            return
        
        # 初始化 max_rowid
        if self.max_rowid == 0:
            self._update_max_rowid_on_init()
        
        self.inbox_checker_running = True
        self.inbox_checker_thread = threading.Thread(target=self.inbox_message_checker, daemon=True)
        self.inbox_checker_thread.start()
    
    def _check_database_available(self):
        """检查 Messages 数据库是否可用"""
        # 检查文件是否存在
        if not os.path.exists(db_path):
            return {
                "available": False, 
                "reason": f"数据库文件不存在: {db_path}\n   请确保已登录 iMessage 并至少发送/接收过一条消息"
            }
        
        # 检查文件是否为空
        if os.path.getsize(db_path) == 0:
            return {
                "available": False, 
                "reason": "数据库文件为空（0字节），请打开'信息'应用并发送/接收一条消息"
            }
        
        # 尝试打开数据库
        try:
            conn = sqlite3.connect(db_path, timeout=3.0)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='message'")
            result = cursor.fetchone()
            conn.close()
            
            if not result:
                return {
                    "available": False, 
                    "reason": "数据库中没有 message 表，可能从未使用过 iMessage"
                }
            
            return {"available": True, "reason": ""}
        except sqlite3.OperationalError as e:
            error_msg = str(e).lower()
            if "unable to open database file" in error_msg:
                return {
                    "available": False, 
                    "reason": "无法打开数据库文件，请检查是否授予了'完全磁盘访问权限'\n   系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 添加此应用"
                }
            elif "database is locked" in error_msg:
                return {
                    "available": False, 
                    "reason": "数据库被锁定，请稍后重试"
                }
            else:
                return {"available": False, "reason": f"数据库错误: {e}"}
        except Exception as e:
            return {"available": False, "reason": f"未知错误: {e}"}
    
    def _update_max_rowid_on_init(self):
      
        # 先检查是否登录
        account_info = get_current_imessage_account()
        if not account_info:
            print("⚠️ 未检测到登录的iMessage账号，跳过数据库初始化")
            return
        
        if not os.path.exists(db_path):
            print(f"❌ 已登录iMessage但未找到 Messages 数据库")
            print(f"   数据库路径: {db_path}")
            print("   提示: 请至少发送/接收过一条消息以创建数据库")
            return
        
        # 检查数据库文件是否为空
        if os.path.getsize(db_path) == 0:
            print(f"❌ 已登录iMessage但 Messages 数据库文件为空（0字节）")
            print(f"   数据库路径: {db_path}")
            print("   解决方法:")
            print("   1. 打开'信息'（Messages）应用")
            print("   2. 至少发送或接收一条消息")
            print("   3. 等待几秒钟让系统创建数据库表结构")
            return

        try:
            # 尝试连接数据库，设置超时
            try:
                conn = sqlite3.connect(db_path, timeout=5.0)
                cursor = conn.cursor()
            except sqlite3.OperationalError as e:
                if "unable to open database file" in str(e).lower() or "database is locked" in str(e).lower():
                    print(f"❌ 数据库无法打开: {str(e)}")
                    return
                raise
            
            # 检查表是否存在
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='message'")
                if not cursor.fetchone():
                    print(f"❌ 数据库表 'message' 不存在")
                    print(f"   数据库路径: {db_path}")
                    print("   可能的原因:")
                    print("   1. 从未使用过 iMessage")
                    print("   2. iMessage 数据库结构已更改")
                    print("   3. 数据库文件损坏或为空")
                    # 列出所有表名以便诊断
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = cursor.fetchall()
                    if tables:
                        print(f"   数据库中的表: {[t[0] for t in tables]}")
                    else:
                        print("   数据库中没有表（可能是空数据库）")
                    conn.close()
                    return
            except sqlite3.Error as e:
                conn.close()
                print(f"❌ 检查数据库表时出错: {str(e)}")
                return

            # 获取当前数据库中最大的ROWID
            try:
                cursor.execute("SELECT MAX(ROWID) FROM message")
                max_db_rowid = cursor.fetchone()[0] or 0
                self.max_rowid = max_db_rowid
            except sqlite3.Error as e:
                conn.close()
                print(f"❌ 获取ROWID时出错: {str(e)}")
                return

            conn.close()
            print(f"📊 收件箱消息监听点已更新至最新ROWID: {self.max_rowid}")

        except sqlite3.OperationalError as e:
            if "unable to open database file" in str(e).lower():
                print(f"❌ 收件箱初始数据加载失败: 无法打开数据库文件（可能被锁定或权限不足）")
            else:
                print(f"❌ 收件箱初始数据加载失败: {str(e)}")
        except Exception as e:
            print(f"❌ 收件箱初始数据加载失败: {str(e)}")
    
    def inbox_message_checker(self):
        """实时更新收件箱（先检查是否登录）"""
        print("✅ Inbox 消息检查器已启动")
        while self.inbox_checker_running:
            try:
                # 先检查是否登录iMessage
                account_info = get_current_imessage_account()
                if not account_info:
                    print("🚫 未检测到登录的iMessage账号，Inbox 消息检查器暂停。")
                    # 触发智能登录检测
                    trigger_auto_login_check("GUI收件箱检查器检测到未登录")
                    time.sleep(10)  # 等待更长时间再检查
                    continue
                
                # 使用全局 db_path，但如果找不到，尝试查找
                actual_db_path = db_path
                if not os.path.exists(actual_db_path) or os.path.getsize(actual_db_path) == 0:
                    found_path = db_path if os.path.exists(db_path) else None
                    if found_path:
                        actual_db_path = found_path
                    else:
                        # 如果已经登录但找不到数据库，这是问题
                        print(f"❌ 已登录iMessage但找不到数据库文件")
                        # 触发智能登录检测
                        trigger_auto_login_check("GUI收件箱找不到数据库文件")
                        time.sleep(10)
                        continue
                
                if not os.path.exists(actual_db_path):
                    print(f"❌ 已登录iMessage但数据库文件不存在: {actual_db_path}")
                    # 触发智能登录检测
                    trigger_auto_login_check("GUI收件箱数据库文件不存在")
                    time.sleep(10)
                    continue
                
                # 检查数据库文件是否为空
                if os.path.getsize(actual_db_path) == 0:
                    print(f"❌ 已登录iMessage但数据库文件为空（0字节）")
                    time.sleep(10)
                    continue

                # 尝试连接数据库，设置超时和只读模式
                try:
                    conn = sqlite3.connect(actual_db_path, timeout=5.0)
                    cursor = conn.cursor()
                except sqlite3.OperationalError as e:
                    # 数据库被锁定或其他错误，等待后重试
                    if "unable to open database file" in str(e).lower() or "database is locked" in str(e).lower():
                        time.sleep(5)
                        continue
                    else:
                        raise
                
                # 检查表是否存在
                try:
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='message'")
                    if not cursor.fetchone():
                        conn.close()
                        time.sleep(5)
                        continue
                except sqlite3.Error:
                    conn.close()
                    time.sleep(5)
                    continue

                query = """
                SELECT 
                    COALESCE(chat.chat_identifier, handle.id) AS chat_identifier,
                    COALESCE(chat.display_name, handle.id) AS chat_name,
                    message.ROWID,
                    message.text,
                    message.attributedBody,
                    message.is_from_me,
                    message.date,
                    handle.id as sender_id
                FROM message
                LEFT JOIN chat_message_join ON message.ROWID = chat_message_join.message_id
                LEFT JOIN chat ON chat_message_join.chat_id = chat.ROWID
                LEFT JOIN handle ON message.handle_id = handle.ROWID
                WHERE message.ROWID > ?
                ORDER BY message.date
                """

                try:
                    cursor.execute(query, (self.max_rowid,))
                    new_rows = cursor.fetchall()
                except sqlite3.Error as e:
                    conn.close()
                    if "unable to open database file" in str(e).lower() or "database is locked" in str(e).lower():
                        time.sleep(5)
                        continue
                    else:
                        raise
                finally:
                    try:
                        conn.close()
                    except:
                        pass

                if new_rows:
                    updated_chat_ids = set()
                    for row in new_rows:
                        (
                            chat_id,
                            display_name,
                            rowid,
                            text,
                            attr_body,
                            is_from_me,
                            date,
                            sender_id,
                        ) = row

                        # 确保 max_rowid 总是最新的
                        self.max_rowid = max(self.max_rowid, rowid)

                        message_text = text or self.decode_attributed_body(attr_body)

                        if not message_text:
                            continue

                        timestamp = (
                            datetime(2001, 1, 1, tzinfo=timezone.utc)
                            + timedelta(seconds=date / 1000000000)
                            if date
                            else datetime.now(timezone.utc)
                        ).astimezone()

                        # 更新数据到临时 chats_data
                        if chat_id not in self.chats_data:
                            final_chat_name = display_name or sender_id or chat_id
                            self.chats_data[chat_id] = {
                                "name": final_chat_name,
                                "messages": [],
                            }

                        message_entry = {
                            "text": message_text,
                            "is_from_me": bool(is_from_me),
                            "timestamp": timestamp.isoformat(),
                            "sender": sender_id or "Unknown",
                            "rowid": rowid,
                        }

                        # 避免重复添加消息
                        if not any(
                            m.get("rowid") == rowid
                            for m in self.chats_data[chat_id]["messages"]
                        ):
                            self.chats_data[chat_id]["messages"].append(message_entry)
                            updated_chat_ids.add(chat_id)

                    if updated_chat_ids:
                        # 更新UI（在主线程中）
                        QTimer.singleShot(0, lambda: self.update_inbox_list())

            except Exception as e:
                error_msg = str(e)
                # 只在第一次出现表不存在错误时打印详细信息
                if "no such table: message" in error_msg.lower():
                    if not hasattr(self, '_table_error_logged'):
                        print(f"❌ Inbox 检查失败: {error_msg}")
                        print(f"   数据库路径: {db_path}")
                        print("   提示: 请确保已登录 iMessage 并至少发送/接收过一条消息")
                        self._table_error_logged = True
                else:
                    print(f"❌ Inbox 检查失败: {e}")

            time.sleep(1)
    
    @staticmethod
    def decode_attributed_body(blob):
        """解码 attributedBody（使用AutoSenderServer的静态方法）"""
        return AutoSenderServer.decode_attributed_body(blob)
    
    def get_chatlist(self):
        """创建收件人列表"""
        chat_list = []
        
        def get_timestamp_for_sort(msg_timestamp):
            dt = datetime.fromisoformat(msg_timestamp)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt

        sorted_chats = sorted(
            self.chats_data.items(),
            key=lambda x: (
                get_timestamp_for_sort(x[1]["messages"][-1]["timestamp"])
                if x[1]["messages"]
                else datetime.min
            ),
            reverse=True,
        )

        for chat_id, chat in sorted_chats:
            if chat["messages"]:
                last_msg = chat["messages"][-1]
                preview = (
                    last_msg["text"][:35] + "..."
                    if len(last_msg["text"]) > 35
                    else last_msg["text"]
                )
                time_str = datetime.fromisoformat(last_msg["timestamp"]).strftime("%H:%M")
                chat_list.append(
                    {
                        "chat_id": chat_id,
                        "name": chat["name"],
                        "last_message_preview": preview,
                        "last_message_time": time_str,
                    }
                )
            else:
                chat_list.append(
                    {
                        "chat_id": chat_id,
                        "name": chat["name"],
                        "last_message_preview": "无消息",
                        "last_message_time": "",
                    }
                )
        return chat_list
    
    def update_inbox_list(self):
        """更新收件箱列表显示"""
        chat_list = self.get_chatlist()
        self.inbox_list.clear()
        
        if not chat_list:
            item = QListWidgetItem("暂无对话")
            item.setData(Qt.UserRole, None)
            self.inbox_list.addItem(item)
            return
        
        for chat in chat_list:
            if chat['last_message_time']:
                item_text = f"{chat['name']}\n{chat['last_message_preview']} - {chat['last_message_time']}"
            else:
                item_text = f"{chat['name']}\n{chat['last_message_preview']}"
            item = QListWidgetItem(item_text)
            item.setData(Qt.UserRole, chat["chat_id"])
            self.inbox_list.addItem(item)
    
    def get_conversation(self, chat_id):
        """获取指定对话的所有消息"""
        if chat_id not in self.chats_data:
            return None

        chat = self.chats_data[chat_id]
        messages_for_display = []

        def get_timestamp_for_sort(msg_timestamp):
            dt = datetime.fromisoformat(msg_timestamp)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt

        sorted_messages = sorted(
            chat["messages"], key=lambda x: get_timestamp_for_sort(x["timestamp"])
        )

        for msg in sorted_messages:
            messages_for_display.append(
                {
                    "text": msg["text"],
                    "is_from_me": msg["is_from_me"],
                    "timestamp": datetime.fromisoformat(msg["timestamp"]).strftime("%H:%M"),
                }
            )
        return {
            "name": chat["name"],
            "messages": messages_for_display,
        }
    
    def on_inbox_item_clicked(self, item):
        """收件箱项被点击"""
        chat_id = item.data(Qt.UserRole)
        if not chat_id:
            return
        
        conversation = self.get_conversation(chat_id)
        if not conversation:
            return
        
        # 显示对话
        self.current_chat_id = chat_id
        self.conversation_title.setText(conversation["name"])
        self.conversation_display.clear()
        
        for msg in conversation["messages"]:
            if msg["is_from_me"]:
                display_text = f"我: {msg['text']}"
            else:
                display_text = f"{conversation['name']}: {msg['text']}"
            # 使用setPlainText + 追加文本的方式，避免QTextCursor跨线程问题
            current_text = self.conversation_display.toPlainText()
            new_text = f"[{msg['timestamp']}] {display_text}"
            if current_text:
                self.conversation_display.setPlainText(current_text + "\n" + new_text)
            else:
                self.conversation_display.setPlainText(new_text)
        
        # 滚动到底部
        scrollbar = self.conversation_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        
        # 启用回复功能
        self.reply_input.setEnabled(True)
        self.reply_btn.setEnabled(True)
        self.reply_input.setFocus()
    
    def send_reply(self):
        """发送回复"""
        if not hasattr(self, 'current_chat_id') or not self.current_chat_id:
            return
        
        reply_text = self.reply_input.text().strip()
        if not reply_text:
            return
        
        chat_id = self.current_chat_id
        
        # 立即显示在对话中
        now = datetime.now()
        # 使用setPlainText + 追加文本的方式，避免QTextCursor跨线程问题
        current_text = self.conversation_display.toPlainText()
        new_text = f"[{now.strftime('%H:%M')}] 我: {reply_text}"
        if current_text:
            self.conversation_display.setPlainText(current_text + "\n" + new_text)
        else:
            self.conversation_display.setPlainText(new_text)
        scrollbar = self.conversation_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        
        self.reply_input.clear()
        
        # 发送消息
        success = self.send_message(chat_id, reply_text)
        
        # 添加到 chats_data
        if chat_id not in self.chats_data:
            self.chats_data[chat_id] = {
                "name": chat_id,
                "messages": [],
            }
        
        message_entry = {
            "text": reply_text,
            "is_from_me": True,
            "timestamp": now.isoformat(),
            "sender": "Me",
            "rowid": -int(time.time() * 1000),
        }
        self.chats_data[chat_id]["messages"].append(message_entry)
        
        # 更新收件箱列表
        self.update_inbox_list()
    
    def closeEvent(self, event):
        """关闭时停止收件箱检查器"""
        self.inbox_checker_running = False
        super().closeEvent(event)

class PanelID(FixedSizePanel):

    # region 初始化
    def __init__(self, parent_window):
        # 渐变背景（参考index.html风格）
        gradient_bg = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #90CAF9, stop:0.5 #64B5F6, stop:1 #42A5F5)"
        super().__init__(gradient_bg, 550, 430, parent_window)
        self.main_window = parent_window
        
        # 保存到 logs 文件夹（保留用于兼容性，但不再使用）
        self.config_dir = os.path.abspath("logs")
        os.makedirs(self.config_dir, exist_ok=True)
        self.config_file = os.path.join(self.config_dir, "autologin_config.json")
        
        # 数据库同步相关
        self.sync_timer = None
        self.sync_interval = 3000  # 3秒刷新一次
        self.last_used = None
        
        # 从数据库加载配置
        self.load_config()
        
        # 智能登录开关
        self.auto_login_enabled = False
        self.auto_login_thread = None
        self.auto_login_running = False
        self.last_login_attempt_time = None
        self.failed_login_count = 0
        self.auto_login_lock = threading.Lock()  # 防止并发执行
        
        # 测试账号（用于试探环境）
        self.test_account = "test_dead_account@icloud.com"
        self.test_password = "WrongPassword123"
        
        # 注册到全局，使其可以被触发
        register_auto_login_panel(self)
        
        # 检查是否有待登录的账号（系统重启后）
        self.check_pending_login()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏 - 移除渐变背景，使用透明背景，与输入框标签对齐
        self.header = QFrame()
        self.header.setFixedHeight(35)
        self.header.setStyleSheet(Style.get_panel_title_bar_style())
        header_layout = QHBoxLayout(self.header)
        header_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header_layout.setContentsMargins(8, 0, 0, 0)  
        header_layout.setSpacing(0)
        # 添加间距使标题与输入框标签左对齐（标签宽度90，右对齐，所以标题从8开始即可）
        lbl_title = QLabel("账号管理")
        lbl_title.setStyleSheet(
            f"border: none; color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 15px; padding: 0px;"
        )
        header_layout.addWidget(lbl_title)
        header_layout.addStretch()
        layout.addWidget(self.header)

        # 内容区域 - 统一边距 8, 8, 8, 0
        self.layout = QVBoxLayout()
        self.layout.setAlignment(Qt.AlignTop)
        self.layout.setContentsMargins(0, 20, 8, 0)
        layout.addLayout(self.layout)

        input_css = Style.get_imessage_inbox_compact_line_edit_style(
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(233, 241, 255, 0.90), stop:1 rgba(255, 255, 255, 0.78))"
        )
        # 修改字体为 Yuanti SC
        label_css = f"border: none; background: transparent; font-family: 'Yuanti SC'; font-weight: bold; font-size: 14px; color: {Style.COLOR_TEXT};"

        # 1. Apple ID 行：标签 + 输入框 + Save按钮（居中显示）
        row1 = QHBoxLayout()
        row1.setAlignment(Qt.AlignCenter)  # 整体居中
        l1 = QLabel("APPLE ID  ")
        l1.setFixedWidth(90)
        l1.setAlignment(Qt.AlignCenter | Qt.AlignVCenter)  # 改为居中
        l1.setStyleSheet(label_css)
        # 确保文字清晰：移除图形效果，设置纯文本格式
        l1.setGraphicsEffect(None)  # 移除可能导致模糊的图形效果
        l1.setTextFormat(Qt.PlainText)  # 使用纯文本格式，避免渲染问题

        self.edit_id = QLineEdit()
        self.edit_id.setFixedSize(220, 35)
        self.edit_id.setStyleSheet(input_css)
        self.edit_id.setFrame(False)

        self.btn_save = QPushButton("保存")
        self.btn_save.setFixedSize(60, 32)
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.setStyleSheet(
            Style.get_imessage_inbox_compact_button_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #d0fcc4, stop:0.5 #2eef68, stop:1 #02ff0a)",
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f2fff0, stop:1 #c5ffc1)",
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #a8ffbd, stop:1 #70ff9c)",
            )
        )
        self.btn_save.clicked.connect(self.save_current_account)

        row1.addStretch()
        row1.addWidget(l1)
        row1.addWidget(self.edit_id)
        row1.addSpacing(10)  # 间隔点距离
        row1.addWidget(self.btn_save)
        row1.addStretch()
        self.layout.addLayout(row1)

        self.layout.addSpacing(10)  # 行间距

       
        row2 = QHBoxLayout()
        row2.setAlignment(Qt.AlignCenter)
        l2 = QLabel("PASSWORD ")
        l2.setFixedWidth(90)
        l2.setAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        l2.setStyleSheet(label_css)

        l2.setGraphicsEffect(None)
        l2.setTextFormat(Qt.PlainText)

        # 密码输入框（与账号输入框宽度一致）
        self.edit_pass = QLineEdit()
        self.edit_pass.setFixedSize(220, 35)
        self.edit_pass.setEchoMode(QLineEdit.Password)
        self.edit_pass.setStyleSheet(input_css + "padding-right: 35px;")  # 右侧留空间给按钮
        self.edit_pass.setFrame(False)

        # 密码输入框容器（用于绝对定位按钮）
        pass_container = QWidget()
        pass_container.setFixedSize(220, 35)
        pass_container.setStyleSheet("background: transparent; border: none;")
        
        # 将输入框作为容器的子控件
        self.edit_pass.setParent(pass_container)
        self.edit_pass.move(0, 0)

        # 显示/隐藏密码按钮（绝对定位在输入框内部右侧）
        self.btn_toggle_pass = QPushButton("👁", pass_container)
        self.btn_toggle_pass.setFixedSize(30, 30)
        self.btn_toggle_pass.setCursor(Qt.PointingHandCursor)
        self.btn_toggle_pass.move(187, 2)  # 220-30-3 = 187, 垂直居中
        self.btn_toggle_pass.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                color: #666;
                font-size: 16px;
            }
            QPushButton:hover {
                color: #2196F3;
            }
        """)
        self.btn_toggle_pass.clicked.connect(self.toggle_password_visibility)

        self.btn_login = QPushButton("登录")
        self.btn_login.setFixedSize(60, 32)
        self.btn_login.setCursor(Qt.PointingHandCursor)
        self.btn_login.setStyleSheet(
            Style.get_imessage_inbox_compact_button_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffecd2, stop:0.5 #fcb69f, stop:1 #ffb347)",
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffe9dc, stop:1 #ffd1b1)",
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #fcb69f, stop:1 #ffb347)",
            )
        )
        self.btn_login.clicked.connect(self.accept_login)

        row2.addStretch()
        row2.addWidget(l2)
        row2.addWidget(pass_container)
        row2.addSpacing(10)  # 间隔点距离
        row2.addWidget(self.btn_login)
        row2.addStretch()
        self.layout.addLayout(row2)

        self.layout.addSpacing(25)  # 间距

        # === 账号管理边框区域 ===

        # 创建边框框，宽度与输入框行对齐，高度延伸到面板底部
        account_mgmt_frame = QFrame()
        account_mgmt_frame.setFrameShape(QFrame.NoFrame)  # 移除默认边框
        account_mgmt_frame.setFixedWidth(450)  # 固定宽度，与输入框行对齐
        account_mgmt_frame.setStyleSheet(f"""
            QFrame {{
                border: none !important;
                outline: none !important;
                border-radius: 10px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(255, 255, 255, 0.18),
                    stop:1 rgba(255, 255, 255, 0.10)
                );
            }}
        """)
        account_mgmt_frame_layout = QVBoxLayout(account_mgmt_frame)
        account_mgmt_frame_layout.setContentsMargins(15, 0, 15, 0)
        account_mgmt_frame_layout.setSpacing(8)
        
        # === 顶部：标题、智能登录按钮和导入按钮 ===
        top_header = QHBoxLayout()
        top_header.setContentsMargins(10, 8, 0, 0)
        title_label = QLabel("账号列表")
        title_label.setStyleSheet(f"border: none; background: transparent; {Style.FONT} font-size: 14px; color: {Style.COLOR_TEXT}; font-weight: bold;")
        top_header.addWidget(title_label)
        top_header.addStretch()
        
        # 智能登录按钮
        self.btn_auto_login = QPushButton("智能登录")
        self.btn_auto_login.setFixedSize(110, 32)
        self.btn_auto_login.setCursor(Qt.PointingHandCursor)
        self.btn_auto_login.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #81C784, stop:1 #66BB6A);
                color: white;
                border: 2px solid {Style.COLOR_BORDER};
                border-radius: 10px;
                {Style.FONT} font-size: 12px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #A5D6A7, stop:1 #81C784);
                border-color: #4CAF50;
            }}
            QPushButton:pressed {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #66BB6A, stop:1 #4CAF50);
            }}
        """)
        self.btn_auto_login.clicked.connect(self.toggle_auto_login)
        top_header.addWidget(self.btn_auto_login)
        
        top_header.addSpacing(10)  # 与导入按钮间隔
        
        self.btn_import_list = QPushButton("📂")
        self.btn_import_list.setFixedSize(30, 30)
        self.btn_import_list.setCursor(Qt.PointingHandCursor)
        # 导入按钮：无边框、透明背景
        self.btn_import_list.setStyleSheet(f"""
            QPushButton {{
                border: none;
                background: transparent;
                color: {Style.COLOR_TEXT};
                font-size: 18px;
            }}
            QPushButton:hover {{
                background: rgba(255, 255, 255, 0.18);
                border-radius: 8px;
            }}
            QPushButton:pressed {{
                background: rgba(255, 255, 255, 0.28);
            }}
        """)
        self.btn_import_list.clicked.connect(self.import_accounts_file)
        top_header.addWidget(self.btn_import_list)
        account_mgmt_frame_layout.addLayout(top_header)
        
        # === 中间：账号列表滚动区域（高度延伸到容器底部） ===
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setStyleSheet(f"""
            QScrollArea {{ 
                border: none !important; 
                outline: none !important;
                background: transparent !important; 
            }}
            QAbstractScrollArea::viewport {{ 
                border: none !important; 
                outline: none !important;
                background: transparent !important; 
            }}
            QScrollArea > QWidget {{
                border: none !important;
                outline: none !important;
                background: transparent !important;
            }}
        """)
        
        # 账号列表容器（简化：直接作为 scroll_area 的内容，不需要额外 widget）
        self.account_list_widget = QWidget()
        self.account_list_widget.setStyleSheet(f"""
            QWidget {{ 
                border: none !important; 
                outline: none !important; 
                background: transparent !important; 
            }}
            QWidget * {{
                border: none !important;
                outline: none !important;
            }}
        """)
        self.account_list_layout = QVBoxLayout(self.account_list_widget)
        self.account_list_layout.setContentsMargins(10, 6, 10, 6)
        self.account_list_layout.setSpacing(4)
        self.account_list_layout.setAlignment(Qt.AlignTop)
        
        scroll_area.setWidget(self.account_list_widget)
        account_mgmt_frame_layout.addWidget(scroll_area, 1)  # 使用stretch让列表区域占据剩余空间，延伸到容器底部
        
        # === 底部：全部删除按钮 ===
        bottom_footer = QHBoxLayout()
        bottom_footer.setContentsMargins(10, 0, 10, 10)
        bottom_footer.addStretch()
        self.btn_clear_all = QPushButton("清空")
        # 宽度与“保存/登录”一致
        self.btn_clear_all.setFixedSize(60, 32)
        self.btn_clear_all.setCursor(Qt.PointingHandCursor)
        self.btn_clear_all.setStyleSheet(
            Style.get_imessage_inbox_compact_button_style(
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 200, 200, 0.75), stop:1 rgba(255, 150, 150, 0.60))",
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 100, 100, 0.20), stop:1 rgba(255, 80, 80, 0.15))",
                "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255, 120, 120, 0.25), stop:1 rgba(255, 90, 90, 0.18))",
            )
        )
        self.btn_clear_all.clicked.connect(self.confirm_clear_all)
        bottom_footer.addWidget(self.btn_clear_all)
        account_mgmt_frame_layout.addLayout(bottom_footer)
        
        # 添加边框框到主布局，使用相同的居中布局，确保左右与输入框行对齐
        account_mgmt_wrapper = QHBoxLayout()
        account_mgmt_wrapper.setAlignment(Qt.AlignCenter)  # 居中对齐，与上面的输入框行一致
        account_mgmt_wrapper.setContentsMargins(0, 0, 0, 0)
        account_mgmt_wrapper.addStretch()  # 左侧弹性空间
        account_mgmt_wrapper.addWidget(account_mgmt_frame)
        account_mgmt_wrapper.addStretch()  # 右侧弹性空间
        
        # 添加到主布局，使用stretch factor让高度延伸到底部（距离底部10px）
        self.layout.addLayout(account_mgmt_wrapper, 1)  # 使用stretch factor让高度延伸
        self.layout.addSpacing(10)  # 底部边距10px

        # 初始化列表显示
        self.refresh_account_list()
        
        # 启动定时同步机制（延迟启动，等待初始化完成）
        QTimer.singleShot(1000, self.start_sync_timer)
    
    def show_message_box(self, icon, title, text, buttons=None):
        """统一的弹窗样式函数，固定在GUI中央显示"""
        msg = QMessageBox(self)
        msg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        msg.setStyleSheet(f"""
            QMessageBox {{
                background-color: #FFF8E7;
                border: 3px solid {Style.COLOR_BORDER};
                border-radius: 18px;
                padding: 25px;
                min-width: 350px;
                max-width: 500px;
            }}
            QLabel {{
                color: {Style.COLOR_TEXT};
                font-size: 15px;
                font-weight: 600;
                padding: 15px;
                background: transparent;
            }}
            QPushButton {{
                border: 2px solid {Style.COLOR_BORDER};
                border-radius: 12px;
                padding: 10px 25px;
                background-color: #C8E6C9;
                color: {Style.COLOR_TEXT};
                font-size: 14px;
                font-weight: bold;
                min-width: 90px;
                {Style.FONT}
            }}
            QPushButton:hover {{
                background-color: #A5D6A7;
                border-width: 3px;
            }}
            QPushButton:pressed {{
                background-color: #81C784;
            }}
            QPushButton:default {{
                background-color: #4CAF50;
                color: white;
                border-width: 3px;
            }}
        """)
        msg.setIcon(icon)
        msg.setWindowTitle(title)
        msg.setText(text)
        if buttons:
            msg.setStandardButtons(buttons)
        
        msg.adjustSize()
        
        # 获取主窗口（MainWindow）的几何信息，确保弹窗显示在主窗口中央
        main_window = None
        if hasattr(self, 'main_window'):
            main_window = self.main_window
        else:
            main_window = self.window()
        
        if main_window:
            main_geometry = main_window.geometry()
        else:
            main_geometry = self.geometry()
        
        msg_geometry = msg.geometry()
        x = main_geometry.x() + (main_geometry.width() - msg_geometry.width()) // 2
        y = main_geometry.y() + (main_geometry.height() - msg_geometry.height()) // 2
        msg.move(x, y)
        
        return msg.exec_()

    # endregion

    # region  账号列表/避免锁在列表内

    def create_account_item(self, account, password, index, message_count, status="normal"):
        """创建单个账号项Widget（原AccountTableItemWidget逻辑）"""
        item = QWidget()
        item.setFixedHeight(35)
        item.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(item)
        layout.setContentsMargins(4, 6, 6, 6)
        layout.setSpacing(4)

        # 根据状态设置颜色
        text_color = "#FF0000" if status == "fault" else Style.COLOR_TEXT

        # 序号
        idx_lbl = QLabel(f"{index}.")
        idx_lbl.setFixedWidth(22)
        idx_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        idx_lbl.setStyleSheet(f"border:none;background:transparent;{Style.FONT} font-size:13px;color:{text_color};")
        layout.addWidget(idx_lbl)

        # 账号（如果故障，添加标记）
        account_text = f"{account} / ****"
        if status == "fault":
            account_text += "  ⚠️ 账号故障"
        acc_lbl = QLabel(account_text)
        acc_lbl.setStyleSheet(f"border:none;background:transparent;{Style.FONT} font-size:13px;color:{text_color};font-weight:bold;")
        layout.addWidget(acc_lbl, 1)

        # 删除按钮（末尾）
        del_btn = QPushButton("✖")
        del_btn.setFixedSize(20, 20)
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(lambda: self.delete_line(index-1))
        del_btn.setStyleSheet("QPushButton{border:none;background:transparent;color:rgba(255,0,0,0);}")
        layout.addWidget(del_btn)

        # 悬停/按下效果
        def enter(): 
            del_btn.setStyleSheet("QPushButton{border:none;background:transparent;color:#ff0000;font-weight:bold;}"
                                  "QPushButton:hover{background:rgba(255,200,200,0.3);border-radius:3px;}")
            item.setStyleSheet("QWidget{background:rgba(255,255,255,0.22);border:none;border-radius:10px;}")
        def leave(): 
            del_btn.setStyleSheet("QPushButton{border:none;background:transparent;color:rgba(255,0,0,0);}")
            item.setStyleSheet("QWidget{background:transparent;border:none;}")
        def press(): item.setStyleSheet("QWidget{background:rgba(255,255,255,0.32);border:none;border-radius:10px;}")
        item.enterEvent = lambda e: enter()
        item.leaveEvent = lambda e: leave()
        item.mousePressEvent = lambda e: press()
        item.mouseReleaseEvent = lambda e: enter() if item.underMouse() else leave()
        item.mouseDoubleClickEvent = lambda e: self.fill_account(account, password)

        return item
    
    def refresh_account_list(self):
        while self.account_list_layout.count():
            child = self.account_list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        for idx, line in enumerate(self.imported_lines, 1):
            # 兼容新旧格式
            if len(line) == 3:
                acc, pwd, status = line
            else:
                acc, pwd = line[0], line[1]
                status = "normal"
            item = self.create_account_item(acc, pwd, idx, 0, status)
            self.account_list_layout.addWidget(item)
    # endregion

    # region  数据库操作方法
    def get_api_base_url(self):
        """获取 API 基础 URL"""
        try:
            # 尝试从 PanelBackend 获取 server 的 api_base_url
            if hasattr(self.main_window, 'panel_backend') and self.main_window.panel_backend.server:
                api_url = self.main_window.panel_backend.server.api_base_url
                if api_url:
                    return api_url.rstrip('/')
            # 尝试从环境变量获取
            api_url = os.getenv("API_BASE_URL", "https://autosender.up.railway.app/api")
            return api_url.rstrip('/')
        except:
            return "https://autosender.up.railway.app/api"
    
    async def _fetch_accounts_from_db(self):
        """从数据库获取账号列表"""
        api_url = self.get_api_base_url()
        if not api_url:
            return []
        
        try:
            async with aiohttp.ClientSession(connector=self._get_ssl_connector()) as session:
                async with session.get(
                    f"{api_url}/id-library",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("success") and data.get("accounts"):
                            accounts = []
                            for acc in data["accounts"]:
                                accounts.append((
                                    acc.get("appleId", ""),
                                    acc.get("password", ""),
                                    acc.get("status", "normal")
                                ))
                            return accounts
        except Exception as e:
            print(f"⚠️ 从数据库获取账号列表失败: {e}")
        return []
    
    async def _save_accounts_to_db(self, accounts):
        """保存账号列表到数据库"""
        api_url = self.get_api_base_url()
        if not api_url:
            return False
        
        try:
            accounts_data = []
            for acc, pwd, status in accounts:
                accounts_data.append({
                    "appleId": acc,
                    "password": pwd,
                    "status": status,
                    "usageStatus": "new"
                })
            
            async with aiohttp.ClientSession(connector=self._get_ssl_connector()) as session:
                async with session.post(
                    f"{api_url}/id-library",
                    json={"accounts": accounts_data},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result.get("success", False)
        except Exception as e:
            print(f"⚠️ 保存账号列表到数据库失败: {e}")
        return False
    
    async def _delete_account_from_db(self, apple_id):
        """从数据库删除账号"""
        api_url = self.get_api_base_url()
        if not api_url:
            return False
        
        try:
            async with aiohttp.ClientSession(connector=self._get_ssl_connector()) as session:
                async with session.delete(
                    f"{api_url}/id-library/{apple_id}",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result.get("success", False)
        except Exception as e:
            print(f"⚠️ 从数据库删除账号失败: {e}")
        return False
    
    def _get_ssl_connector(self):
        """获取 SSL 连接器"""
        try:
            if hasattr(self.main_window, 'panel_backend') and self.main_window.panel_backend.server:
                return self.main_window.panel_backend.server._get_ssl_connector()
        except:
            pass
        # 如果没有 server，创建新的连接器
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        return aiohttp.TCPConnector(ssl=ssl_context)
    
    def _run_async_in_thread(self, coro):
        """在线程中运行异步函数"""
        def run_in_thread():
            try:
                # 尝试使用 server 的 loop
                if hasattr(self.main_window, 'panel_backend') and self.main_window.panel_backend.server:
                    server = self.main_window.panel_backend.server
                    if hasattr(server, 'loop') and server.loop:
                        asyncio.run_coroutine_threadsafe(coro, server.loop)
                        return
                # 如果没有 server loop，创建新的事件循环
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(coro)
                loop.close()
            except Exception as e:
                print(f"⚠️ 运行异步函数失败: {e}")
        
        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
    
    # region  定时同步机制
    def start_sync_timer(self):
        """启动定时同步定时器"""
        if self.sync_timer is None:
            self.sync_timer = QTimer()
            self.sync_timer.timeout.connect(self.sync_from_database)
            self.sync_timer.start(self.sync_interval)  # 每3秒同步一次
    
    def stop_sync_timer(self):
        """停止定时同步定时器"""
        if self.sync_timer:
            self.sync_timer.stop()
            self.sync_timer = None
    
    def sync_from_database(self):
        """从数据库同步账号列表"""
        def sync():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                accounts = loop.run_until_complete(self._fetch_accounts_from_db())
                loop.close()
                
                # 检查是否有变化（包括初始加载的情况）
                current_accounts = {(acc[0].lower(), acc[1], acc[2]) for acc in self.imported_lines} if self.imported_lines else set()
                new_accounts = {(acc[0].lower(), acc[1], acc[2]) for acc in accounts} if accounts else set()
                
                # 如果有变化，或者是初始加载（当前为空但新数据不为空），则更新
                if current_accounts != new_accounts or (not self.imported_lines and accounts):
                    # 有变化，更新列表
                    self.imported_lines = accounts
                    self.accounts = [acc[0] for acc in accounts]
                    self.passwords = {acc[0]: acc[1] for acc in accounts}
                    
                    # 在主线程刷新UI
                    QTimer.singleShot(0, self.refresh_account_list)
            except Exception as e:
                print(f"⚠️ 同步账号列表失败: {e}")
        
        # 在后台线程同步
        thread = threading.Thread(target=sync, daemon=True)
        thread.start()
    
    # region  自动保存登录记录（改为数据库）
    def load_config(self):
        """从数据库加载账号列表"""
        self.accounts = []
        self.passwords = {}
        self.imported_lines = []  # 格式: [(account, password, status), ...] status: "normal" 或 "fault"
        
        # 异步从数据库加载
        def load_from_db():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                accounts = loop.run_until_complete(self._fetch_accounts_from_db())
                loop.close()
                
                # 更新到主线程
                self.imported_lines = accounts
                for acc, pwd, status in accounts:
                    if acc not in self.accounts:
                        self.accounts.append(acc)
                    self.passwords[acc] = pwd
                
                # 刷新UI（需要在主线程执行）
                QTimer.singleShot(0, self.refresh_account_list)
            except Exception as e:
                print(f"⚠️ 加载账号列表失败: {e}")
                # 如果数据库加载失败，尝试从 JSON 文件加载（兼容性）
                if os.path.exists(self.config_file):
                    try:
                        with open(self.config_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            self.accounts = data.get("accounts", [])[:5]
                            self.passwords = data.get("passwords", {})
                            self.last_used = data.get("last_used")
                            imported = data.get("imported_lines", [])
                            self.imported_lines = []
                            for item in imported:
                                if isinstance(item, (list, tuple)):
                                    if len(item) == 2:
                                        self.imported_lines.append((item[0], item[1], "normal"))
                                    elif len(item) >= 3:
                                        self.imported_lines.append((item[0], item[1], item[2]))
                    except:
                        pass
        
        # 在后台线程加载
        thread = threading.Thread(target=load_from_db, daemon=True)
        thread.start()

    def save_config(self):
        """保存账号列表到数据库"""
        if not self.imported_lines:
            return
        
        # 异步保存到数据库
        self._run_async_in_thread(self._save_accounts_to_db(self.imported_lines))

    # endregion


    # region 账号操作：填充/删除/清空

    def fill_account(self, account, password):
        """填充账号到输入框"""
        self.edit_id.setText(account)
        self.edit_pass.setText(password)
        self.last_used = account
        self.save_config()

    def delete_line(self, index):
        """删除指定索引的账号"""
        if 0 <= index < len(self.imported_lines):
            account = self.imported_lines[index]
            apple_id = account[0] if isinstance(account, (list, tuple)) else account
            
            # 从数据库删除
            self._run_async_in_thread(self._delete_account_from_db(apple_id))
            
            # 从本地列表删除
            del self.imported_lines[index]
            self.refresh_account_list()

    def confirm_clear_all(self):
        """确认清空所有账号"""
        msg = QMessageBox(self)
        msg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        msg.setStyleSheet(
            "QMessageBox { background-color: #FFF8E7; border: 2px solid #2F2F2F; border-radius: 10px; }"
            "QLabel { color: #2F2F2F; font-size: 13px; }"
            "QPushButton { border: 2px solid #2F2F2F; border-radius: 8px; padding: 5px 15px; background: #C8E6C9; }"
            "QPushButton:hover { margin-top: 2px; margin-left: 2px; }"
        )
        msg.setIcon(QMessageBox.Question)
        msg.setWindowTitle("提示")
        msg.setText("是否全部删除？")
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.No)
        reply = msg.exec_()
        
        if reply == QMessageBox.Yes:
            # 从数据库删除所有账号
            async def delete_all():
                for account in self.imported_lines:
                    apple_id = account[0] if isinstance(account, (list, tuple)) else account
                    await self._delete_account_from_db(apple_id)
            
            self._run_async_in_thread(delete_all())
            
            self.imported_lines = []
            self.refresh_account_list()

    # endregion
    
    # region 检查系统重启后待登录账号
    
    def check_pending_login(self):
        """检查是否有待登录的账号（系统重启后）"""
        next_account_file = os.path.join(self.config_dir, "next_login_account.json")
        if os.path.exists(next_account_file):
            try:
                with open(next_account_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    next_account = data.get("account")
                    next_password = data.get("password")
                
                if next_account and next_password:
                    print(f"\n{'='*60}")
                    print(f"🔄 检测到系统重启后待登录账号: {next_account}")
                    print(f"{'='*60}\n")
                    
                    # 删除临时文件
                    os.remove(next_account_file)
                    
                    # 延迟5秒后自动登录
                    QTimer.singleShot(5000, lambda: self.auto_login_after_reboot(next_account, next_password))
                    
            except Exception as e:
                print(f"❌ 读取待登录账号失败: {e}")
                try:
                    os.remove(next_account_file)
                except:
                    pass
    
    def auto_login_after_reboot(self, account, password):
        """系统重启后自动登录"""
        print("\n📋 开始重启后自动登录...")
        
        # 在新线程中执行
        def do_login():
            try:
                # 等待10秒让系统完全稳定
                print("⏳ 等待10秒，系统启动中...")
                time.sleep(10)
                
                # 步骤1: 用测试账号试探环境
                print(f"\n{'='*60}")
                print(f"🔍 步骤1: 用测试账号试探环境安全性")
                print(f"{'='*60}")
                
                print(f"1️⃣ 打开 Messages 应用...")
                subprocess.Popen(['open', '-a', 'Messages'])
                time.sleep(5)
                
                # 检查 Messages 是否正常启动
                check_process = subprocess.run(['pgrep', '-x', 'Messages'], 
                                              capture_output=True)
                if check_process.returncode != 0:
                    print("❌ Messages 应用无法启动")
                    print("⚠️ 环境可能存在问题，停止自动登录")
                    QTimer.singleShot(0, lambda: self.show_manual_intervention_dialog(
                        "Messages 应用无法启动，可能系统存在问题"))
                    return
                print("   ✅ Messages 应用已启动")
                
                # 用测试账号试探
                print(f"\n2️⃣ 使用测试账号试探: {self.test_account}")
                print("   （这是一个废弃账号，用于测试环境安全性）")
                
                self.run_login_script(self.test_account, self.test_password)
                
                # 等待10秒观察反应
                print("   等待10秒观察系统反应...")
                time.sleep(10)
                
                # 检查是否有异常
                # 1. 检查 Messages 是否被强制关闭
                check_process = subprocess.run(['pgrep', '-x', 'Messages'], 
                                              capture_output=True)
                if check_process.returncode != 0:
                    print("❌ 测试后 Messages 被关闭，环境可能被标记")
                    print("⚠️ 不安全，停止自动登录")
                    QTimer.singleShot(0, lambda: self.show_manual_intervention_dialog(
                        "测试账号登录后 Messages 被关闭\n环境可能已被标记，不安全"))
                    return
                
                # 2. 检查是否弹出登录窗口（说明可以正常使用）
                check_window = subprocess.run([
                    'osascript', '-e',
                    'tell application "System Events" to get name of windows of process "Messages"'
                ], capture_output=True, text=True)
                
                has_window = check_window.returncode == 0 and check_window.stdout.strip()
                
                if has_window:
                    print("   ✅ 测试通过：系统正常响应")
                else:
                    print("   ⚠️ 无法确定窗口状态，但 Messages 未崩溃")
                
                # 强制退出 Messages（清理测试环境）
                print("\n3️⃣ 清理测试环境...")
                subprocess.run(['killall', '-9', 'Messages'], 
                              capture_output=True, stderr=subprocess.DEVNULL)
                time.sleep(3)
                
                # 步骤2: 环境安全，登录真正的账号
                print(f"\n{'='*60}")
                print(f"✅ 环境测试通过，开始登录下一个账号")
                print(f"{'='*60}\n")
                
                print(f"4️⃣ 重新打开 Messages 应用...")
                subprocess.Popen(['open', '-a', 'Messages'])
                time.sleep(3)
                
                print(f"5️⃣ 登录账号: {account}")
                login_success = self.run_login_script(account, password)
                
                # 等待15秒
                print("   等待15秒...")
                time.sleep(15)
                
                # 检查登录状态
                account_info = get_current_imessage_account()
                if account_info:
                    print(f"\n✅ 重启后登录成功: {account}")
                    print(f"{'='*60}\n")
                    
                    # 更新后端服务器面板的邮箱显示
                    if login_success:
                        try:
                            if hasattr(self.main_window, 'panel_backend'):
                                QTimer.singleShot(0, lambda: self.main_window.panel_backend.btn_email.setText(f"邮箱: {account}"))
                        except:
                            pass
                else:
                    print(f"\n❌ 重启后登录失败: {account}")
                    print("⚠️ 需要人工干预")
                    print(f"{'='*60}\n")
                    
                    # 弹窗通知用户
                    QTimer.singleShot(0, lambda: self.show_manual_intervention_dialog(
                        f"账号登录失败: {account}\n\n可能原因：\n1. 账号密码错误\n2. 账号被锁定\n3. 需要双因素验证"))
                    
            except Exception as e:
                print(f"❌ 重启后自动登录出错: {e}")
                QTimer.singleShot(0, lambda: self.show_manual_intervention_dialog(
                    f"自动登录过程出错:\n{str(e)}"))
        
        threading.Thread(target=do_login, daemon=True).start()
    
    def show_manual_intervention_dialog(self, detail_message="所有自动尝试均已失败，请手动检查账号状态并登录。"):
        """显示需要人工干预的对话框"""
        msg = QMessageBox(self)
        msg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        msg.setStyleSheet(
            "QMessageBox { background-color: #FFF8E7; border: 2px solid #2F2F2F; border-radius: 10px; }"
            "QLabel { color: #2F2F2F; font-size: 14px; }"
            "QPushButton { border: 2px solid #2F2F2F; border-radius: 8px; padding: 8px 20px; background: #FFCDD2; }"
            "QPushButton:hover { margin-top: 2px; margin-left: 2px; }"
        )
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("需要人工干预")
        msg.setText(f"智能登录失败\n\n{detail_message}")
        msg.exec_()
    
    # endregion

    # region 智能登录开关
    
    def toggle_auto_login(self):
        """切换智能登录开关"""
        self.auto_login_enabled = not self.auto_login_enabled
        
        if self.auto_login_enabled:
            
            self.btn_auto_login.setText("智能登录 已开启")
            self.btn_auto_login.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FF5252, stop:1 #F44336);
                    color: white;
                    border: 3px solid #FF1744;
                    border-radius: 10px;
                    {Style.FONT} font-size: 11px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FF6E6E, stop:1 #FF5252);
                    border-color: #FF5252;
                }}
            """)
            
      
            notification = SilentNotification(self)
            notification.show()
       
            self.start_auto_login_monitor()
            
        else:
   
            self.btn_auto_login.setText("智能登录")
            self.btn_auto_login.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #81C784, stop:1 #66BB6A);
                    color: white;
                    border: 2px solid {Style.COLOR_BORDER};
                    border-radius: 10px;
                    {Style.FONT} font-size: 12px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #A5D6A7, stop:1 #81C784);
                    border-color: #4CAF50;
                }}
                QPushButton:pressed {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #66BB6A, stop:1 #4CAF50);
                }}
            """)
            
  
            self.stop_auto_login_monitor()
    
    def start_auto_login_monitor(self):
  
        pass  # 事件驱动模式，无需额外操作
    
    def stop_auto_login_monitor(self):
   
        pass  # 事件驱动模式，无需额外操作
    
    def check_and_perform_auto_login(self, reason="未知"):
        """完整的检测和自动登录流程（被触发时执行）"""
        # 使用锁防止并发执行
        if not self.auto_login_lock.acquire(blocking=False):
            print("⏳ 智能登录正在执行中，跳过本次触发")
            return
        
        try:
            if not self.auto_login_enabled:
                return
            
            print(f"\n{'='*60}")
            print(f"🔍 开始智能登录检测流程")
            print(f"📌 触发原因: {reason}")
            print(f"{'='*60}\n")
            
            # 步骤1: 完全确认未登录（检查所有条件）
            if not self.confirm_not_logged_in():
                print("✅ 检测结果：账号正常登录，无需处理\n")
                return
            
            print("🚨 确认未登录，开始自动登录流程\n")
            
            # 步骤2: 执行自动登录
            success = self.perform_auto_login()
            
            if success:
                print("\n✅ 智能登录成功")
            else:
                print("\n❌ 智能登录失败")
            
            print(f"{'='*60}\n")
            
        finally:
            self.auto_login_lock.release()
    
    def confirm_not_logged_in(self):
        """完全确认未登录（检查所有条件）"""
        print("1️⃣ 检查登录状态...")
        
        # 条件1: 检查账号信息
        account_info = get_current_imessage_account()
        if account_info:
            print(f"   ✅ 已登录: {account_info.get('email', account_info.get('account', ''))}")
            return False
        print("   ❌ 未检测到登录账号")
        
        # 条件2: 检查数据库文件是否存在
        actual_db_path = db_path
        if not os.path.exists(actual_db_path) or os.path.getsize(actual_db_path) == 0:
            found_path = find_messages_database()
            if found_path:
                actual_db_path = found_path
            else:
                print("   ⚠️ 数据库文件不存在（可能从未登录过）")
                return True
        print(f"   ✅ 数据库文件存在: {actual_db_path}")
        
        # 条件3: 检查数据库是否可连接
        try:
            conn = sqlite3.connect(actual_db_path, timeout=5.0)
            cursor = conn.cursor()
            print("   ✅ 数据库可以连接")
            
            # 条件4: 检查 account 表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='account'")
            if not cursor.fetchone():
                print("   ⚠️ account 表不存在")
                conn.close()
                return True
            print("   ✅ account 表存在")
            
            # 条件5: 检查是否有 iMessage 记录
            cursor.execute("""
                SELECT account_login FROM account 
                WHERE service_name = 'iMessage' OR service_name LIKE '%iMessage%'
                LIMIT 1
            """)
            if cursor.fetchone():
                print("   ✅ 发现 iMessage 账号记录")
                conn.close()
                return False
            print("   ❌ 没有 iMessage 账号记录")
            conn.close()
            
        except sqlite3.OperationalError as e:
            print(f"   ⚠️ 数据库连接失败: {e}")
            return True
        
        # 条件6: 等待10秒后二次确认
        print("\n2️⃣ 等待10秒后进行二次确认...")
        time.sleep(10)
        
        account_info = get_current_imessage_account()
        if account_info:
            print(f"   ✅ 二次确认：已登录 {account_info.get('email', '')}")
            return False
        print("   ❌ 二次确认：仍未登录")
        
        return True
    
    def perform_auto_login(self):
        """执行自动登录流程"""
        try:
            # 检查账号列表是否为空
            if not self.imported_lines:
                print("❌ 账号列表为空，无法自动登录")
                return False
            
            # 获取当前使用的账号（第一个）
            current_account, current_password = self.imported_lines[0]
            
            # 步骤1: 完全退出 Messages 应用
            print("3️⃣ 完全退出 Messages 应用...")
            subprocess.run(['osascript', '-e', 'tell application "Messages" to quit'], 
                          capture_output=True, timeout=5)
            time.sleep(2)
            
            # 强制杀死进程（确保退干净）
            subprocess.run(['killall', '-9', 'Messages'], 
                          capture_output=True, stderr=subprocess.DEVNULL)
            time.sleep(1)
            print("   ✅ Messages 应用已退出")
            
            # 步骤2: 清理缓存
            print("\n4️⃣ 清理缓存...")
            cache_paths = [
                os.path.expanduser("~/Library/Caches/com.apple.Messages"),
                os.path.expanduser("~/Library/Messages/Cache"),
            ]
            for cache_path in cache_paths:
                if os.path.exists(cache_path):
                    try:
                        import shutil
                        shutil.rmtree(cache_path)
                        print(f"   ✅ 已清理: {cache_path}")
                    except Exception as e:
                        print(f"   ⚠️ 清理失败: {cache_path} - {e}")
            
            # 删除可能的锁文件
            lock_file = os.path.expanduser("~/Library/Messages/.lock")
            if os.path.exists(lock_file):
                try:
                    os.remove(lock_file)
                    print("   ✅ 已删除锁文件")
                except:
                    pass
            
            time.sleep(2)
            
            # 步骤3: 重新打开 Messages 应用
            print("\n5️⃣ 重新打开 Messages 应用...")
            subprocess.Popen(['open', '-a', 'Messages'])
            time.sleep(3)
            print("   ✅ Messages 应用已启动")
            
            # 步骤4: 登录相同的账号
            print(f"\n6️⃣ 登录账号: {current_account}")
            login_success = self.run_login_script(current_account, current_password)
            
            # 步骤5: 等待10秒，第一次检测
            print("   等待10秒...")
            time.sleep(10)
            
            account_info = get_current_imessage_account()
            if account_info:
                print(f"   ✅ 第一次检测：登录成功")
                self.failed_login_count = 0
                
                # 更新后端服务器面板的邮箱显示
                if login_success:
                    try:
                        if hasattr(self.main_window, 'panel_backend'):
                            self.main_window.panel_backend.btn_email.setText(f"邮箱: {current_account}")
                    except:
                        pass
                
                return True
            
            print("   ❌ 第一次检测：失败")
            
            # 步骤6: 再等5秒，第二次检测
            print("   再等待5秒...")
            time.sleep(5)
            
            account_info = get_current_imessage_account()
            if account_info:
                print(f"   ✅ 第二次检测：登录成功")
                self.failed_login_count = 0
                return True
            
            print("   ❌ 第二次检测：仍然失败")
            
            # 步骤7: 登录失败，执行 Plan B
            print("\n7️⃣ 登录失败，执行 Plan B...")
            return self.execute_plan_b(current_account, current_password)
                
        except Exception as e:
            print(f"❌ 自动登录流程出错: {e}")
            return False
    
    def execute_plan_b(self, failed_account, failed_password):
        """Plan B: 标记故障账号，超级修复，尝试下一个账号"""
        try:
            print("📋 Plan B 步骤1: 查找故障账号位置...")
            
            # 查找账号位置
            failed_index = -1
            for i, line in enumerate(self.imported_lines):
                acc = line[0] if len(line) >= 1 else ""
                if acc == failed_account:
                    failed_index = i
                    break
            
            if failed_index >= 0:
                print(f"   找到故障账号，位置: {failed_index + 1}")
                # 移除原位置
                self.imported_lines.pop(failed_index)
            else:
                print("   故障账号不在列表中")
            
            # 添加到最后，标记为故障
            self.imported_lines.append((failed_account, failed_password, "fault"))
            print(f"   已将故障账号移到最后并标记: {failed_account}")
            
            # 保存配置
            print("\n📋 Plan B 步骤2: 保存配置...")
            self.save_config()
            print("   ✅ 配置已保存")
            
            # 刷新界面显示
            try:
                self.refresh_account_list()
            except:
                pass
            
            # 获取下一个账号
            if failed_index >= 0 and failed_index < len(self.imported_lines) - 1:
                # 如果有下一个账号（不是故障账号）
                next_line = self.imported_lines[failed_index]
                if len(next_line) >= 3 and next_line[2] != "fault":
                    next_account, next_password = next_line[0], next_line[1]
                    print(f"\n📋 Plan B 步骤3: 找到下一个账号: {next_account}")
                elif len(self.imported_lines) > 1:
                    # 使用第一个非故障账号
                    for line in self.imported_lines:
                        if len(line) < 3 or line[2] != "fault":
                            next_account, next_password = line[0], line[1]
                            print(f"\n📋 Plan B 步骤3: 使用第一个正常账号: {next_account}")
                            break
                    else:
                        print("\n❌ 没有可用的正常账号")
                        return False
                else:
                    print("\n❌ 没有其他账号可用")
                    return False
            elif len(self.imported_lines) > 1:
                # 使用第一个非故障账号
                for line in self.imported_lines:
                    if len(line) < 3 or line[2] != "fault":
                        next_account, next_password = line[0], line[1]
                        print(f"\n📋 Plan B 步骤3: 使用第一个正常账号: {next_account}")
                        break
                else:
                    print("\n❌ 所有账号都标记为故障")
                    return False
            else:
                print("\n❌ 只有一个账号，无法切换")
                return False
            
            # 执行超级修复
            print("\n📋 Plan B 步骤4: 执行超级修复...")
            print("   ⚠️ 系统即将重启...")
            
            # 保存下一个要登录的账号到临时文件
            next_account_file = os.path.join(self.config_dir, "next_login_account.json")
            with open(next_account_file, "w", encoding="utf-8") as f:
                json.dump({"account": next_account, "password": next_password}, f)
            print(f"   ✅ 下次登录账号已保存: {next_account}")
            
            # 调用超级修复（从 PanelTools 获取）
            if hasattr(self.main_window, 'panel_tools'):
                self.main_window.panel_tools._run_hard_reset_thread()
                return True
            else:
                print("   ❌ 无法找到超级修复功能")
                return False
                
        except Exception as e:
            print(f"❌ Plan B 执行失败: {e}")
            return False
    
    # endregion

    # region 按钮 保存/登录/导入

    def import_accounts_file(self):
        fname, _ = QFileDialog.getOpenFileName(
            self, "选择账号文件", "", "文本文件 (*.txt);;所有文件 (*)"
        )
        if not fname:
            return
        try:
            with open(fname, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f.readlines() if line.strip()]
            new_accounts = []
            for line in lines:
                parts = [p.strip() for p in line.replace(",", " ").split() if p.strip()]
                if len(parts) >= 2:
                    acc, pwd = parts[0], parts[1]
                    new_accounts.append((acc, pwd, "normal"))
            
            # 合并到现有列表（去重）
            existing_accounts = {acc[0].lower() for acc in self.imported_lines}
            for acc, pwd, status in new_accounts:
                if acc.lower() not in existing_accounts:
                    self.imported_lines.append((acc, pwd, status))
                    existing_accounts.add(acc.lower())
            
            # 保存到数据库
            self.save_config()
            self.refresh_account_list()  # 刷新账号列表显示（表格形式）
        except Exception as e:
            msg = QMessageBox(self)
            msg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
            msg.setStyleSheet(
                "QMessageBox { background-color: #FFF8E7; border: 2px solid #2F2F2F; border-radius: 10px; }"
                "QLabel { color: #2F2F2F; font-size: 13px; }"
                "QPushButton { border: 2px solid #2F2F2F; border-radius: 8px; padding: 5px 15px; background: #C8E6C9; }"
                "QPushButton:hover { margin-top: 2px; margin-left: 2px; }"
            )
            msg.setIcon(QMessageBox.Critical)
            msg.setWindowTitle("错误")
            msg.setText(f"导入失败: {str(e)}")
            msg.exec_()

    def save_current_account(self):
        account = self.edit_id.text().strip()
        password = self.edit_pass.text().strip()
        if not account or not password:
            msg = QMessageBox(self)
            msg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
            msg.setStyleSheet(
                "QMessageBox { background-color: #FFF8E7; border: 2px solid #2F2F2F; border-radius: 10px; }"
                "QLabel { color: #2F2F2F; font-size: 13px; }"
                "QPushButton { border: 2px solid #2F2F2F; border-radius: 8px; padding: 5px 15px; background: #C8E6C9; }"
                "QPushButton:hover { margin-top: 2px; margin-left: 2px; }"
            )
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("提示")
            msg.setText("账号和密码不能为空")
            msg.exec_()
            return

        # 去重并保存（保留状态）
        self.imported_lines = [(a, p, s) if len(item) == 3 else (a, p, "normal") 
                               for item in self.imported_lines 
                               for a, p, *rest in [item if len(item) == 3 else (*item, "normal")]
                               for s in [rest[0] if rest else "normal"]
                               if a != account]
        self.imported_lines.insert(0, (account, password, "normal"))
        
        # 保存到数据库
        self.save_config()
        self.refresh_account_list()  # 刷新账号列表显示（表格形式）

    def toggle_password_visibility(self):
        """切换密码显示/隐藏"""
        if self.edit_pass.echoMode() == QLineEdit.Password:
            self.edit_pass.setEchoMode(QLineEdit.Normal)
            self.btn_toggle_pass.setText("🙈")
        else:
            self.edit_pass.setEchoMode(QLineEdit.Password)
            self.btn_toggle_pass.setText("👁")

    def accept_login(self):
        account = self.edit_id.text().strip()
        password = self.edit_pass.text()
        if not account or not password:
            msg = QMessageBox(self)
            msg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
            msg.setStyleSheet(
                "QMessageBox { background-color: #FFF8E7; border: 2px solid #2F2F2F; border-radius: 10px; }"
                "QLabel { color: #2F2F2F; font-size: 13px; }"
                "QPushButton { border: 2px solid #2F2F2F; border-radius: 8px; padding: 5px 15px; background: #C8E6C9; }"
                "QPushButton:hover { margin-top: 2px; margin-left: 2px; }"
            )
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("提示")
            msg.setText("Apple ID 和密码不能为空")
            msg.exec_()
            return

        self.last_used = account
        self.passwords[account] = password
        self.save_config()
        
        # 执行登录
        login_success = self.run_login_script(account, password)
        
        # 如果登录成功，更新后端服务器面板的邮箱显示
        if login_success:
            try:
                # 获取 PanelBackend 实例并更新邮箱显示
                if hasattr(self.main_window, 'panel_backend'):
                    self.main_window.panel_backend.btn_email.setText(f"邮箱: {account}")
            except Exception as e:
                pass  # 静默失败，不影响登录流程


# endregion
      
    # region  获取当前登录的账号信息

    def get_current_logged_in_account(self):
        """获取当前登录的iMessage账号信息"""
        try:
            account_info = self._query_current_account_from_db()
            if account_info:
                # 显示账号信息
                display_text = account_info.get('email', '') or account_info.get('phone', '') or account_info.get('account', '未知')
                if account_info.get('phone'):
                    display_text = f"{display_text} ({account_info['phone']})"
                self.current_account_display.setText(display_text)
                
                # 如果找到了账号，也可以自动填充到输入框
                if account_info.get('email'):
                    self.edit_id.setText(account_info['email'])
                
                self.show_message_box(
                    QMessageBox.Information,
                    "获取成功",
                    f"当前登录账号:\n"
                    f"Email: {account_info.get('email', '未找到')}\n"
                    f"电话: {account_info.get('phone', '未找到')}\n"
                    f"账号: {account_info.get('account', '未找到')}"
                )
            else:
                self.current_account_display.setText("未找到登录账号")
                self.show_message_box(
                    QMessageBox.Warning,
                    "获取失败",
                    "未能找到当前登录的iMessage账号信息。\n"
                    "请确保：\n"
                    "1. 已登录iMessage\n"
                    "2. 至少发送或接收过一条消息"
                )
        except Exception as e:
            print(f"获取当前账号失败: {str(e)}")
            self.show_message_box(
                QMessageBox.Warning,
                "错误",
                f"获取当前账号时出错:\n{str(e)}"
            )
   
    #从数据库查询当前登录的账号信息
    def _query_current_account_from_db(self):
        """从数据库查询当前登录的账号信息（使用全局函数）"""
        return get_current_imessage_account()
    
    def closeEvent(self, event):
        """关闭时停止定时同步"""
        self.stop_sync_timer()
        super().closeEvent(event)
    
    # endregion
    
    # region  使用脚本登录 

    def run_login_script(self, account_id, password, timeout=15):
        
        # 检查辅助功能权限
        check_cmd = "osascript -e 'tell application \"System Events\" to get name of processes' 2>/dev/null"
        has_permission = subprocess.call(check_cmd, shell=True) == 0
        
        # 弹窗
        if not has_permission:
            subprocess.Popen([
                'osascript', '-e',
                'button returned of (display dialog "需要添加终端辅助权限才能自动登录\\n\\n点击「打开设置」后:\\n1. 点击🔒解锁\\n2. 勾选✅Terminal\\n3. 关闭窗口" buttons {"稍后添加", "打开设置"} default button 2 with icon caution)',
                '-e',
                'if result is "打开设置" then do shell script "open \\"x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility\\""'
            ])
        
    
        applescript = f'''
        on run argv
            if (count of argv) < 2 then
                return "error:missing_args"
            end if
            set account to item 1 of argv
            set pwd to item 2 of argv

            -- 先激活 Messages,确保程序前置
            tell application "Messages" to activate

            tell application "System Events"
                set t0 to (current date)
                -- 重复等待，直到找到 window 1 且 window 有 text field(输入框)
                repeat
                    try
                        if (exists process "Messages") then
                            if (exists window 1 of process "Messages") then
                                -- 如果窗口中有 text field(登录输入框)，退出循环
                                if (exists text field 1 of window 1 of process "Messages") then
                                    exit repeat
                                end if
                            end if
                        end if
                    end try
                    delay 0.5
                    if ((current date) - t0) > {timeout} then
                        return "timeout"
                    end if
                end repeat

                -- 确保前端focus稳定
                delay 0.2

                -- 输入账号并回车，等一会再输入密码并回车
                keystroke account
                delay 0.2
                key code 36 -- return
                delay 0.5
                keystroke pwd
                delay 0.2
                key code 36 -- return
            end tell

            -- 等待5秒，然后检查登录是否成功
            delay 5
            
            tell application "System Events"
                try
                    -- 如果还存在登录输入框，说明登录失败
                    if (exists text field 1 of window 1 of process "Messages") then
                        return "login_failed"
                    end if
                end try
            end tell
            
            -- 登录窗口消失，说明登录成功
            return "ok"
        end run
        '''

        try:
            # 把脚本写成临时文件并执行（避免命令行转义问题）
            tmp = os.path.join(self.config_dir, "tmp_autologin.scpt")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(applescript)

            # 运行 osascript，传入账号和密码作为 argv
            process = subprocess.Popen(['osascript', tmp, account_id, password],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate(timeout=timeout + 10)

            # 清理临时脚本
            try:
                os.remove(tmp)
            except:
                pass

            out = stdout.decode('utf-8', errors='ignore').strip()
            err = stderr.decode('utf-8', errors='ignore').strip()
            if process.returncode != 0:
                return False

            if out == "ok":
                return True
            elif out == "login_failed":
                return False  # 登录窗口依然存在，登录失败
            elif out == "timeout":
                return False
            else:
                return False

        except subprocess.TimeoutExpired:
            return False
        except Exception as e:
            return False

    # endregion

class PanelTools(FixedSizePanel):
    # region 界面初始化

    def __init__(self, parent_window):
        # 渐变背景（参考index.html风格）
        gradient_bg = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #F48FB1, stop:0.5 #F06292, stop:1 #EC407A)"
        super().__init__(gradient_bg, 550, 430, parent_window)
        self.main_window = parent_window
        # 程序系统文件夹路径（datapath）
        self.datapath = os.path.dirname(os.path.abspath(__file__))
        # 记录上一次报告文件路径
        self.last_system_diag_report = None
        self.last_db_diag_report = None
        # 确保报告文件夹存在（保存到 logs）
        self.reports_dir = os.path.join(self.datapath, "logs")
        os.makedirs(self.reports_dir, exist_ok=True)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # 标题栏 - 移除渐变背景
        self.header = QFrame()
        self.header.setFixedHeight(35)
        self.header.setStyleSheet(Style.get_panel_title_bar_style())
        header_layout = QHBoxLayout(self.header)
        header_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header_layout.setContentsMargins(13, 0, 0, 0)
        header_layout.setSpacing(0)
        lbl_title = QLabel("修复工具")
        lbl_title.setStyleSheet(f"border: none; color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 15px; padding: 0px;")
        header_layout.addWidget(lbl_title)
        header_layout.addStretch()
        layout.addWidget(self.header)
        
        # 功能按钮区域 - 去掉边框，只保留面板外边框
        function_panel = QFrame()
        function_panel.setStyleSheet("background: transparent; border: none;")
        function_layout = QVBoxLayout(function_panel)
        function_layout.setContentsMargins(15, 15, 15, 15)
        function_layout.setSpacing(12)
        
        # 1. 系统检测 - 蓝色渐变
        sys_check_row = QHBoxLayout()
        sys_check_row.setSpacing(15)
        sys_check_row.addSpacing(30)  # 按钮右移50
        self.btn_system_check = QPushButton("系统检测")
        self.btn_system_check.setFixedSize(100, 35)  # 宽度减少20
        self.btn_system_check.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4FC3F7, stop:1 #29B6F6);
                color: white;
                border: {Style.BORDER_WIDTH}px solid {Style.COLOR_BORDER};
                border-radius: {Style.BORDER_RADIUS_SMALL}px;
                {Style.FONT} font-size: 13px;
                padding: 5px 15px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #29B6F6, stop:1 #0288D1);
                margin-top: 2px;
                margin-left: 2px;
            }}
            QPushButton:pressed {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0288D1, stop:1 #0277BD);
                margin-top: 3px;
                margin-left: 3px;
            }}
        """)
        self.btn_system_check.clicked.connect(self.run_diagnose)
        # 添加右键菜单功能
        self.btn_system_check.setContextMenuPolicy(Qt.CustomContextMenu)
        self.btn_system_check.customContextMenuRequested.connect(lambda: self._open_last_report("system"))
        sys_check_row.addWidget(self.btn_system_check)
        sys_label = QLabel("检查系统环境和依赖 | 安全检测，不修改系统")
        sys_label.setStyleSheet(f"color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 12px; padding: 5px 0px;")
        sys_check_row.addWidget(sys_label)
        sys_check_row.addStretch()
        function_layout.addLayout(sys_check_row)
        
        # 2. 数据库检测 - 绿色渐变
        db_check_row = QHBoxLayout()
        db_check_row.setSpacing(15)
        db_check_row.addSpacing(30)  # 按钮右移50
        self.btn_database_check = QPushButton("数据库检测")
        self.btn_database_check.setFixedSize(100, 35)  # 宽度减少20
        self.btn_database_check.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #66BB6A, stop:1 #4CAF50);
                color: white;
                border: {Style.BORDER_WIDTH}px solid {Style.COLOR_BORDER};
                border-radius: {Style.BORDER_RADIUS_SMALL}px;
                {Style.FONT} font-size: 13px;
                padding: 5px 15px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4CAF50, stop:1 #388E3C);
                margin-top: 2px;
                margin-left: 2px;
            }}
            QPushButton:pressed {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #388E3C, stop:1 #2E7D32);
                margin-top: 3px;
                margin-left: 3px;
            }}
        """)
        self.btn_database_check.clicked.connect(self.run_database_diagnose)
        # 添加右键菜单功能
        self.btn_database_check.setContextMenuPolicy(Qt.CustomContextMenu)
        self.btn_database_check.customContextMenuRequested.connect(lambda: self._open_last_report("database"))
        db_check_row.addWidget(self.btn_database_check)
        db_label = QLabel("检查数据库完整性和连接 | 安全检测，不修改数据")
        db_label.setStyleSheet(f"color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 12px; padding: 5px 0px;")
        db_check_row.addWidget(db_label)
        db_check_row.addStretch()
        function_layout.addLayout(db_check_row)
        
        # 3. 权限修复 - 橙色渐变
        perm_fix_row = QHBoxLayout()
        perm_fix_row.setSpacing(15)
        perm_fix_row.addSpacing(30)  # 按钮右移50
        self.btn_permission_fix = QPushButton("权限修复")
        self.btn_permission_fix.setFixedSize(100, 35)  # 宽度减少20
        self.btn_permission_fix.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFA726, stop:1 #FF9800);
                color: white;
                border: {Style.BORDER_WIDTH}px solid {Style.COLOR_BORDER};
                border-radius: {Style.BORDER_RADIUS_SMALL}px;
                {Style.FONT} font-size: 13px;
                padding: 5px 15px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FF9800, stop:1 #F57C00);
                margin-top: 2px;
                margin-left: 2px;
            }}
            QPushButton:pressed {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #F57C00, stop:1 #E65100);
                margin-top: 3px;
                margin-left: 3px;
            }}
        """)
        self.btn_permission_fix.clicked.connect(self.run_permission_fix)
        perm_fix_row.addWidget(self.btn_permission_fix)
        perm_label = QLabel("修复文件和访问权限 | 修改系统权限配置")
        perm_label.setStyleSheet(f"color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 12px; padding: 5px 0px;")
        perm_fix_row.addWidget(perm_label)
        perm_fix_row.addStretch()
        function_layout.addLayout(perm_fix_row)
        
        # 4. 清空收件箱 - 红色渐变
        clear_inbox_row = QHBoxLayout()
        clear_inbox_row.setSpacing(15)
        clear_inbox_row.addSpacing(30)  # 按钮右移50
        self.btn_clear_inbox = QPushButton("清空收件箱")
        self.btn_clear_inbox.setFixedSize(100, 35)  # 宽度减少20
        self.btn_clear_inbox.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #EF5350, stop:1 #F44336);
                color: white;
                border: {Style.BORDER_WIDTH}px solid {Style.COLOR_BORDER};
                border-radius: {Style.BORDER_RADIUS_SMALL}px;
                {Style.FONT} font-size: 13px;
                padding: 5px 15px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #F44336, stop:1 #D32F2F);
                margin-top: 2px;
                margin-left: 2px;
            }}
            QPushButton:pressed {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #D32F2F, stop:1 #C62828);
                margin-top: 3px;
                margin-left: 3px;
            }}
        """)
        self.btn_clear_inbox.clicked.connect(self.clear_imessage_inbox)
        clear_inbox_row.addWidget(self.btn_clear_inbox)
        clear_label = QLabel("清空iMessage收件箱 | 永久删除所有聊天记录")
        clear_label.setStyleSheet(f"color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 12px; padding: 5px 0px;")
        clear_inbox_row.addWidget(clear_label)
        clear_inbox_row.addStretch()
        function_layout.addLayout(clear_inbox_row)
        
        # 5. 超级修复 - 紫色渐变
        super_fix_row = QHBoxLayout()
        super_fix_row.setSpacing(15)
        super_fix_row.addSpacing(30)  # 按钮右移50
        self.btn_super_fix = QPushButton("超级修复")
        self.btn_super_fix.setFixedSize(100, 35)  # 宽度减少20
        self.btn_super_fix.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #BA68C8, stop:1 #AB47BC);
                color: white;
                border: {Style.BORDER_WIDTH}px solid {Style.COLOR_BORDER};
                border-radius: {Style.BORDER_RADIUS_SMALL}px;
                {Style.FONT} font-size: 13px;
                padding: 5px 15px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #AB47BC, stop:1 #8E24AA);
                margin-top: 2px;
                margin-left: 2px;
            }}
            QPushButton:pressed {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #8E24AA, stop:1 #6A1B9A);
                margin-top: 3px;
                margin-left: 3px;
            }}
        """)
        self.btn_super_fix.clicked.connect(self.run_hard_reset)
        super_fix_row.addWidget(self.btn_super_fix)
        super_label = QLabel("执行全面深度修复 | 删除所有数据并可能重启系统")
        super_label.setStyleSheet(f"color: {Style.COLOR_TEXT}; {Style.FONT} font-size: 12px; padding: 5px 0px;")
        super_fix_row.addWidget(super_label)
        super_fix_row.addStretch()
        function_layout.addLayout(super_fix_row)
        
        # 添加到主布局
        layout.addWidget(function_panel)

    # endregion

    # region 显示消息框函数

    def _center_message_box(self, msg):
        """全局弹窗定位函数，确保弹窗固定在GUI中央且不超出屏幕范围"""
        msg.adjustSize()
        
        # 获取主窗口（MainWindow）的几何信息，确保弹窗显示在主窗口中央
        main_window = None
        if hasattr(self, 'main_window'):
            main_window = self.main_window
        else:
            main_window = self.window()
        
        if main_window:
            main_geometry = main_window.geometry()
        else:
            main_geometry = self.geometry()
        
        # 获取屏幕尺寸
        screen = QApplication.primaryScreen().geometry()
        screen_width = screen.width()
        screen_height = screen.height()
        
        msg_geometry = msg.geometry()
        msg_width = msg_geometry.width()
        msg_height = msg_geometry.height()
        
        # 计算居中位置
        x = main_geometry.x() + (main_geometry.width() - msg_width) // 2
        y = main_geometry.y() + (main_geometry.height() - msg_height) // 2
        
        # 确保不超出屏幕范围
        if x < screen.x():
            x = screen.x() + 20  # 左边距20px
        elif x + msg_width > screen.x() + screen_width:
            x = screen.x() + screen_width - msg_width - 20  # 右边距20px
        
        if y < screen.y():
            y = screen.y() + 20  # 上边距20px
        elif y + msg_height > screen.y() + screen_height:
            y = screen.y() + screen_height - msg_height - 20  # 下边距20px
        
        msg.move(x, y)

    def show_message_box(self, icon, title, text, buttons=None):
        """统一的弹窗样式函数，固定在GUI中央显示"""
        msg = QMessageBox(self)
        msg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        
        # 限制文本长度，避免弹窗过大
        max_text_length = 800  # 大约限制在800字符以内
        if len(text) > max_text_length:
            text = text[:max_text_length] + "\n\n... (内容过长，已截断，请查看完整报告文件)"
        
        msg.setStyleSheet(f"""
            QMessageBox {{
                background-color: #FFF8E7;
                border: 3px solid {Style.COLOR_BORDER};
                border-radius: 18px;
                padding: 20px;
                min-width: 350px;
                max-width: 600px;
            }}
            QLabel {{
                color: {Style.COLOR_TEXT};
                font-size: 13px;
                font-weight: 600;
                padding: 10px;
                background: transparent;
            }}
            QPushButton {{
                border: 2px solid {Style.COLOR_BORDER};
                border-radius: 12px;
                padding: 10px 25px;
                background-color: #C8E6C9;
                color: {Style.COLOR_TEXT};
                font-size: 14px;
                font-weight: bold;
                min-width: 90px;
                {Style.FONT}
            }}
            QPushButton:hover {{
                background-color: #A5D6A7;
                border-width: 3px;
            }}
            QPushButton:pressed {{
                background-color: #81C784;
            }}
            QPushButton:default {{
                background-color: #4CAF50;
                color: white;
                border-width: 3px;
            }}
        """)
        msg.setIcon(icon)
        msg.setWindowTitle(title)
        msg.setText(text)
        if buttons:
            msg.setStandardButtons(buttons)
        
        # 使用全局定位函数
        self._center_message_box(msg)
        
        return msg.exec_()

    # endregion

    # region 右键打开上一次报告

    def _open_last_report(self, report_type):
        """右键点击按钮时打开上一次的报告文件"""
        if report_type == "system":
            last_report = self.last_system_diag_report
            report_name = "系统检测"
        elif report_type == "database":
            last_report = self.last_db_diag_report
            report_name = "数据库检测"
        else:
            return
        
        if last_report and os.path.exists(last_report):
            try:
                subprocess.run(["open", last_report])
            except Exception as e:
                self.show_message_box(QMessageBox.Warning, "提示", f"无法打开报告文件: {str(e)}")
        # 没有文件时不显示提示，直接返回

    # endregion

    # region 运行命令函数

    def run_with_auth(self, cmd: str):
        safe = cmd.replace('\\', '\\\\').replace('"', '\\"')
        applescript = f'''do shell script "{safe}" with administrator privileges'''
        p = subprocess.Popen(['osascript', '-e', applescript], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate()
        return p.returncode, out.strip(), err.strip()

    def run_cmd_local(self, cmd: str):
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate()
        return p.returncode, out.strip(), err.strip()


    #endregion

    # region 修复权限（使用线程）

    def _run_permission_fix_thread(self):
        """权限修复的线程函数"""
        try:
            get_permissions_cmd = '''
chmod -R 755 /Library/Preferences/com.apple.apsd.plist /Library/Preferences/com.apple.ids.service* /Library/Preferences/com.apple.imfoundation* 2>/dev/null || true
chmod -R 755 ~/Library/Preferences/com.apple.iChat* ~/Library/Preferences/com.apple.immessage* ~/Library/Preferences/com.apple.ids.service* ~/Library/Preferences/com.apple.identityservices* ~/Library/Preferences/com.apple.imfoundation* 2>/dev/null || true
chmod -R 755 ~/Library/Caches/com.apple.Messages ~/Library/Caches/com.apple.apsd ~/Library/Caches/com.apple.imfoundation* ~/Library/Caches/com.apple.identityservices* 2>/dev/null || true
chown -R "$USER" ~/Library/Preferences/com.apple.* ~/Library/Caches/com.apple.* 2>/dev/null || true
/usr/bin/killall -HUP mDNSResponder 2>/dev/null || true
/usr/bin/killall -9 apsd 2>/dev/null || true
'''
            ret, out, err = self.run_with_auth(get_permissions_cmd)
            result_msg = "✅ 权限修复完成（退出码 0）" if ret == 0 else f"⚠️ 权限修复执行结束，退出码 {ret}，stderr: {err}"
            if hasattr(self.main_window, 'system_log'):
                self.main_window.system_log(result_msg)
            # 使用QTimer在主线程中显示弹窗
            QTimer.singleShot(0, lambda: self.show_message_box(QMessageBox.Information, "完成", result_msg))
        except Exception as e:
            error_msg = f"权限修复过程出错: {str(e)}"
            if hasattr(self.main_window, 'system_log'):
                self.main_window.system_log(error_msg)
            QTimer.singleShot(0, lambda: self.show_message_box(QMessageBox.Critical, "错误", error_msg))

    def run_permission_fix(self):
        """权限修复（带线程管理）"""
        if sys.platform != "darwin":
            self.show_message_box(QMessageBox.Warning, "提示", "此功能仅在 macOS 系统上可用")
            return
        reply = self.show_message_box(QMessageBox.Question, "确认", "确定要修复 iMessage/IDS/Push 相关文件权限吗？（需要管理员授权）", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.No: 
            return
        if hasattr(self.main_window, 'system_log'):
            self.main_window.system_log("开始修复权限...")
        # 在后台线程中运行
        thread = threading.Thread(target=self._run_permission_fix_thread, daemon=True)
        thread.start()

    # endregion

    # region 激活诊断函数

    def run_diagnose(self):
        if sys.platform != "darwin":
            self.show_message_box(QMessageBox.Warning, "提示", "此功能仅在 macOS 系统上可用")
            return
        try:
            # 使用程序系统文件夹（datapath）
            # 格式：Diag_IM1228 (月/日)
            date_str = datetime.now().strftime("%m%d")
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")  # 用于临时脚本文件名
            logfile = os.path.join(self.reports_dir, f"Diag_IM{date_str}.log")
            # 记录上一次报告路径
            self.last_system_diag_report = logfile
            script_content = '''#!/bin/bash
echo "===== iMessage 自检工具 ====="
check_process() { pgrep "$1" >/dev/null && echo "1" || echo "0"; }
check_lockdown() { [ -d "/private/var/db/lockdown" ] && ls /private/var/db/lockdown >/dev/null 2>&1 && echo "1" || echo "0"; }
check_ping() { ping -c 1 init.itunes.apple.com >/dev/null 2>&1 && echo "1" || echo "0"; }
check_logs() { log show --last 5m --style syslog --predicate 'subsystem == "com.apple.imfoundation" OR eventMessage CONTAINS "iMessage" OR eventMessage CONTAINS "apsd" OR eventMessage CONTAINS "IDS" OR eventMessage CONTAINS "activation"' 2>/dev/null | grep -Ei "fail|error|denied|timeout|lost|invalid" >/dev/null && echo "0" || echo "1"; }
apsd_ok=$(check_process "apsd")
imagent_ok=$(check_process "imagent")
ids_ok=$(check_process "identityservicesd")
lockdown_ok=$(check_lockdown)
ping_ok=$(check_ping)
logs_ok=$(check_logs)
echo "===== 检测结果 ====="
[[ $apsd_ok == 1 ]] && echo "✔ APS 推送服务进程正常（apsd）" || echo "✘ APS 推送服务未运行"
[[ $imagent_ok == 1 ]] && echo "✔ iMessage 服务进程正常（imagent）" || echo "✘ imagent 未运行"
[[ $ids_ok == 1 ]] && echo "✔ Apple ID / 激活服务正常（identityservicesd）" || echo "✘ identityservicesd 未运行"
[[ $lockdown_ok == 1 ]] && echo "✔ 权限正常（/private/var/db/lockdown 可访问）" || echo "✘ lockdown 权限异常"
[[ $ping_ok == 1 ]] && echo "✔ 苹果激活服务器连接正常" || echo "✘ 无法连接激活服务器"
[[ $logs_ok == 1 ]] && echo "✔ 日志正常：没有激活失败、没有推送错误" || echo "✘ 日志发现可能的失败信息（网络/激活/APS）"
echo "===== 建议动作 ====="
echo "killall apsd"
echo "killall imagent"
echo "killall identityservicesd"
echo "===== 完成 ====="
'''
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            script_file = os.path.join(tempfile.gettempdir(), f"imessage_diag_{timestamp_str}.sh")
            with open(script_file, "w", encoding="utf-8") as f:
                f.write(script_content)
            os.chmod(script_file, 0o755)
            ret, out, err = self.run_cmd_local(f'bash "{script_file}"')
            with open(logfile, "w", encoding="utf-8") as f:
                f.write("="*60+"\n")
                f.write("🎯 iMessage 诊断报告\n")
                f.write("="*60+"\n")
                f.write(f"诊断时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
                f.write(f"操作系统: macOS\n")
                f.write(f"日志文件: {logfile}\n\n")
                if out: f.write(out)
                if err: f.write(f"\n错误输出:\n{err}\n")
                f.write("\n诊断完成！\n")
            try: os.remove(script_file)
            except: pass
            subprocess.run(["open", logfile])
            if hasattr(self.main_window, 'system_log'):
                self.main_window.system_log("诊断已完成")
            self.show_message_box(QMessageBox.Information, "完成", f"诊断报告已生成！\n\n文件位置: {logfile}\n已自动打开文件查看")
        except Exception as e:
            self.show_message_box(QMessageBox.Critical, "错误", f"执行诊断时出错: {str(e)}")

    # endregion

    # region 一键硬核修复（使用线程）

    def run_hard_reset(self):
        if sys.platform != "darwin":
            self.show_message_box(QMessageBox.Warning, "提示", "此功能仅在 macOS 系统上可用")
            return
        reply = self.show_message_box(QMessageBox.Warning, "警告", "警告：此操作会删除 Messages、Caches、Preferences 等 iMessage 相关所有数据（不可恢复）。\n不会退出 Apple ID。\n是否继续？", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.No: 
            return
        reply2 = self.show_message_box(QMessageBox.Warning, "最后确认", "最后一次确认：确定进行超级修复？\n\n此操作将删除所有 iMessage 相关数据并重新初始化服务。", QMessageBox.Yes | QMessageBox.No)
        if reply2 == QMessageBox.No: 
            return
        
        if hasattr(self.main_window, 'system_log'):
            self.main_window.system_log("开始：一键硬核修复...")
        
        # 在后台线程中运行
        thread = threading.Thread(target=self._run_hard_reset_thread, daemon=True)
        thread.start()
        
        # 显示正在修复的提示（非阻塞，使用 QTimer 在主线程中显示）
        QTimer.singleShot(100, lambda: self.show_message_box(QMessageBox.Information, "超级修复进行中", 
            "正在执行超级修复...\n\n"
            "正在执行的操作：\n"
            "• 清理所有 iMessage 数据和配置\n"
            "• 清理缓存文件\n"
            "• 重启相关服务\n\n"
            "修复完成后会显示结果提示。\n"
            "请稍候..."))

    def _run_hard_reset_thread(self):
        """超级修复的线程函数"""
        try:
            HOME = os.path.expanduser("~")
            hard_reset_script = f'''#!/bin/bash
set -e
pkill -9 apsd 2>/dev/null || true
pkill -9 imagent 2>/dev/null || true
pkill -9 identityservicesd 2>/dev/null || true
pkill -9 ids 2>/dev/null || true
pkill -9 assistantd 2>/dev/null || true
rm -rf /Library/Preferences/com.apple.apsd.plist 2>/dev/null || true
rm -rf /Library/Preferences/com.apple.ids.service* 2>/dev/null || true
rm -rf /Library/Preferences/com.apple.imfoundation* 2>/dev/null || true
rm -rf "{HOME}/Library/Preferences/com.apple.iChat*" 2>/dev/null || true
rm -rf "{HOME}/Library/Preferences/com.apple.imessage*" 2>/dev/null || true
rm -rf "{HOME}/Library/Preferences/com.apple.ids.service*" 2>/dev/null || true
rm -rf "{HOME}/Library/Preferences/com.apple.identityservices*" 2>/dev/null || true
rm -rf "{HOME}/Library/Preferences/com.apple.imfoundation*" 2>/dev/null || true
rm -rf "{HOME}/Library/Preferences/com.apple.FaceTime*" 2>/dev/null || true
rm -rf "{HOME}/Library/Messages" 2>/dev/null || true
rm -rf "{HOME}/Library/Caches/com.apple.Messages" 2>/dev/null || true
rm -rf "{HOME}/Library/Caches/com.apple.apsd" 2>/dev/null || true
rm -rf "{HOME}/Library/Caches/com.apple.imfoundation*" 2>/dev/null || true
rm -rf "{HOME}/Library/Caches/com.apple.identityservices*" 2>/dev/null || true
rm -rf "{HOME}/Library/Caches/com.apple.ids*" 2>/dev/null || true
rm -rf "{HOME}/Library/IdentityServices" 2>/dev/null || true
rm -rf /private/var/db/crls/* 2>/dev/null || true
rm -rf /private/var/folders/*/*/*/com.apple.aps* 2>/dev/null || true
rm -rf /private/var/folders/*/*/*/com.apple.imfoundation* 2>/dev/null || true
rm -rf /private/var/folders/*/*/*/com.apple.ids* 2>/dev/null || true
/usr/bin/dscacheutil -flushcache 2>/dev/null || true
/usr/bin/killall -HUP mDNSResponder 2>/dev/null || true
launchctl bootout system /System/Library/LaunchDaemons/com.apple.apsd.plist 2>/dev/null || true
launchctl bootstrap system /System/Library/LaunchDaemons/com.apple.apsd.plist 2>/dev/null || true
launchctl bootout gui/$UID /System/Library/LaunchAgents/com.apple.imagent.plist 2>/dev/null || true
launchctl bootstrap gui/$UID /System/Library/LaunchAgents/com.apple.imagent.plist 2>/dev/null || true
launchctl bootout gui/$UID /System/Library/LaunchAgents/com.apple.identityservicesd.plist 2>/dev/null || true
launchctl bootstrap gui/$UID /System/Library/LaunchAgents/com.apple.identityservicesd.plist 2>/dev/null || true
launchctl kickstart -k system/com.apple.apsd 2>/dev/null || true
launchctl kickstart -k gui/$UID/com.apple.imagent 2>/dev/null || true
launchctl kickstart -k gui/$UID/com.apple.identityservicesd 2>/dev/null || true
'''
            fd, path = tempfile.mkstemp(suffix='.sh', text=True)
            with os.fdopen(fd, 'w') as f: 
                f.write(hard_reset_script)
            os.chmod(path, 0o755)
            ret, out, err = self.run_with_auth(f'"{path}"')
            if ret == 0:
                result_msg = "✅ 超级修复已完成！\n\n" \
                           "已执行的操作：\n" \
                           "• 清理了所有 iMessage 数据和配置\n" \
                           "• 清理了缓存文件\n" \
                           "• 重启了相关服务\n\n" \
                           "请重新登录 iMessage 进行测试。"
            else:
                result_msg = f"❌ 超级修复执行出错\n\n退出码: {ret}\n错误信息: {err}"
            if hasattr(self.main_window, 'system_log'):
                self.main_window.system_log(result_msg)
            # 使用QTimer在主线程中显示弹窗
            QTimer.singleShot(0, lambda: self.show_message_box(QMessageBox.Information, "完成", result_msg))
        except Exception as e:
            error_msg = f"超级修复过程出错: {str(e)}"
            if hasattr(self.main_window, 'system_log'):
                self.main_window.system_log(error_msg)
            QTimer.singleShot(0, lambda: self.show_message_box(QMessageBox.Critical, "错误", error_msg))

    # endregion

    # region 数据库诊断和修复

    def _find_messages_database(self):
        """尝试找到 Messages 数据库文件"""
        possible_paths = [
            os.path.expanduser("~/Library/Messages/chat.db"),
            os.path.expanduser("~/Library/Containers/com.apple.iChat/Data/Library/Messages/chat.db"),
        ]
        
        # 检查是否有其他可能的路径
        home = os.path.expanduser("~")
        if home:
            # 检查各种容器路径
            containers_base = os.path.join(home, "Library", "Containers")
            if os.path.exists(containers_base):
                for container in ["com.apple.iChat", "com.apple.MobileSMS", "com.apple.Messages"]:
                    container_path = os.path.join(containers_base, container, "Data", "Library", "Messages", "chat.db")
                    if os.path.exists(container_path):
                        possible_paths.append(container_path)
            
            # 检查是否有其他 Messages 相关目录
            messages_dir = os.path.join(home, "Library", "Messages")
            if os.path.exists(messages_dir):
                try:
                    for item in os.listdir(messages_dir):
                        item_path = os.path.join(messages_dir, item)
                        if os.path.isfile(item_path) and item.endswith('.db'):
                            if item_path not in possible_paths:
                                possible_paths.append(item_path)
                except PermissionError:
                    pass
        
        found = []
        for path in possible_paths:
            if os.path.exists(path):
                try:
                    size = os.path.getsize(path)
                    found.append((path, size))
                except (PermissionError, OSError):
                    found.append((path, -1))  # -1 表示无法访问
        
        return found

    def _check_database(self, path):
        """检查数据库文件"""
        info = {
            "path": path,
            "exists": os.path.exists(path),
            "size": 0,
            "readable": False,
            "has_message_table": False,
            "all_tables": []
        }
        
        if info["exists"]:
            info["size"] = os.path.getsize(path)
            
            if info["size"] > 0:
                try:
                    conn = sqlite3.connect(path)
                    cursor = conn.cursor()
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    info["all_tables"] = [row[0] for row in cursor.fetchall()]
                    info["has_message_table"] = "message" in info["all_tables"]
                    info["readable"] = True
                    conn.close()
                except Exception as e:
                    info["error"] = str(e)
        
        return info

    def _fix_database_permissions(self, path):
        """尝试修复数据库文件权限"""
        try:
            os.chmod(path, 0o644)
            return True, "权限修复成功"
        except PermissionError:
            return False, "需要管理员权限"
        except Exception as e:
            return False, f"权限修复失败: {str(e)}"

    def _repair_database(self, path):
        """尝试修复损坏的数据库（VACUUM 和 REINDEX）"""
        try:
            # 先备份（安全措施）
            if os.path.exists(path):
                timestamp = int(time.time())
                backup_path = path + f".backup_{timestamp}"
                try:
                    shutil.copy2(path, backup_path)
                except Exception as e:
                    return False, f"备份失败: {str(e)}", None
            else:
                return False, "数据库文件不存在", None
            
            # 尝试连接数据库
            try:
                conn = sqlite3.connect(path)
                cursor = conn.cursor()
            except sqlite3.DatabaseError as e:
                return False, f"数据库损坏，无法连接: {str(e)}。建议从 Time Machine 恢复或重新初始化 iMessage。", backup_path
            
            try:
                # 执行完整性检查
                cursor.execute("PRAGMA integrity_check")
                integrity_result = cursor.fetchone()
                
                if integrity_result and integrity_result[0] == "ok":
                    # 数据库完整，执行优化
                    cursor.execute("VACUUM")
                    cursor.execute("REINDEX")
                    conn.commit()
                    conn.close()
                    return True, "数据库优化完成（已创建备份）", backup_path
                else:
                    conn.close()
                    return False, f"数据库完整性检查失败: {integrity_result[0] if integrity_result else '未知错误'}。建议从备份恢复。", backup_path
            except sqlite3.DatabaseError as e:
                conn.close()
                return False, f"数据库操作失败: {str(e)}。备份已保存: {backup_path}", backup_path
        except Exception as e:
            return False, f"修复过程出错: {str(e)}", None

    def run_database_diagnose(self):
        """运行数据库诊断"""
        if sys.platform != "darwin":
            self.show_message_box(QMessageBox.Warning, "提示", "此功能仅在 macOS 系统上可用")
            return
        
        try:
            # 查找数据库文件
            found_files = self._find_messages_database()
            
            if not found_files:
                self.show_message_box(QMessageBox.Warning, "诊断结果", 
                    "❌ 未找到任何 Messages 数据库文件\n\n"
                    "可能的原因:\n"
                    "1. 从未使用过 iMessage\n"
                    "2. 数据库文件在其他位置\n"
                    "3. 需要先登录 iMessage 并发送/接收消息")
                return
            
            # 收集诊断信息
            report_lines = []
            report_lines.append("=" * 60)
            report_lines.append("macOS Messages 数据库诊断报告")
            report_lines.append("=" * 60)
            report_lines.append(f"诊断时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            
            fixable_issues = []
            
            for path, size in found_files:
                report_lines.append(f"\n📁 {path}")
                
                if size == -1:
                    report_lines.append("   ⚠️  无法访问（可能是权限问题）")
                    fixable_issues.append(("permission", path))
                elif size == 0:
                    report_lines.append("   ⚠️  文件为空（0字节）")
                    report_lines.append("   提示: 需要先使用 iMessage 发送/接收消息来初始化数据库")
                else:
                    size_mb = size / 1024 / 1024
                    report_lines.append(f"   大小: {size} 字节 ({size_mb:.2f} MB)")
                    info = self._check_database(path)
                    if info["readable"]:
                        report_lines.append(f"   ✅ 可读取")
                        report_lines.append(f"   表数量: {len(info['all_tables'])}")
                        if info["has_message_table"]:
                            report_lines.append(f"   ✅ 包含 'message' 表（可以使用）")
                        else:
                            report_lines.append(f"   ❌ 不包含 'message' 表")
                            if info["all_tables"]:
                                report_lines.append(f"   数据库中的表: {', '.join(info['all_tables'][:10])}")
                    else:
                        report_lines.append(f"   ❌ 无法读取")
                        if "error" in info:
                            error_msg = info['error']
                            report_lines.append(f"   错误: {error_msg}")
                            if "database" in error_msg.lower() or "corrupt" in error_msg.lower() or "locked" in error_msg.lower():
                                fixable_issues.append(("corrupt", path))
                            else:
                                fixable_issues.append(("permission", path))
            
            # 推荐使用的路径
            valid_files = [(f[0], f[1]) for f in found_files if f[1] > 0 and os.path.exists(f[0])]
            if valid_files:
                for path, size in valid_files:
                    info = self._check_database(path)
                    if info.get("has_message_table"):
                        report_lines.append("\n" + "=" * 60)
                        report_lines.append(f"✅ 推荐使用: {path}")
                        report_lines.append("=" * 60)
                        break
            
            # 保存报告到程序系统文件夹（datapath）
            # 格式：Diag_DB1228 (月/日)
            date_str = datetime.now().strftime("%m%d")
            logfile = os.path.join(self.reports_dir, f"Diag_DB{date_str}.log")
            # 记录上一次报告路径
            self.last_db_diag_report = logfile
            with open(logfile, "w", encoding="utf-8") as f:
                f.write("\n".join(report_lines))
            
            # 显示报告摘要并询问是否修复（完整报告在文件中）
            # 只显示前15行摘要，避免弹窗过大
            summary_lines = report_lines[:15]
            summary_text = "\n".join(summary_lines)
            if len(report_lines) > 15:
                summary_text += "\n\n... (更多内容请查看完整报告文件)"
            
            if fixable_issues:
                reply = self.show_message_box(QMessageBox.Question, "诊断完成", 
                    f"{summary_text}\n\n"
                    f"发现 {len(fixable_issues)} 个可修复的问题。\n\n"
                    f"完整报告已保存到: {logfile}\n"
                    f"是否尝试自动修复？", 
                    QMessageBox.Yes | QMessageBox.No)
                
                if reply == QMessageBox.Yes:
                    self._fix_database_issues(fixable_issues)
            else:
                self.show_message_box(QMessageBox.Information, "诊断完成", 
                    f"{summary_text}\n\n"
                    f"完整报告已保存到: {logfile}")
            
            # 打开日志文件
            if os.path.exists(logfile):
                subprocess.run(["open", logfile])
            
            if hasattr(self.main_window, 'system_log'):
                self.main_window.system_log("数据库诊断已完成")
                
        except Exception as e:
            self.show_message_box(QMessageBox.Critical, "错误", f"执行数据库诊断时出错: {str(e)}")

    def _fix_database_issues(self, fixable_issues):
        """修复数据库问题"""
        results = []
        for issue_type, path in fixable_issues:
            if issue_type == "permission":
                success, message = self._fix_database_permissions(path)
                if success:
                    results.append(f"✅ {path}: {message}")
                else:
                    results.append(f"❌ {path}: {message}")
                    if "管理员权限" in message:
                        results.append("   提示: 请使用管理员权限运行")
            elif issue_type == "corrupt":
                success, message, backup = self._repair_database(path)
                if success:
                    results.append(f"✅ {path}: {message}")
                    if backup:
                        results.append(f"   📦 备份文件: {backup}")
                else:
                    results.append(f"❌ {path}: {message}")
                    if backup:
                        results.append(f"   📦 备份文件: {backup}")
        
        result_text = "\n".join(results)
        self.show_message_box(QMessageBox.Information, "修复完成", 
            f"{result_text}\n\n"
            f"建议重新运行诊断以确认问题已解决。")

    # endregion

    # region 清空收件箱（使用线程）

    def _clear_imessage_inbox_thread(self):
        """清空收件箱的线程函数"""
        try:
            HOME = os.path.expanduser("~")
            db_path = os.path.join(HOME, "Library/Messages/chat.db")
            attachments_path = os.path.join(HOME, "Library/Messages/Attachments")
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                c.execute("DELETE FROM message")
                c.execute("DELETE FROM chat")
                c.execute("DELETE FROM chat_message_join")
                conn.commit()
                conn.close()
            if os.path.exists(attachments_path):
                shutil.rmtree(attachments_path)
            if hasattr(self.main_window, 'system_log'):
                self.main_window.system_log("收件箱已清空")
            # 使用QTimer在主线程中显示弹窗
            QTimer.singleShot(0, lambda: self.show_message_box(QMessageBox.Information, "完成", "✅ 收件箱已清空"))
        except Exception as e:
            error_msg = f"清空收件箱失败: {str(e)}"
            if hasattr(self.main_window, 'system_log'):
                self.main_window.system_log(error_msg)
            QTimer.singleShot(0, lambda: self.show_message_box(QMessageBox.Critical, "错误", error_msg))

    def clear_imessage_inbox(self):
        """清空收件箱（带线程管理）"""
        reply = self.show_message_box(QMessageBox.Warning, "警告", "此操作将删除所有 iMessage 聊天记录及附件，不可恢复。继续？", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.No: 
            return
        if hasattr(self.main_window, 'system_log'):
            self.main_window.system_log("开始清空收件箱...")
        # 在后台线程中运行
        thread = threading.Thread(target=self._clear_imessage_inbox_thread, daemon=True)
        thread.start()

    # endregion

# endregion


# region  ending

if __name__ == "__main__":

 
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    
    app = QApplication(sys.argv)
  
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec_())

# endregion