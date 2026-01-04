#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Redis状态管理器"""

import os
import json
import time
import threading
import logging
from typing import Any, Dict, List, Optional, Set, Union
from datetime import datetime, timedelta

import redis
from redis import Redis

logger = logging.getLogger(__name__)


class RedisManager:
    """Redis状态管理器（支持内存降级）"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_redis()
            return cls._instance
    
    def _init_redis(self):
        """初始化Redis连接"""
        self.redis_url = os.environ.get("REDIS_URL")
        
        # 🔥 开发环境：如果没有设置 REDIS_URL，尝试使用默认的本地Redis连接
        if not self.redis_url:
            is_dev = os.environ.get("ENV", "").lower() in ["dev", "development", "local"]
            if is_dev:
                # 使用默认的本地Redis连接（Docker端口：6379）
                redis_host = os.environ.get("REDIS_HOST", "localhost")
                redis_port = os.environ.get("REDIS_PORT", "6379")
                redis_password = os.environ.get("REDIS_PASSWORD", "")
                
                if redis_password:
                    self.redis_url = f"redis://{redis_password}@{redis_host}:{redis_port}"
                else:
                    self.redis_url = f"redis://{redis_host}:{redis_port}"
                logger.info(f"[DEV] REDIS_URL 未设置，使用默认开发环境配置: redis://{redis_host}:{redis_port}")
        
        self.use_redis = bool(self.redis_url)
        
        if self.use_redis:
            try:
                # 解析Redis URL
                parsed = self.redis_url.replace("redis://", "")
                if "@" in parsed:
                    # 有密码的情况: password@host:port
                    auth_part, host_part = parsed.split("@", 1)
                    password = auth_part
                    if ":" in host_part:
                        host, port = host_part.split(":", 1)
                        port = int(port)
                    else:
                        host = host_part
                        port = 6379
                    self.client = Redis(
                        host=host,
                        port=port,
                        password=password,
                        decode_responses=True,
                        socket_connect_timeout=2,  # 🔥 连接超时：2秒（快速失败）
                        socket_timeout=1,  # 🔥 操作超时：1秒（快速失败）
                        retry_on_timeout=False,  # 🔥 超时不重试
                        health_check_interval=30  # 🔥 健康检查间隔：30秒
                    )
                else:
                    # 无密码的情况: host:port
                    if ":" in parsed:
                        host, port = parsed.split(":", 1)
                        port = int(port)
                    else:
                        host = parsed
                        port = 6379
                    self.client = Redis(
                        host=host,
                        port=port,
                        decode_responses=True,
                        socket_connect_timeout=2,  # 🔥 连接超时：2秒（快速失败）
                        socket_timeout=1,  # 🔥 操作超时：1秒（快速失败）
                        retry_on_timeout=False,  # 🔥 超时不重试
                        health_check_interval=30  # 🔥 健康检查间隔：30秒
                    )
                
                # 🔥 测试连接（快速失败）
                try:
                    self.client.ping()
                    logger.info("✅ Redis连接成功")
                except Exception as ping_error:
                    logger.warning(f"Redis ping失败: {ping_error}，降级到内存模式")
                    self.use_redis = False
                    self.client = None
                    return
                
            except Exception as e:
                logger.error(f"❌ Redis连接失败: {e}, 降级到内存模式")
                self.use_redis = False
                self.client = None
        else:
            logger.warning("⚠️ REDIS_URL未设置，使用内存模式")
            self.client = None
        
        # 内存后备存储（仅当Redis不可用时）
        self._memory_store = {
            "online_workers": set(),
            "worker_data": {},
            "worker_load": {},
            "frontend_subs": {},
            "task_subs": {},
            "locks": {}
        }
        self._memory_lock = threading.Lock()
        
        # 自动重连机制
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 3  # 最多尝试3次重连
        self._last_reconnect_time = 0
        self._reconnect_cooldown = 60  # 重连冷却期60秒
    
    def _reconnect(self) -> bool:
        """尝试重新连接Redis"""
        current_time = time.time()
        
        # 检查冷却期
        if current_time - self._last_reconnect_time < self._reconnect_cooldown:
            return False
        
        # 检查重连次数
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.warning(f"Redis重连尝试已达上限({self._max_reconnect_attempts}次)，使用内存模式")
            return False
        
        self._last_reconnect_time = current_time
        self._reconnect_attempts += 1
        
        logger.info(f"尝试重新连接Redis (第{self._reconnect_attempts}次)...")
        
        try:
            # 重新初始化Redis连接
            self._init_redis()
            
            if self.use_redis and self.client:
                # 测试连接
                self.client.ping()
                logger.info("✅ Redis重连成功")
                # 重置重连计数器
                self._reconnect_attempts = 0
                return True
            else:
                logger.warning("Redis重连失败：配置不可用")
                return False
                
        except Exception as e:
            logger.error(f"Redis重连失败: {e}")
            self.use_redis = False
            self.client = None
            return False
    
    # ==================== Worker管理 ====================
    
    def register_worker(self, server_id: str, data: Dict[str, Any]) -> bool:
        """注册Worker（心跳）"""
        return self.worker_online(server_id, data)
    
    def worker_online(self, server_id: str, info: Dict[str, Any]) -> bool:
        """标记Worker在线（兼容API调用）"""
        if self.use_redis and self.client:
            try:
                # 存储Worker数据
                worker_key = f"worker:{server_id}"
                pipe = self.client.pipeline()
                
                # 准备数据，处理各种类型
                worker_data = {}
                for key, value in info.items():
                    if isinstance(value, bool):
                        worker_data[key] = "1" if value else "0"
                    elif isinstance(value, (int, float)):
                        worker_data[key] = str(value)
                    elif isinstance(value, dict):
                        worker_data[key] = json.dumps(value) if not isinstance(value, str) else value
                    elif value is None:
                        worker_data[key] = ""
                    else:
                        worker_data[key] = str(value)
                
                # 确保必要字段存在
                if "server_name" not in worker_data:
                    worker_data["server_name"] = info.get("server_name", server_id)
                if "ready" not in worker_data:
                    worker_data["ready"] = "1" if info.get("ready", False) else "0"
                if "clients_count" not in worker_data:
                    worker_data["clients_count"] = str(info.get("clients_count", 0))
                if "load" not in worker_data:
                    worker_data["load"] = str(info.get("load", 0))
                if "last_seen" not in worker_data:
                    worker_data["last_seen"] = str(time.time())
                
                pipe.hset(worker_key, mapping=worker_data)
                # 设置30秒过期
                pipe.expire(worker_key, 30)
                # 添加到在线集合
                pipe.sadd("online_workers", server_id)
                pipe.execute()
                return True
            except Exception as e:
                logger.error(f"Redis注册Worker失败: {e}")
                return False
        else:
            # 内存模式
            with self._memory_lock:
                self._memory_store["online_workers"].add(server_id)
                self._memory_store["worker_data"][server_id] = {
                    **info,
                    "last_seen": time.time()
                }
            return True
    
    def update_worker_heartbeat(self, server_id: str, data: Dict[str, Any] = None) -> bool:
        """更新Worker心跳"""
        return self.update_heartbeat(server_id, data)
    
    def update_heartbeat(self, server_id: str, data: Dict[str, Any] = None) -> bool:
        """更新心跳（兼容API调用）"""
        if self.use_redis and self.client:
            try:
                worker_key = f"worker:{server_id}"
                # 检查worker是否存在
                if not self.client.exists(worker_key):
                    # 如果不存在，重新注册
                    if data:
                        return self.worker_online(server_id, data)
                    return False
                
                if data:
                    # 更新完整数据
                    update_data = {}
                    for key, value in data.items():
                        if isinstance(value, bool):
                            update_data[key] = "1" if value else "0"
                        elif isinstance(value, (int, float)):
                            update_data[key] = str(value)
                        elif isinstance(value, dict):
                            update_data[key] = json.dumps(value)
                        else:
                            update_data[key] = str(value)
                    update_data["last_seen"] = str(time.time())
                    self.client.hset(worker_key, mapping=update_data)
                else:
                    # 只更新时间
                    self.client.hset(worker_key, "last_seen", str(time.time()))
                # 续期
                self.client.expire(worker_key, 30)
                return True
            except Exception as e:
                logger.error(f"Redis更新心跳失败: {e}")
                return False
        else:
            # 内存模式
            with self._memory_lock:
                if server_id in self._memory_store["worker_data"]:
                    self._memory_store["worker_data"][server_id]["last_seen"] = time.time()
                    if data:
                        self._memory_store["worker_data"][server_id].update(data)
                else:
                    # 如果不存在，重新注册
                    if data:
                        return self.worker_online(server_id, data)
            return True
    
    def remove_worker(self, server_id: str) -> bool:
        """移除Worker"""
        return self.worker_offline(server_id)
    
    def worker_offline(self, server_id: str) -> bool:
        """标记Worker离线（兼容API调用）"""
        if self.use_redis and self.client:
            try:
                pipe = self.client.pipeline()
                pipe.delete(f"worker:{server_id}")
                pipe.srem("online_workers", server_id)
                pipe.delete(f"worker:{server_id}:load")
                pipe.execute()
                return True
            except Exception as e:
                logger.error(f"Redis移除Worker失败: {e}")
                return False
        else:
            # 内存模式
            with self._memory_lock:
                self._memory_store["online_workers"].discard(server_id)
                self._memory_store["worker_data"].pop(server_id, None)
                self._memory_store["worker_load"].pop(server_id, None)
            return True
    
    def get_online_workers(self, only_ready: bool = False) -> List[str]:
        """获取在线Worker列表（快速失败，不阻塞）"""
        if self.use_redis and self.client:
            try:
                # 🔥 使用快速超时，避免阻塞
                online_workers = list(self.client.smembers("online_workers"))
                if not only_ready:
                    return online_workers
                
                # 过滤出就绪的Worker（批量操作，避免循环查询）
                if not online_workers:
                    return []
                
                # 🔥 使用 pipeline 批量获取，减少 Redis 往返
                pipe = self.client.pipeline()
                for worker_id in online_workers:
                    worker_key = f"worker:{worker_id}"
                    pipe.hget(worker_key, "ready")
                results = pipe.execute()
                
                ready_workers = []
                for i, ready in enumerate(results):
                    if ready in ("1", "True", "true"):
                        ready_workers.append(online_workers[i])
                return ready_workers
            except Exception as e:
                # 🔥 Redis 失败时尝试重连，而不是永久禁用
                logger.warning(f"Redis获取在线Worker失败: {e}")
                # 尝试重连
                if self._reconnect():
                    # 重连成功，重试操作
                    try:
                        return self.get_online_workers(only_ready)
                    except:
                        pass
                # 重连失败或重试失败，降级到内存模式
                self.use_redis = False
                return []
        else:
            # 内存模式
            with self._memory_lock:
                workers = list(self._memory_store["online_workers"])
                if not only_ready:
                    return workers
                
                # 过滤就绪的Worker
                ready_workers = []
                for worker_id in workers:
                    worker_data = self._memory_store["worker_data"].get(worker_id)
                    if worker_data and worker_data.get("ready"):
                        ready_workers.append(worker_id)
                return ready_workers
    
    def get_worker_info(self, server_id: str) -> Optional[Dict[str, Any]]:
        """获取Worker信息（快速失败，不阻塞）"""
        if self.use_redis and self.client:
            try:
                worker_key = f"worker:{server_id}"
                # 🔥 快速获取，超时立即返回 None
                data = self.client.hgetall(worker_key)
                if not data:
                    return None
                
                # 解析ready状态（支持多种格式）
                ready_str = data.get("ready", "0")
                ready = ready_str in ("1", "True", "true", "True")
                
                # 解析meta（可能是JSON字符串或字典）
                meta_str = data.get("meta", "{}")
                try:
                    if isinstance(meta_str, str):
                        meta = json.loads(meta_str)
                    else:
                        meta = meta_str
                except:
                    meta = {}
                
                # 解析数据
                result = {
                    "server_name": data.get("server_name", server_id),
                    "ready": ready,
                    "clients_count": int(data.get("clients_count", 0)),
                    "last_seen": float(data.get("last_seen", 0)),
                    "load": int(data.get("load", 0)),
                    "meta": meta
                }
                return result
            except Exception as e:
                logger.error(f"Redis获取Worker信息失败: {e}")
                return None
        else:
            # 内存模式
            with self._memory_lock:
                return self._memory_store["worker_data"].get(server_id)
    
    # ==================== 负载管理 ====================
    
    def set_worker_load(self, server_id: str, load: int) -> bool:
        """设置Worker负载"""
        if self.use_redis and self.client:
            try:
                self.client.set(f"worker:{server_id}:load", load, ex=60)
                return True
            except Exception as e:
                logger.error(f"Redis设置负载失败: {e}")
                return False
        else:
            # 内存模式
            with self._memory_lock:
                self._memory_store["worker_load"][server_id] = {
                    "load": load,
                    "timestamp": time.time()
                }
            return True
    
    def incr_worker_load(self, server_id: str, amount: int = 1) -> int:
        """增加Worker负载"""
        if self.use_redis and self.client:
            try:
                key = f"worker:{server_id}:load"
                # 如果key不存在，先设置为0
                if not self.client.exists(key):
                    self.client.set(key, 0, ex=60)
                new_load = self.client.incrby(key, amount)
                self.client.expire(key, 60)
                return new_load
            except Exception as e:
                logger.error(f"Redis增加负载失败: {e}")
                return 0
        else:
            # 内存模式
            with self._memory_lock:
                current = self._memory_store["worker_load"].get(server_id, {}).get("load", 0)
                new_load = current + amount
                self._memory_store["worker_load"][server_id] = {
                    "load": new_load,
                    "timestamp": time.time()
                }
                return new_load
    
    def decr_worker_load(self, server_id: str, amount: int = 1) -> int:
        """减少Worker负载"""
        if self.use_redis and self.client:
            try:
                key = f"worker:{server_id}:load"
                new_load = self.client.decrby(key, amount)
                if new_load < 0:
                    self.client.set(key, 0, ex=60)
                    new_load = 0
                self.client.expire(key, 60)
                return new_load
            except Exception as e:
                logger.error(f"Redis减少负载失败: {e}")
                return 0
        else:
            # 内存模式
            with self._memory_lock:
                current = self._memory_store["worker_load"].get(server_id, {}).get("load", 0)
                new_load = max(0, current - amount)
                self._memory_store["worker_load"][server_id] = {
                    "load": new_load,
                    "timestamp": time.time()
                }
                return new_load
    
    def get_worker_load(self, server_id: str) -> int:
        """获取Worker负载（快速失败，不阻塞）"""
        if self.use_redis and self.client:
            try:
                load = self.client.get(f"worker:{server_id}:load")
                return int(load) if load else 0
            except Exception as e:
                # 🔥 Redis 失败时快速返回 0，不阻塞
                logger.warning(f"Redis获取负载失败: {e}，返回0")
                return 0
        else:
            # 内存模式
            with self._memory_lock:
                return self._memory_store["worker_load"].get(server_id, {}).get("load", 0)
    
    def get_best_worker(self, exclude: List[str] = None) -> Optional[str]:
        """获取最佳Worker（负载最轻的）"""
        online_workers = self.get_online_workers(only_ready=True)
        if not online_workers:
            return None
        
        if exclude:
            online_workers = [w for w in online_workers if w not in exclude]
        
        # 获取每个Worker的负载
        worker_loads = []
        for worker_id in online_workers:
            load = self.get_worker_load(worker_id)
            worker_loads.append((worker_id, load))
        
        if not worker_loads:
            return None
        
        # 选择负载最轻的
        best_worker = min(worker_loads, key=lambda x: x[1])[0]
        return best_worker
    
    # ==================== 分布式锁 ====================
    
    def acquire_lock(self, lock_key: str, timeout: int = 10) -> bool:
        """获取分布式锁"""
        if self.use_redis and self.client:
            try:
                # 使用SETNX实现锁
                lock_key = f"lock:{lock_key}"
                result = self.client.setnx(lock_key, "1")
                if result:
                    self.client.expire(lock_key, timeout)
                return result
            except Exception as e:
                logger.error(f"Redis获取锁失败: {e}")
                return False
        else:
            # 内存模式
            with self._memory_lock:
                lock_key = f"lock:{lock_key}"
                if lock_key in self._memory_store["locks"]:
                    return False
                self._memory_store["locks"][lock_key] = {
                    "expire": time.time() + timeout
                }
                return True
    
    def release_lock(self, lock_key: str) -> bool:
        """释放分布式锁"""
        if self.use_redis and self.client:
            try:
                lock_key = f"lock:{lock_key}"
                self.client.delete(lock_key)
                return True
            except Exception as e:
                logger.error(f"Redis释放锁失败: {e}")
                return False
        else:
            # 内存模式
            with self._memory_lock:
                lock_key = f"lock:{lock_key}"
                self._memory_store["locks"].pop(lock_key, None)
            return True
    
    def with_lock(self, lock_key: str, timeout: int = 10):
        """锁上下文管理器"""
        class LockContext:
            def __init__(self, manager, lock_key, timeout):
                self.manager = manager
                self.lock_key = lock_key
                self.timeout = timeout
                self.acquired = False
            
            def __enter__(self):
                self.acquired = self.manager.acquire_lock(self.lock_key, self.timeout)
                return self.acquired
            
            def __exit__(self, exc_type, exc_val, exc_tb):
                if self.acquired:
                    self.manager.release_lock(self.lock_key)
        
        return LockContext(self, lock_key, timeout)
    
    # ==================== 清理过期数据 ====================
    
    def cleanup_expired(self) -> Dict[str, int]:
        """清理过期数据"""
        cleaned = {}
        
        if self.use_redis and self.client:
            try:
                # Redis自动过期，只需要清理无效的在线记录
                online_workers = self.get_online_workers()
                expired_workers = []
                
                for worker_id in online_workers:
                    worker_key = f"worker:{worker_id}"
                    if not self.client.exists(worker_key):
                        expired_workers.append(worker_id)
                
                if expired_workers:
                    self.client.srem("online_workers", *expired_workers)
                    cleaned["expired_workers"] = len(expired_workers)
                
            except Exception as e:
                logger.error(f"Redis清理失败: {e}")
        else:
            # 内存模式清理
            with self._memory_lock:
                # 清理过期Worker（30秒无心跳）
                expired_workers = []
                current_time = time.time()
                
                for worker_id in list(self._memory_store["online_workers"]):
                    worker_data = self._memory_store["worker_data"].get(worker_id)
                    if not worker_data:
                        expired_workers.append(worker_id)
                    elif current_time - worker_data.get("last_seen", 0) > 30:
                        expired_workers.append(worker_id)
                
                for worker_id in expired_workers:
                    self._memory_store["online_workers"].discard(worker_id)
                    self._memory_store["worker_data"].pop(worker_id, None)
                    self._memory_store["worker_load"].pop(worker_id, None)
                
                cleaned["expired_workers"] = len(expired_workers)
                
                # 清理过期锁
                expired_locks = []
                for lock_key, lock_data in list(self._memory_store["locks"].items()):
                    if current_time > lock_data.get("expire", 0):
                        expired_locks.append(lock_key)
                
                for lock_key in expired_locks:
                    self._memory_store["locks"].pop(lock_key, None)
                
                cleaned["expired_locks"] = len(expired_locks)
        
        return cleaned
    
    # ==================== 任务状态缓存 ====================
    
    def cache_task_progress(self, task_id: str, progress: Dict[str, Any], ttl: int = 300) -> bool:
        """缓存任务进度（快速查询）"""
        if self.use_redis and self.client:
            try:
                key = f"task:{task_id}:progress"
                self.client.setex(key, ttl, json.dumps(progress))
                return True
            except Exception as e:
                logger.error(f"Redis缓存任务进度失败: {e}")
                return False
        # 内存模式暂不实现
        return False
    
    def get_task_progress(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务进度缓存"""
        if self.use_redis and self.client:
            try:
                key = f"task:{task_id}:progress"
                data = self.client.get(key)
                return json.loads(data) if data else None
            except Exception as e:
                logger.error(f"Redis获取任务进度失败: {e}")
                return None
        return None
    
    # ==================== 统计信息 ====================
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = {
            "use_redis": self.use_redis,
            "redis_connected": self.use_redis and self.client is not None
        }
        
        if self.use_redis and self.client:
            try:
                stats["online_workers"] = len(self.get_online_workers())
                stats["ready_workers"] = len(self.get_online_workers(only_ready=True))
                stats["redis_info"] = self.client.info()
            except:
                stats["redis_info"] = "unavailable"
        else:
            with self._memory_lock:
                stats["online_workers"] = len(self._memory_store["online_workers"])
                stats["memory_store_size"] = len(self._memory_store["worker_data"])
        
        return stats


# 全局单例实例
redis_manager = RedisManager()


# 清理线程
def start_cleanup_thread(interval: int = 60):
    """启动定期清理线程"""
    def cleanup_loop():
        while True:
            try:
                cleaned = redis_manager.cleanup_expired()
                if cleaned:
                    logger.info(f"清理过期数据: {cleaned}")
            except Exception as e:
                logger.error(f"清理线程错误: {e}")
            time.sleep(interval)
    
    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()
    return thread