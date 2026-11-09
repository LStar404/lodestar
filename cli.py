#!/usr/bin/env python3
"""Lodestar 星引 管线 CLI（9.1）。退出码：0 成功；1 参数错误；2 运行错误。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.core.errors import InputError, NoteGenError  # noqa: E402
from src.core.jsonl import sha256_file  # noqa: E402
from src.core.llm import LLMClient  # noqa: E402
from src.core.model_registry import ModelRegistry  # noqa: E402
from src.core.paths import Layout  # noqa: E402
from src.core.store import DocStore  # noqa: E402
from src.core.text import utc_now_iso  # noqa: E402


def _layout() -> Layout:
    return Layout(ROOT).ensure_base()


def _client(layout: Layout, config: dict):
    from src.db.runtime import RuntimeDB

    return LLMClient(ModelRegistry(layout), RuntimeDB(layout.runtime_db), config)


def cmd_ingest(args) -> int:
    from src.ingest.parse_pdf import parse_pdf_with_fallback

    layout = _layout()
    config = _load_config()
    if args.dry_run:
        print(f"[dry-run] ingest {args.pdf_path} doc_id={args.doc_id} parser={args.parser}")
        return 0
    manifest = parse_pdf_with_fallback(args.pdf_path, layout.ingest_root, args.parser,
                                       layout=layout, doc_id=args.doc_id, profile=args.profile,
                                       title=args.title, ocr=args.ocr,
                                       chapters_override=args.chapters_override, config=config)
    print(json.dumps({"doc_id": manifest["doc_id"], "chapters": manifest["chapters"],
                      "pages": len(manifest["pages"])}, ensure_ascii=False))
    return 0


def cmd_anchor(args) -> int:
    from src.anchor.chunker import chunk_and_anchor

    layout = _layout()
    if args.dry_run:
        print(f"[dry-run] anchor {args.doc_id}")
        return 0
    store = DocStore(layout, args.doc_id)
    counts = {}
    for ch in store.chapter_ids():
        counts[ch] = chunk_and_anchor(args.doc_id, ch, layout=layout, force=args.force)
    print(json.dumps({"doc_id": args.doc_id, "anchors": counts}, ensure_ascii=False))
    return 0


def cmd_generate(args) -> int:
    from src.generate.engine import generate_chapter

    layout = _layout()
    config = _load_config()
    if args.concurrency:
        config.setdefault("generate", {})["concurrency"] = args.concurrency
    if getattr(args, "language", None):
        config.setdefault("generate", {})["output_language"] = args.language
    if args.dry_run:
        print(f"[dry-run] generate {args.doc_id} merge={not args.replace}")
        return 0
    store = DocStore(layout, args.doc_id)
    chapters = [args.chapter] if args.chapter else store.chapter_ids()
    client = _client(layout, config)
    registry = ModelRegistry(layout)
    failed = []
    for ch in chapters:
        try:
            report = generate_chapter(args.doc_id, ch, layout=layout, client=client, config=config,
                                      registry=registry, model=args.model, force=args.force,
                                      use_cache=not args.no_cache,
                                      merge_mode="replace" if args.replace else "merge")
            print(json.dumps({k: report.get(k) for k in
                              ("chapter_id", "status", "created", "updated", "skipped_protected",
                               "failures", "cache_hit")}, ensure_ascii=False))
            if report.get("status") != "complete":
                failed.append(ch)
        except Exception as exc:
            failed.append(ch)
            print(f"[generate] {ch} 失败: {exc}", file=sys.stderr)
    if failed:
        print(json.dumps({"failed_chapters": failed}, ensure_ascii=False))
        return 2
    return 0


def cmd_link(args) -> int:
    from src.link.backlink import link_doc

    layout = _layout()
    if args.dry_run:
        print(f"[dry-run] link {args.doc_id} chapter={args.chapter}")
        return 0
    report = link_doc(args.doc_id, chapter_id=args.chapter, layout=layout)
    print(json.dumps(report, ensure_ascii=False)[:2000])
    return 0


def cmd_export(args) -> int:
    layout = _layout()
    if args.dry_run:
        print(f"[dry-run] export {args.doc_id} format={args.format}")
        return 0
    from src.export.anki_export import export_anki
    from src.export.md_export import export_md_book, export_md_chapter, export_md_obsidian
    from src.export.tex_export import export_tex_book, export_tex_chapter

    store = DocStore(layout, args.doc_id)
    cfg = _load_config()
    lang = getattr(args, "language", None) or cfg.get("generate", {}).get("output_language", "zh")
    encoding = getattr(args, "encoding", None) or cfg.get("export", {}).get("encoding", "utf-8")
    out_dir = Path(args.out) if args.out else layout.notes_doc_dir(args.doc_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    chapters = [args.chapter] if args.chapter else [c["chapter_id"] for c in store.manifest()["chapters"]]
    paths = []
    for ch in chapters:
        if args.format in ("md", "both"):
            paths.append(export_md_chapter(store, ch, out_dir=out_dir, lang=lang, encoding=encoding))
        if args.format in ("tex", "both"):
            paths.append(export_tex_chapter(store, ch, out_dir=out_dir, lang=lang, encoding=encoding))
        if args.format == "md-obsidian":
            paths.append(export_md_obsidian(store, ch, out_dir=out_dir.parent, lang=lang, encoding=encoding))
        if args.format == "anki":
            paths.append(export_anki(store, ch, out_dir=out_dir, lang=lang, encoding=encoding))
    if not args.chapter:
        if args.format == "anki":
            paths.append(export_anki(store, None, out_dir=out_dir, lang=lang, encoding=encoding))
        if args.format in ("md", "both"):
            paths.append(export_md_book(store, out_dir=out_dir, lang=lang, encoding=encoding))
        if args.format in ("tex", "both"):
            paths.append(export_tex_book(store, out_dir=out_dir, lang=lang, encoding=encoding))
    for p in paths:
        print(p)
    return 0


def cmd_run(args) -> int:
    """一键串联：ingest → anchor → generate → link → reindex → export。"""
    layout = _layout()
    config = _load_config()
    if args.dry_run:
        print(f"[dry-run] run {args.pdf_path}")
        return 0
    from src.anchor.chunker import chunk_and_anchor
    from src.db.reindex import reindex_doc
    from src.generate.engine import generate_chapter
    from src.ingest.parse_pdf import parse_pdf_with_fallback
    from src.link.backlink import link_doc

    manifest = parse_pdf_with_fallback(args.pdf_path, layout.ingest_root, args.parser,
                                       layout=layout, doc_id=args.doc_id,
                                       profile=args.profile, title=args.title,
                                       ocr=args.ocr, config=config)
    doc_id = manifest["doc_id"]
    store = DocStore(layout, doc_id)
    for ch in store.chapter_ids():
        chunk_and_anchor(doc_id, ch, layout=layout, force=args.force)

    client = _client(layout, config)
    registry = ModelRegistry(layout)
    failed = []
    for ch in ([args.chapter] if args.chapter else store.chapter_ids()):
        try:
            report = generate_chapter(doc_id, ch, layout=layout, client=client, config=config,
                                      registry=registry, model=args.model, force=args.force,
                                      use_cache=not args.no_cache,
                                      merge_mode="replace" if args.replace else "merge")
            if report.get("status") != "complete":
                failed.append(ch)
        except Exception as exc:
            failed.append(ch)
            print(f"[run] generate {ch} 失败: {exc}", file=sys.stderr)

    link_doc(doc_id, chapter_id=args.chapter, layout=layout)
    reindex_doc(layout, doc_id)

    from src.export.anki_export import export_anki
    from src.export.md_export import export_md_book, export_md_chapter, export_md_obsidian
    from src.export.tex_export import export_tex_chapter

    out_dir = layout.notes_doc_dir(doc_id)
    lang = getattr(args, "language", None) or config.get("generate", {}).get("output_language", "zh")
    encoding = getattr(args, "encoding", None) or config.get("export", {}).get("encoding", "utf-8")
    for ch in ([args.chapter] if args.chapter else store.chapter_ids()):
        if args.format in ("md", "both"):
            export_md_chapter(store, ch, out_dir=out_dir, lang=lang, encoding=encoding)
        if args.format in ("tex", "both"):
            export_tex_chapter(store, ch, out_dir=out_dir, lang=lang, encoding=encoding)
        if args.format == "md-obsidian":
            export_md_obsidian(store, ch, out_dir=out_dir.parent, lang=lang, encoding=encoding)
        if args.format == "anki":
            export_anki(store, ch, out_dir=out_dir, lang=lang, encoding=encoding)
    if not args.chapter:
        if args.format in ("md", "both"):
            export_md_book(store, out_dir=out_dir, lang=lang, encoding=encoding)
        if args.format == "anki":
            export_anki(store, None, out_dir=out_dir, lang=lang, encoding=encoding)
    print(json.dumps({"doc_id": doc_id, "failed_chapters": failed,
                      "notes_out": str(out_dir)}, ensure_ascii=False))
    return 2 if failed else 0


def cmd_reindex(args) -> int:
    from src.db.reindex import reindex_doc

    layout = _layout()
    if args.dry_run:
        print(f"[dry-run] reindex {args.doc_id} embed={args.embed}")
        return 0
    if args.embed:
        n = embed_anchors(layout, args.doc_id)
        print(json.dumps({"embedded": n}, ensure_ascii=False))
    print(json.dumps(reindex_doc(layout, args.doc_id), ensure_ascii=False))
    return 0


def embed_anchors(layout: Layout, doc_id: str) -> int:
    """为缺失 embedding 的锚点计算段落级向量（5.2 第三层）。"""
    from src.anchor.embed import embedder_from_config
    from src.core.config import load_config
    from src.core.store import DocMutation, DocStore

    embedder = embedder_from_config(load_config(ROOT))
    if not embedder.enabled:
        raise InputError("未启用 embedding：请设置 config.yaml embedding.provider=onnx_bge 或 hashed_lexical")
    store = DocStore(layout, doc_id)
    updates: dict = {}
    count = 0
    for ch in store.chapter_ids():
        rows = [dict(r) for r in store.anchor_rows(ch)]
        missing = [r for r in rows if not r.get("embedding")]
        if not missing:
            continue
        vecs = embedder.encode([r["content"] for r in missing])
        for row, vec in zip(missing, vecs):
            if vec:
                row["embedding"] = vec
                count += 1
        updates[ch] = rows
    if updates:
        store.commit(DocMutation(anchors=updates), sync_backlinks=True)
    return count


def cmd_report(args) -> int:
    layout = _layout()
    report_dir = layout.reports_doc_dir(args.doc_id)
    out = {"doc_id": args.doc_id, "reports": []}
    if report_dir.exists():
        for p in sorted(report_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            out["reports"].append({"file": p.name, "summary": {
                k: data.get(k) for k in ("status", "created", "updated", "skipped_protected",
                                         "suppressed_deleted", "ambiguous", "orphan_kept",
                                         "dead_links", "failures") if k in data}})
    store = DocStore(layout, args.doc_id)
    out["dead_links"] = []
    blocks = store.all_block_rows()
    ids = {b["id"] for b in blocks}
    for b in blocks:
        for ln in b.get("links", []):
            if ln.startswith("b_") and ln not in ids:
                out["dead_links"].append({"block_id": b["id"], "link": ln})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_eval(args) -> int:
    """跑生成质量评测集（10.5）：三维度评分 + 基线回归告警。"""
    from src.eval.runner import run_eval

    layout = _layout()
    cases_path = Path(args.cases) if args.cases else ROOT / "tests" / "eval" / "cases.jsonl"
    labels_path = ROOT / "tests" / "eval" / "labels.json"
    baseline_path = ROOT / "reports" / "eval_baseline.json"
    result = run_eval(layout, args.doc_id, cases_path=cases_path, labels_path=labels_path,
                      baseline_path=baseline_path, update_baseline=args.update_baseline)
    out = layout.reports_doc_dir(args.doc_id) / "eval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {k: result[k] for k in
               ("doc_id", "cases", "human_labelled", "dimensions", "baseline",
                "regressions", "baseline_written") if k in result}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if result.get("regressions"):
        print("回归告警：以下维度低于基线 5 个百分点（10.5）", file=sys.stderr)
        return 2
    return 0


def cmd_backup(args) -> int:
    layout = _layout()
    if args.dry_run:
        print(f"[dry-run] backup {args.doc_id}")
        return 0
    store = DocStore(layout, args.doc_id)
    store.recover()
    out_dir = Path(args.out) if args.out else layout.root / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.doc_id}-{utc_now_iso().replace(':', '')}.zip"
    doc_dir = layout.doc_dir(args.doc_id)
    manifest = store.manifest()
    pdf_path = layout.root / manifest["pdf_path"]
    checksums = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        if pdf_path.exists():
            zf.write(pdf_path, f"pdf/{pdf_path.name}")
            checksums[f"pdf/{pdf_path.name}"] = sha256_file(pdf_path)
        for p in sorted(doc_dir.rglob("*")):
            if p.is_file() and p.name != "index.db" and "transactions" not in p.parts:
                rel = f"data/{p.relative_to(doc_dir).as_posix()}"
                zf.write(p, rel)
                checksums[rel] = sha256_file(p)
        zf.writestr("checksums.json", json.dumps(checksums, ensure_ascii=False, indent=2))
        zf.writestr("backup.json", json.dumps({
            "doc_id": args.doc_id, "created_at": utc_now_iso(),
            "schema_version": manifest.get("schema_version"),
            "data_revision": manifest.get("data_revision")}, ensure_ascii=False, indent=2))
    print(out)
    return 0


def cmd_restore(args) -> int:
    layout = _layout()
    if args.dry_run:
        print(f"[dry-run] restore {args.backup}")
        return 0
    src = Path(args.backup)
    if not src.exists():
        raise InputError(f"备份不存在: {src}")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                name = info.filename
                if name.startswith("/") or ".." in Path(name).parts:
                    raise InputError(f"备份包含非法路径（Zip Slip）: {name}")
            zf.extractall(td_path)
        checksums = json.loads((td_path / "checksums.json").read_text(encoding="utf-8"))
        for rel, want in checksums.items():
            got = sha256_file(td_path / rel)
            if got != want:
                raise InputError(f"校验和不匹配: {rel}")
        meta = json.loads((td_path / "backup.json").read_text(encoding="utf-8"))
        doc_id = meta["doc_id"]
        target = layout.doc_dir(doc_id)
        if target.exists():
            if not args.as_new_doc:
                raise InputError(f"文档已存在: {doc_id}（默认拒绝覆盖；请先备份或使用 --as-new-doc）")
            backup_dir = target.with_suffix(".bak-" + utc_now_iso().replace(":", ""))
            shutil.move(str(target), str(backup_dir))
        target.mkdir(parents=True, exist_ok=True)
        for p in sorted((td_path / "data").rglob("*")):
            if p.is_file():
                rel = p.relative_to(td_path / "data")
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
        pdf_src = td_path / "pdf"
        if pdf_src.exists():
            for p in pdf_src.iterdir():
                shutil.copy2(p, layout.textbooks_dir / p.name)
    from src.db.reindex import reindex_doc

    print(json.dumps({"restored": doc_id, "reindex": reindex_doc(layout, doc_id)}, ensure_ascii=False))
    return 0


def cmd_delete(args) -> int:
    """删除教材（移入回收站）。破坏性操作必须 --yes。"""
    layout = _layout()
    if args.dry_run:
        print(f"[dry-run] delete {args.doc_id}")
        return 0
    if not args.yes:
        print("错误：删除教材需显式确认（加 --yes）", file=sys.stderr)
        return 1
    store = DocStore(layout, args.doc_id)
    summary = {"blocks": len(store.all_block_rows(include_deleted=True)),
               "anchors": len(store.all_anchor_rows())}
    result = store.trash_document()
    result["removed"] = summary
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args) -> int:
    from src.api.main import run_server

    run_server(host=args.host, port=args.port)
    return 0


def _load_config() -> dict:
    from src.core.config import load_config

    return load_config(ROOT)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli.py", description="Lodestar 星引 管线 CLI")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--dry-run", action="store_true", help="打印将执行的动作不落盘")

    sp = sub.add_parser("ingest"); common(sp)
    sp.add_argument("pdf_path"); sp.add_argument("--doc-id"); sp.add_argument("--title")
    sp.add_argument("--profile", default="textbook", choices=["textbook", "paper"])
    sp.add_argument("--parser", default="auto", choices=["auto", "marker", "docling", "pypdf"])
    sp.add_argument("--ocr", action="store_true"); sp.add_argument("--force", action="store_true")
    sp.add_argument("--chapters-override", type=json.loads, default=None)
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("anchor"); common(sp)
    sp.add_argument("doc_id"); sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_anchor)

    sp = sub.add_parser("generate"); common(sp)
    sp.add_argument("doc_id"); sp.add_argument("--chapter"); sp.add_argument("--model")
    sp.add_argument("--language", choices=["zh", "en", "ja"], help="生成笔记语言")
    sp.add_argument("--concurrency", type=int, default=0); sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-cache", action="store_true")
    sp.add_argument("--merge", action="store_true", default=True)
    sp.add_argument("--replace", action="store_true")
    sp.add_argument("--yes", action="store_true", help="--replace 必须显式确认")
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("link"); common(sp)
    sp.add_argument("doc_id"); sp.add_argument("--chapter")
    sp.set_defaults(func=cmd_link)

    sp = sub.add_parser("export"); common(sp)
    sp.add_argument("doc_id"); sp.add_argument("--chapter")
    sp.add_argument("--format", default="md", choices=["md", "tex", "both", "md-obsidian", "anki"])
    sp.add_argument("--out")
    sp.add_argument("--encoding", choices=["utf-8", "utf-8-sig", "gbk", "big5"],
                    help="导出文件编码（默认取 config.yaml export.encoding）")
    sp.add_argument("--language", choices=["zh", "en", "ja"],
                    help="导出标签语言（默认取 config.yaml generate.output_language）")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("run"); common(sp)
    sp.add_argument("pdf_path"); sp.add_argument("--doc-id"); sp.add_argument("--title")
    sp.add_argument("--profile", default="textbook", choices=["textbook", "paper"])
    sp.add_argument("--parser", default="auto", choices=["auto", "marker", "docling", "pypdf"])
    sp.add_argument("--chapter"); sp.add_argument("--model")
    sp.add_argument("--format", default="md", choices=["md", "tex", "both", "md-obsidian", "anki"])
    sp.add_argument("--encoding", choices=["utf-8", "utf-8-sig", "gbk", "big5"],
                    help="导出文件编码")
    sp.add_argument("--language", choices=["zh", "en", "ja"], help="生成与导出语言")
    sp.add_argument("--ocr", action="store_true"); sp.add_argument("--force", action="store_true")
    sp.add_argument("--concurrency", type=int, default=0); sp.add_argument("--no-cache", action="store_true")
    sp.add_argument("--merge", action="store_true", default=True)
    sp.add_argument("--replace", action="store_true"); sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("reindex"); common(sp); sp.add_argument("doc_id")
    sp.add_argument("--embed", action="store_true", help="先为缺失向量的锚点计算 embedding")
    sp.set_defaults(func=cmd_reindex)

    sp = sub.add_parser("report"); common(sp); sp.add_argument("doc_id")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("eval"); common(sp); sp.add_argument("doc_id")
    sp.add_argument("--cases", help="评测集路径（默认 tests/eval/cases.jsonl）")
    sp.add_argument("--update-baseline", action="store_true", help="固化/更新基线 reports/eval_baseline.json")
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("backup"); common(sp); sp.add_argument("doc_id"); sp.add_argument("--out")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore"); common(sp); sp.add_argument("backup")
    sp.add_argument("--as-new-doc", action="store_true")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("delete"); common(sp)
    sp.add_argument("doc_id")
    sp.add_argument("--yes", action="store_true", help="确认删除（移入回收站 data/.trash）")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("serve"); common(sp)
    sp.add_argument("--host"); sp.add_argument("--port", type=int)
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "replace", False) and not getattr(args, "yes", False):
        print("错误：--replace 需要同时指定 --yes（并会自动备份）", file=sys.stderr)
        return 1
    try:
        return args.func(args)
    except InputError as exc:
        print(f"参数错误: {exc.message}", file=sys.stderr)
        return 1
    except NoteGenError as exc:
        print(f"运行错误: {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except Exception as exc:  # CLI 顶层兜底，仍打印可诊断信息
        print(f"运行错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
