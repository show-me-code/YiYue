"""译阅一键启动器：装依赖 → 构建前端 → 起服务 → 等就绪 → 自动打开浏览器。

由根目录的 启动译阅.bat / scripts/start.sh 调用；也可直接运行：
    cd backend && uv run python ../scripts/launch.py
仅用标准库。

排查用日志：data/launch.log（启动失败时先看这里）
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
LOG_PATH = ROOT / "backend" / "data" / "launch.log"


def log(msg: str) -> None:
    print(msg, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass  # 日志写不了不影响启动


def _is_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/api/health", timeout=1):
            return True
    except Exception:
        return False


def port_holder_pids(port: int = PORT) -> list[int]:
    """占用该端口且处于 LISTENING 的进程号（端口被"僵尸服务"占住会导致启动失败）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=10,
                             encoding="utf-8", errors="replace").stdout
    except Exception:  # noqa: BLE001
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                try:
                    pid = int(parts[4])
                except ValueError:
                    continue
                if pid not in pids and pid != 0:
                    pids.append(pid)
    return pids


def _pid_command_line(pid: int) -> str:
    """进程命令行，用来确认占用端口的确实是本应用的 uvicorn。"""
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}')"
                 ".CommandLine"],
                capture_output=True, text=True, timeout=25,
                encoding="utf-8", errors="replace").stdout
            return (out or "").strip()
        except Exception:  # noqa: BLE001
            return ""
    try:
        return (Path(f"/proc/{pid}/cmdline").read_bytes()
                .replace(b"\0", b" ").decode("utf-8", "replace")).strip()
    except OSError:
        return ""


def _is_our_server(cmd_line: str) -> bool:
    cmd = cmd_line.lower()
    return "uvicorn" in cmd and "app.main" in cmd


def free_stale_port() -> None:
    """健康检查不通过却占着端口 → 清掉它再启动，避免"点了没反应"。

    只结束**确认是本应用 uvicorn** 的进程：8000 是很常见的开发端口，
    无差别 taskkill 会把用户自己的服务杀掉。
    """
    for pid in port_holder_pids():
        cmd = _pid_command_line(pid)
        if not _is_our_server(cmd):
            log(f"注意：端口 {PORT} 被进程 {pid} 占用，但它不是译阅的 uvicorn"
                f"（命令行：{cmd[:120] or '未知'}）。不会自动结束它——"
                "请自行关闭该程序，或修改 scripts/launch.py 里的 PORT。")
            continue
        log(f"端口 {PORT} 被残留的译阅进程 {pid} 占用但服务不健康，正在清理…")
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        except Exception as e:  # noqa: BLE001
            log(f"清理进程 {pid} 失败：{e}")
    time.sleep(1)


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
        log("译阅已在运行，直接打开页面。")
        webbrowser.open(URL)
        return

    ensure_frontend()
    apply_babeldoc_patches()
    free_stale_port()
    log(f"启动服务 {URL} …")
    proc = start_server()
    try:
        if not wait_ready():
            proc.terminate()
            log(f"服务启动超时。占用 {PORT} 的进程：{port_holder_pids() or '无'}")
            log("常见原因：端口被别的程序占用、依赖缺失、或应用启动报错。")
            log(f"详细日志见：{LOG_PATH}")
            sys.exit(1)
        log(f"译阅已就绪：{URL}  （关闭本窗口即停止服务）")
        webbrowser.open(URL)
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    main()
