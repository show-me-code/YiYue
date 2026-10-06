"""译阅一键启动器：装依赖 → 构建前端 → 起服务 → 等就绪 → 自动打开浏览器。

由根目录的 启动译阅.bat / scripts/start.sh 调用；也可直接运行：
    cd backend && uv run python ../scripts/launch.py
仅用标准库。
"""

import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST, PORT = "127.0.0.1", 8000
URL = f"http://{HOST}:{PORT}"


def _is_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/api/health", timeout=1):
            return True
    except Exception:
        return False


def ensure_frontend() -> None:
    if (ROOT / "frontend" / "dist").exists():
        return
    print("首次运行：构建前端（需要 Node.js，约 1 分钟）...")
    fe = ROOT / "frontend"
    try:
        subprocess.run("npm install", shell=True, cwd=fe, check=True)
        subprocess.run("npm run build", shell=True, cwd=fe, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(f"前端构建失败（退出码 {e.returncode}）：请确认已安装 Node.js ≥ 18")


def apply_babeldoc_patches() -> None:
    """应用 BabelDOC 排版修复补丁（幂等，须在引擎导入前执行）。"""
    script = ROOT / "scripts" / "apply_babeldoc_patches.py"
    if not script.exists():
        return
    try:
        subprocess.run([sys.executable, str(script)], cwd=ROOT / "backend",
                       check=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"警告：排版补丁应用失败（{e}），翻译质量可能受影响")


def start_server() -> subprocess.Popen:
    print(f"启动服务 {URL} ...")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", HOST, "--port", str(PORT)],
        cwd=ROOT / "backend",
    )


def wait_ready(timeout_s: float = 60) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _is_alive():
            return True
        time.sleep(0.5)
    return False


def main() -> None:
    if _is_alive():
        print("译阅已在运行，直接打开页面。")
        webbrowser.open(URL)
        return

    ensure_frontend()
    apply_babeldoc_patches()
    proc = start_server()
    try:
        if not wait_ready():
            proc.terminate()
            sys.exit("服务启动超时：请查看上方日志排查（如端口被占用可改 launch.py 中的 PORT）。")
        print(f"译阅已就绪：{URL}  （关闭本窗口即停止服务）")
        webbrowser.open(URL)
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    main()
