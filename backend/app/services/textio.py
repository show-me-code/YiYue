"""LaTeX 源码的宽容读取与规范化写回。

arXiv 源码包里混着各种编码：多数是 UTF-8，也有 1990 年代论文用 latin-1 写
`\\'e`/`\\"u` 这类单字节字符，还有历史遗留的坏字节。改用
`read_text("utf-8", errors="replace")` 会把这些字节变成 U+FFFD，一旦再以
UTF-8 写回（翻译管线每个写回点都是这样），**原文里的重音字母就被永久改成
问号菱形**——输出 PDF 里出现"�"，且无法从产物恢复。

这里的策略是"无损解码 + 统一以 UTF-8 输出"：UTF-8 优先；失败则用 cp1252 /
latin-1 逐字节还原（单字节编码永不失真），XeTeX 本来就要求 UTF-8 输入，
所以转成 UTF-8 写回是必要的、也是无损的。

**换行统一为 \\n**（与 Python 文本模式读取的"通用换行"一致）：
`read_bytes()` 不做这个转换，拿到 `\\r\\n` 后 `write_text()` 在 Windows 上会把
`\\n` 再翻成 `\\r\\n`，于是每一行都变成 `\\r\\r\\n`。TeX 把 CR 和 LF 都当行尾，
`\\r\\r\\n` 就成了**空行 = 段落结束**——整篇源码每一行都断成一段，实测直接报
`! Paragraph ended before \\author was complete`，arXiv 引擎全线回退。
"""

from pathlib import Path

# 单字节编码的回退顺序：cp1252 覆盖西文引号/破折号，latin-1 兜底（必然成功）
_FALLBACK_ENCODINGS = ("cp1252", "latin-1")


def normalize_newlines(text: str) -> str:
    """与 Python 文本模式（newline=None）一致的通用换行：CRLF/CR → LF。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """返回 (文本, 实际使用的编码)。保证不产生 U+FFFD。"""
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    # 理论上不可达（latin-1 能解任何字节）
    return raw.decode("utf-8", errors="replace"), "utf-8"


def decode_source_bytes(raw: bytes) -> str:
    """解码源码字节并统一换行（供 gz 单文件等无 Path 的场合使用）。"""
    return normalize_newlines(decode_bytes(raw)[0])


def load_text(path: str | Path) -> str:
    """无损读取文本文件（不产生替换字符，且换行已统一为 \\n）。"""
    return normalize_newlines(decode_bytes(Path(path).read_bytes())[0])


def save_text(path: str | Path, text: str) -> None:
    """以 UTF-8 写回（XeTeX/ctex 的要求）。"""
    Path(path).write_text(text, "utf-8")
