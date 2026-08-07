import os
import http.server
import socketserver
import threading
import time
import ngrok
from dotenv import load_dotenv   # 如果使用 .env 文件

# ---------- 加载环境变量（如使用 .env） ----------
load_dotenv()

# ---------- 配置 ----------
# 定义三个要暴露的文件夹及其对应的本地端口
SERVICES = [
    {"folder": "./project_a", "port": 8001},
    {"folder": "./project_b", "port": 8002},
    {"folder": "./project_c", "port": 8003},
]

# ---------- 1. 启动本地文件服务器（每个在独立线程中） ----------
def start_file_server(folder, port):
    """在指定端口启动一个简单的 HTTP 文件服务器"""
    os.chdir(folder)  # 切换到目标文件夹
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        print(f"[本地] 文件夹 '{folder}' 已启动，地址: http://localhost:{port}")
        httpd.serve_forever()

# 启动所有本地服务
threads = []
for svc in SERVICES:
    t = threading.Thread(
        target=start_file_server,
        args=(svc["folder"], svc["port"]),
        daemon=True
    )
    t.start()
    threads.append(t)
    time.sleep(0.1)  # 避免端口冲突的小延迟

# ---------- 2. 使用官方 SDK 创建 3 个公网隧道 ----------
# 从环境变量自动读取 NGROK_AUTHTOKEN
# 也可手动传入 authtoken="你的token"
listeners = []
for svc in SERVICES:
    # forward() 会创建一个公网端点，将流量转发到 localhost:port
    listener = ngrok.forward(f"localhost:{svc['port']}", authtoken_from_env=True, pooling_enabled=True)
    public_url = listener.url()  # 获取公网地址，例如 https://xxxx.ngrok-free.app
    listeners.append(listener)
    print(f"[公网] 文件夹 '{svc['folder']}' -> {public_url}")

# ---------- 3. 保持主程序运行 ----------
print("\n所有服务已暴露，按 Ctrl+C 退出...")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("正在关闭所有隧道...")
    # 逐个断开监听器
    for listener in listeners:
        ngrok.disconnect(listener)
    print("已退出。")