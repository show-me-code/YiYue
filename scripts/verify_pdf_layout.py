"""闭环版式验证命令行（用户规格第 5 步）。

实现复用 `backend/app/services/verify.py`（引擎流水线里跑的是同一套逻辑），
避免脚本与产品代码两套实现漂移。

用法（在 backend/ 目录下）：
    uv run python ../scripts/verify_pdf_layout.py <translated.pdf> <baseline.pdf> [--tol=25] [--out=DIR]

- translated.pdf：译文（引擎编译产物，如 data/tasks/<id>/mono.pdf）
- baseline.pdf  ：版式基线 —— 同一份源码"未翻译"编译出的 PDF（引擎的 arxiv_probe/main.pdf）
  用它而不是 arXiv 上的原始 PDF：同源编译才有可比性，也才能区分
  "作者原版瑕疵"与"我们引入的问题"。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services import verify  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    tol = verify.DEFAULT_TOL_PT
    out_dir: Path | None = None
    for a in sys.argv[1:]:
        if a.startswith("--tol="):
            tol = float(a.split("=", 1)[1])
        elif a.startswith("--out="):
            out_dir = Path(a.split("=", 1)[1])
    if len(args) < 2:
        sys.exit("用法: verify_pdf_layout.py <translated.pdf> <baseline.pdf> "
                 "[--tol=25] [--out=DIR]")

    translated, baseline = Path(args[0]), Path(args[1])
    if out_dir is None:
        out_dir = translated.parent / "verify"
    report = verify.verify_translation(
        translated, baseline, out_dir, tol=tol,
        translated_log=translated.parent / "main.log",
        baseline_log=baseline.with_suffix(".log"),
    )
    print(verify.summary_text(report))
    return 0 if report["verdict"] == "通过" else 2


if __name__ == "__main__":
    sys.exit(main())
