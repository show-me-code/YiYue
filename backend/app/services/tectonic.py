"""Tectonic 编译器管理：按需下载 + LaTeX 编译。

Tectonic 是自包含的 XeTeX 引擎（约 21MB），首次编译时按需下载宏包。
Windows 二进制来自 GitHub Release；国内网络不畅时可在 data/bin/ 手动放置
tectonic.exe。
"""

import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx

from app import config

logger = logging.getLogger(__name__)

BIN_DIR = config.DATA_DIR / "bin"
TECTONIC_EXE = BIN_DIR / ("tectonic.exe" if shutil.os.name == "nt" else "tectonic")
DOWNLOAD_URL = (
    "https://github.com/tectonic-typesetting/tectonic/releases/download/"
    "tectonic%400.17.0/tectonic-0.17.0-x86_64-pc-windows-msvc.zip"
)


def tectonic_path() -> Path | None:
    """已安装的 tectonic 可执行文件；优先用户 PATH。"""
    if TECTONIC_EXE.exists():
        return TECTONIC_EXE
    which = shutil.which("tectonic")
    if which:
        return Path(which)
    return None


def download_tectonic() -> dict:
    """下载并解压 Tectonic 到 data/bin/（阻塞，放线程执行）。"""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=600, follow_redirects=True) as client:
        r = client.get(DOWNLOAD_URL)
        r.raise_for_status()
        zip_path = BIN_DIR / "tectonic.zip"
        zip_path.write_bytes(r.content)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(BIN_DIR)
    zip_path.unlink(missing_ok=True)
    if not TECTONIC_EXE.exists():
        # 某些版本解压在子目录，找一层
        for p in BIN_DIR.rglob("tectonic.exe"):
            p.replace(TECTONIC_EXE)
            break
    if not TECTONIC_EXE.exists():
        raise RuntimeError("解压后未找到 tectonic.exe")
    return {"ready": True, "path": str(TECTONIC_EXE)}


def compile_tex(main_tex: Path, out_dir: Path, timeout_s: int = 900) -> Path:
    """编译 main.tex（含一次日志驱动修复重试），返回生成的 PDF 路径。"""
    from app.services import tex_translate

    main_tex = Path(main_tex).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_tail = ""
    for attempt in range(2):
        try:
            return _compile_once(main_tex, out_dir, timeout_s)
        except RuntimeError as e:
            log_tail = str(e)
            log_file = out_dir / "compile.log"
            log_text = log_file.read_text("utf-8", errors="replace") if log_file.exists() else ""
            if attempt == 0:
                repaired = tex_translate.repair_from_log(
                    main_tex.read_text("utf-8", errors="replace"), log_text
                )
                if repaired is not None:
                    logger.info("applying compile repair and retrying")
                    main_tex.write_text(repaired, "utf-8")
                    continue
            raise
    raise RuntimeError(log_tail or "编译失败")


def find_broken_input_file(log_path: Path, src_root: Path) -> Path | None:
    """从 LaTeX 日志定位报错时正在处理的 \input 文件（供上层还原重试）。"""
    try:
        text = log_path.read_text("utf-8", errors="replace")
    except OSError:
        return None
    last_file = None
    for m in re.finditer(r"\((\./[^()\s]+\.tex)", text):
        last_file = m.group(1)
    if not last_file:
        return None
    cand = (src_root / last_file[2:]).resolve()
    return cand if cand.exists() else None


def _compile_once(main_tex: Path, out_dir: Path, timeout_s: int) -> Path:
    exe = tectonic_path()
    if exe is None:
        raise RuntimeError("Tectonic 未安装")
    cmd = [str(exe), "--outdir", str(out_dir), "--keep-logs", str(main_tex)]
    try:
        proc = subprocess.run(
            cmd, cwd=str(main_tex.parent), capture_output=True, text=True,
            timeout=timeout_s, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"编译超时（{timeout_s}s）") from e
    pdf = out_dir / (main_tex.stem + ".pdf")
    if proc.returncode != 0 or not pdf.exists():
        log = proc.stdout[-3000:] + "\n" + proc.stderr[-3000:]
        (out_dir / "compile.log").write_text(log, "utf-8")
        raise RuntimeError(
            f"Tectonic 编译失败（exit {proc.returncode}）：{_extract_error_summary(out_dir)}")
    return pdf


def _extract_error_summary(out_dir: Path) -> str:
    """从 LaTeX 日志提取真实错误行（"!" 开头）与 tectonic error 行。"""
    parts: list[str] = []
    for log_file in sorted(out_dir.glob("*.log")) + [out_dir / "compile.log"]:
        try:
            text = log_file.read_text("utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"^(! .{0,200})", text, re.M):
            line = m.group(1).strip()
            if line not in parts:
                parts.append(line)
        for m in re.finditer(r"^(error: .{0,200})", text, re.M):
            line = m.group(1).strip()
            if line not in parts:
                parts.append(line)
    summary = " | ".join(parts[:5]) if parts else "未知错误（详见 compile.log）"
    return summary[:500]
