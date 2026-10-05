"""pdfops 单元测试：切分/合并/页码解析。"""

import pytest

from app.services import pdfops
from tests.conftest import make_sample_pdf


def test_page_count(tmp_path):
    p = make_sample_pdf(tmp_path / "s.pdf", pages=3)
    assert pdfops.page_count(p) == 3


def test_split_and_merge(tmp_path):
    src = make_sample_pdf(tmp_path / "s.pdf", pages=5)
    parts = pdfops.split_pdf(src, tmp_path / "parts", 2)
    assert [pdfops.page_count(p) for p in parts] == [2, 2, 1]
    merged = tmp_path / "merged.pdf"
    pdfops.merge_pdfs(parts, merged)
    assert pdfops.page_count(merged) == 5


def test_parse_page_ranges():
    assert pdfops.parse_page_ranges("1,3-5") == [(1, 1), (3, 5)]
    assert pdfops.parse_page_ranges("2-") == [(2, 10**9)]
    assert pdfops.parse_page_ranges("-3") == [(1, 3)]
    assert pdfops.parse_page_ranges("4") == [(4, 4)]
    with pytest.raises(ValueError):
        pdfops.parse_page_ranges("5-3")
    with pytest.raises(ValueError):
        pdfops.parse_page_ranges("  ")
