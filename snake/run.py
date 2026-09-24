"""Run the game and, if needed, its existing local Laya model service."""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_LAYA_URL = "http://127.0.0.1:8765"
children = []


def health(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def stop(*_):
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def interrupted(*_):
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if health("http://127.0.0.1:8766/api/health"):
            print("游戏已运行：http://127.0.0.1:8766", flush=True)
            return
        laya_url = (os.environ.get("SNAKE_LAYA_URL") or os.environ.get("LAYA_SNAKE_LAYA_URL") or DEFAULT_LAYA_URL).rstrip("/")
        model = health(f"{laya_url}/health")
        if not model or model.get("status") != "ready":
            if laya_url != DEFAULT_LAYA_URL:
                raise RuntimeError(f"Laya 服务未就绪：{laya_url}")
            server = ROOT.parent / "laya-zh" / "server.py"
            child = subprocess.Popen([sys.executable, str(server), "--host", "127.0.0.1", "--port", "8765"], cwd=server.parent)
            children.append(child)
            print("正在加载本地 Laya…", flush=True)
            for _ in range(90):
                model = health(f"{laya_url}/health")
                if model and model.get("status") == "ready":
                    break
                if child.poll() is not None:
                    raise RuntimeError("Laya 服务退出，请检查上方输出。")
                time.sleep(1)
            else:
                raise RuntimeError("Laya 在 90 秒内未就绪。")
        child = subprocess.Popen([sys.executable, str(ROOT / "app.py")], cwd=ROOT)
        children.append(child)
        print("游戏地址：http://127.0.0.1:8766 · Ctrl+C 停止本次启动的服务", flush=True)
        code = child.wait()
        if code:
            raise RuntimeError(f"游戏服务退出，退出码 {code}")
    except KeyboardInterrupt:
        print("\n正在停止游戏。", flush=True)
    finally:
        stop()


if __name__ == "__main__":
    main()
