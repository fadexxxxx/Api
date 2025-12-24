# Gunicorn 配置文件
import os

# 绑定地址和端口
bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"

# Worker 配置
workers = 4
worker_class = "gevent"  
worker_connections = 1000
timeout = 120

# 日志配置
loglevel = "info"
accesslog = "-"  
errorlog = "-" 
