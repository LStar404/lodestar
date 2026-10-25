"""CLI 省略 --doc-id 的回归（Review #10）：默认 doc_id 必须在命名空间检查前归一。

基线复现：parse_pdf(sample_pdf, out_dir) 不传 doc_id → doc_namespace(None) →
AttributeError: 'NoneType' object has no attribute 'encode'（命名空间检查在默认 ID
生成之前）。修好后默认 ID 为 doc-<pdf_sha256 前 12 位>，并贯穿 pages/manifest 路径。
"""

from __future__ import annotations

import hashlib

from src.core.paths import Layout
from src.ingest.parse_pdf import parse_pdf


def test_parse_pdf_without_doc_id_uses_content_hash_id(sample_pdf, workspace: Layout):
    manifest = parse_pdf(sample_pdf, workspace.ingest_root, "pypdf", layout=workspace)

    doc_id = manifest["doc_id"]
    expected = "doc-" + hashlib.sha256(sample_pdf.read_bytes()).hexdigest()[:12]
    assert doc_id == expected, f"默认 doc_id 应为内容哈希: {doc_id} != {expected}"
    assert doc_id.startswith("doc-") and len(doc_id) == 16, doc_id

    # 归一后的 id 必须贯穿产物路径与 manifest（不是只在返回值里改）
    assert manifest["doc_id"] == doc_id
    assert workspace.manifest(doc_id).exists(), "默认 id 的 manifest 未落盘"
    pages = sorted(workspace.ingest_pages(doc_id).glob("*.json"))
    assert len(pages) == len(manifest["pages"]) >= 1
    assert manifest["chapters"], "样本 PDF 应识别出章节"
