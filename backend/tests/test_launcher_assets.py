"""启动脚本的回归防护：批处理必须是 cmd 能正确解析的形式。

背景：把中文写进 .bat 时，cmd 按系统 ANSI 代码页解析 UTF-8 字节会得到乱码，
乱码里若出现命令分隔符，整行会被当成命令执行 —— 表现为"双击没反应"、
连 launch.log 都没写。中文提示统一由 launch.py 输出（chcp 65001 下是安全的）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BAT = ROOT / "启动译阅.bat"
LAUNCH = ROOT / "scripts" / "launch.py"
SH = ROOT / "scripts" / "start.sh"


def test_batch_is_pure_ascii_and_crlf():
    data = BAT.read_bytes()
    bad = [b for b in data if b > 127]
    assert not bad, f"批处理含非 ASCII 字节（cmd 会按 ANSI 解析而乱码）：{bad[:10]}"
    assert b"\r\n" in data, "批处理应使用 CRLF 行尾"
    text = data.decode("ascii")
    assert ".venv\\Scripts\\python.exe" in text, "应优先用项目自带虚拟环境"
    assert "launch.py" in text
    assert "launch.log" in text, "失败时应提示日志位置"


def test_shell_launcher_prefers_venv():
    text = SH.read_text("utf-8")
    assert ".venv/bin/python" in text and "launch.py" in text


def test_launch_script_has_stale_port_cleanup():
    text = LAUNCH.read_text("utf-8")
    assert "def port_holder_pids" in text
    assert "free_stale_port()" in text, "启动前应清理占用端口但不健康的残留进程"
    assert "launch.log" in text, "启动器应写日志便于排查"
