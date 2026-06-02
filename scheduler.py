#!/usr/bin/env python
"""LOF套利监测 - 定时启动脚本
每个交易日下午 14:40 运行，刷新数据并在浏览器中打开页面。
配合系统定时任务使用（Windows 任务计划程序 / macOS launchd / Linux cron）
"""

import subprocess
import sys
import time
import webbrowser
import urllib.request

PORT = 8877
BASE_URL = f"http://localhost:{PORT}"


def server_running():
    try:
        urllib.request.urlopen(f"{BASE_URL}/api/config", timeout=3)
        return True
    except Exception:
        return False


def start_server():
    print("[启动] 正在启动 LOF 监测服务...")
    subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=".",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(15):
        time.sleep(1)
        if server_running():
            print("[启动] 服务就绪")
            return True
    print("[错误] 服务启动超时")
    return False


def refresh_data():
    print("[刷新] 正在拉取最新数据...")
    try:
        urllib.request.urlopen(f"{BASE_URL}/api/opportunities?refresh=1", timeout=90)
        print("[刷新] 完成")
        return True
    except Exception as e:
        print(f"[错误] 刷新失败: {e}")
        return False


def open_browser():
    print("[打开] 正在打开浏览器...")
    webbrowser.open(BASE_URL)


def main():
    print("=" * 50)
    print(f"LOF 套利监测 - {time.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    if not server_running():
        if not start_server():
            sys.exit(1)
    else:
        print("[检测] 服务已在运行")

    if refresh_data():
        open_browser()

    print("[完成]")


if __name__ == "__main__":
    main()
