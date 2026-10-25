"""4.2.2 可恢复的文档提交协议。

单文件 rename/fsync 不能保证 blocks/history/anchors/manifest 同时更新，因此所有主数据变更
都经本模块的 intent → COMMIT → apply → DONE → index 同步 四步流程；崩溃后 recover_doc 依据
意图中的旧/新校验和判断重做。写序固定"先 JSONL 落盘、后 SQLite 同步"。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from filelock import FileLock

from .errors import DataCorrupt, RevisionConflict
from .jsonl import (
    atomic_write_bytes,
    canonical_json,
    dump_line,
    sha256_bytes,
    sha256_file,
)
from .paths import Layout
from .text import new_tx_id, utc_now_iso

LOCK_TIMEOUT = 60.0


@dataclass
class FileChange:
    """一个目标文件的变更；rel_path 为 doc 目录内相对 POSIX 路径。"""

    rel_path: str
    op: str                      # replace | append
    data: bytes                  # replace: 整文件新内容；append: 待追加行
    rev_id: str | None = None    # history 追加的去重键
    old_sha256: str | None = None
    new_sha256: str = ""

    def to_target(self, payload_name: str) -> dict:
        return {
            "rel_path": self.rel_path,
            "op": self.op,
            "old_sha256": self.old_sha256,
            "new_sha256": self.new_sha256,
            "payload": payload_name,
            "rev_id": self.rev_id,
        }


@dataclass
class CommitResult:
    tx_id: str
    base_revision: int
    new_revision: int
    files: list[str] = field(default_factory=list)


def replace_change(layout: Layout, doc_id: str, rel_path: str, content: bytes | str | list | dict) -> FileChange:
    if isinstance(content, bytes):
        data = content
    elif isinstance(content, str):
        data = content.encode("utf-8")
    elif isinstance(content, list):
        data = ("".join(dump_line(x) + "\n" for x in content)).encode("utf-8")
    else:
        data = (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return FileChange(rel_path=rel_path, op="replace", data=data)


def append_change(layout: Layout, doc_id: str, rel_path: str, rows: Iterable[dict]) -> FileChange:
    data = ("".join(dump_line(x) + "\n" for x in rows)).encode("utf-8")
    return FileChange(rel_path=rel_path, op="append", data=data)


def doc_lock(layout: Layout, doc_id: str, timeout: float = LOCK_TIMEOUT) -> FileLock:
    """per-doc 文件锁；timeout 必须真正传给 FileLock——默认 timeout=-1 是无限等待，
    持锁方卡死（如后台追加/恢复异常）会把所有提交与请求一起挂住。"""
    layout.locks_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(layout.lock_file(doc_id)), timeout=timeout)


def _target_path(layout: Layout, doc_id: str, rel_path: str) -> Path:
    return layout.safe_join(layout.doc_dir(doc_id), *rel_path.split("/"))


def _resulting_bytes(path: Path, change: FileChange) -> bytes:
    if change.op == "replace":
        return change.data
    if change.op == "append":
        if path.exists():
            return path.read_bytes() + change.data
        return change.data
    raise DataCorrupt(f"未知操作类型: {change.op}")


def _plan_changes(layout: Layout, doc_id: str, changes: list[FileChange]) -> list[FileChange]:
    planned: list[FileChange] = []
    seen: dict[str, int] = {}
    for ch in changes:
        path = _target_path(layout, doc_id, ch.rel_path)
        ch.old_sha256 = sha256_file(path)
        if ch.op == "append" and ch.rev_id and path.exists():
            # 按 rev_id 去重补写历史（幂等前滚）
            existing = path.read_bytes()
            if (ch.rev_id).encode("utf-8") in existing:
                continue
        ch.new_sha256 = sha256_bytes(_resulting_bytes(path, ch))
        if ch.rel_path in seen:
            idx = seen[ch.rel_path]
            prev = planned[idx]
            if prev.op == "replace" and ch.op == "replace":
                # 同文件多次替换：后写者为准（其候选状态更完整，如 backlink 版）
                prev.data = ch.data
                prev.new_sha256 = ch.new_sha256
                continue
            if prev.op == "append" and ch.op == "append":
                # 同文件多次追加必须合并为一次，否则第二次 old_sha 校验会误报 DATA_CORRUPT
                prev.data = prev.data + ch.data
                prev.rev_id = prev.rev_id or ch.rev_id
                prev.new_sha256 = sha256_bytes(_resulting_bytes(path, prev))
                continue
            raise DataCorrupt(
                f"{ch.rel_path}: 同一文件出现冲突的变更类型（{prev.op} / {ch.op}）")
        seen[ch.rel_path] = len(planned)
        planned.append(ch)
    return planned


def _apply_target(path: Path, change: FileChange) -> None:
    current = sha256_file(path)
    if current == change.new_sha256:
        return  # 幂等：已前滚
    if change.old_sha256 is not None and current != change.old_sha256:
        raise DataCorrupt(
            f"{path}: 校验和既不等于旧值也不等于新值，停止并保留文件供恢复",
            detail={"expected_old": change.old_sha256, "current": current},
        )
    if change.op == "replace":
        atomic_write_bytes(path, change.data)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as fh:
            fh.write(change.data)
            fh.flush()
            os.fsync(fh.fileno())


def _write_tx_dir(layout: Layout, doc_id: str, intent: dict, changes: list[FileChange]) -> Path:
    tx_dir = layout.transactions_dir(doc_id) / intent["tx_id"]
    tx_dir.mkdir(parents=True, exist_ok=True)
    for i, ch in enumerate(changes):
        payload = f"payload_{i}.bin"
        atomic_write_bytes(tx_dir / payload, ch.data)
        ch.to_target(payload)
    intent["targets"] = [ch.to_target(f"payload_{i}.bin") for i, ch in enumerate(changes)]
    atomic_write_bytes(tx_dir / "intent.json", (canonical_json(intent)).encode("utf-8"))
    return tx_dir


def _do_commit_marker(tx_dir: Path, intent: dict) -> None:
    atomic_write_bytes(tx_dir / "COMMIT", (canonical_json({"tx_id": intent["tx_id"]})).encode("utf-8"))


def _apply_intent(layout: Layout, doc_id: str, intent: dict, changes_by_rel: dict[str, FileChange] | None) -> list[str]:
    """按意图前滚：替换目标文件 → 补写历史 → 最后写 manifest.data_revision。"""
    done: list[str] = []
    for t in intent.get("targets", []):
        path = _target_path(layout, doc_id, t["rel_path"])
        if changes_by_rel and t["rel_path"] in changes_by_rel:
            change = changes_by_rel[t["rel_path"]]
        else:
            payload_path = layout.transactions_dir(doc_id) / intent["tx_id"] / t["payload"]
            data = payload_path.read_bytes() if payload_path.exists() else b""
            change = FileChange(rel_path=t["rel_path"], op=t["op"], data=data,
                                rev_id=t.get("rev_id"), old_sha256=t.get("old_sha256"),
                                new_sha256=t.get("new_sha256"))
        _apply_target(path, change)
        done.append(t["rel_path"])
    return done


def recover_doc(layout: Layout, doc_id: str) -> dict:
    """崩溃恢复（4.2.2 第 3/4 条）；调用方必须已持有该 doc 锁。"""
    tx_root = layout.transactions_dir(doc_id)
    status = {"recovered": [], "discarded": [], "corrupt": []}
    if not tx_root.exists():
        return status
    for tx_dir in sorted([p for p in tx_root.iterdir() if p.is_dir()]):
        intent_path = tx_dir / "intent.json"
        if not intent_path.exists():
            shutil.rmtree(tx_dir, ignore_errors=True)
            status["discarded"].append(tx_dir.name)
            continue
        try:
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
        except Exception:
            status["corrupt"].append(tx_dir.name)
            continue
        if (tx_dir / "DONE").exists():
            _finish_index_and_cleanup(layout, doc_id, tx_dir)
            continue
        if not (tx_dir / "COMMIT").exists():
            # 尚未决定生效：丢弃暂存文件
            shutil.rmtree(tx_dir, ignore_errors=True)
            status["discarded"].append(tx_dir.name)
            continue
        try:
            _apply_intent(layout, doc_id, intent, None)
        except DataCorrupt as exc:
            status["corrupt"].append({"tx": tx_dir.name, "error": exc.message})
            continue
        atomic_write_bytes(tx_dir / "DONE", (canonical_json({"tx_id": intent["tx_id"]})).encode("utf-8"))
        _finish_index_and_cleanup(layout, doc_id, tx_dir,
                                  changed=[t.get("rel_path", "") for t in intent.get("targets", [])])
        status["recovered"].append(tx_dir.name)
    return status


def _finish_index_and_cleanup(layout: Layout, doc_id: str, tx_dir: Path,
                              changed: list[str] | None = None) -> None:
    """DONE 后同步 SQLite；同步成功才清理日志（4.2.2 第 3 条）。

    只改 blocks/history 的提交走 reindex 快路径（不重建 anchors/FTS），
    大文档上的块编辑从 O(全库) 降到 O(受影响章)。
    """
    try:
        from src.db.reindex import reindex_doc

        blocks_only = bool(changed) and all(
            (not c.startswith("anchors/")) for c in changed)
        # 只重刷变更到的章（块编辑只影响本章）：从 blocks/chXX.jsonl 解析章 id
        chapters = sorted({c.split("/")[1][:-len(".jsonl")] for c in (changed or [])
                           if c.startswith("blocks/") and c.endswith(".jsonl")})
        reindex_doc(layout, doc_id, blocks_only=blocks_only,
                    chapters=chapters if blocks_only else None)
    except Exception:
        return  # 索引滞后属正常，保留日志待下次恢复
    shutil.rmtree(tx_dir, ignore_errors=True)


def commit_doc(
    layout: Layout,
    doc_id: str,
    base_revision: int,
    changes: list[FileChange],
    *,
    manifest_updates: dict | None = None,
    validate: Callable[[dict[str, bytes]], None] | None = None,
    now: str | None = None,
    timeout: float = LOCK_TIMEOUT,
) -> CommitResult:
    """可恢复提交入口：编辑、生成合并、链接、迁移都必须走这里（4.2.2 第 5 条）。"""
    now = now or utc_now_iso()
    lock = doc_lock(layout, doc_id, timeout)
    with lock:
        recover_doc(layout, doc_id)
        manifest_path = layout.manifest(doc_id)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if int(manifest.get("data_revision", 0)) != int(base_revision):
            raise RevisionConflict(
                "data_revision 已变化，请重新读取后再提交",
                detail={"expected": base_revision, "current": manifest.get("data_revision")},
            )
        planned = _plan_changes(layout, doc_id, list(changes))

        # manifest 变更最后提交：data_revision 单调 +1
        new_revision = int(base_revision) + 1
        new_manifest = dict(manifest)
        for k, v in (manifest_updates or {}).items():
            new_manifest[k] = v
        new_manifest["data_revision"] = new_revision
        new_manifest["updated_at"] = now
        manifest_change = replace_change(layout, doc_id, "manifest.json", new_manifest)
        manifest_change.old_sha256 = sha256_file(manifest_path)
        manifest_change.new_sha256 = sha256_bytes(manifest_change.data)
        planned.append(manifest_change)

        candidates = {}
        for ch in planned:
            path = _target_path(layout, doc_id, ch.rel_path)
            candidates[ch.rel_path] = _resulting_bytes(path, ch)
        if validate is not None:
            validate(candidates)

        intent = {
            "tx_id": new_tx_id(),
            "doc_id": doc_id,
            "base_revision": int(base_revision),
            "new_revision": new_revision,
            "created_at": now,
            "manifest_updates": manifest_updates or {},
        }
        tx_dir = _write_tx_dir(layout, doc_id, intent, planned)
        _do_commit_marker(tx_dir, intent)
        try:
            _apply_intent(layout, doc_id, intent, {c.rel_path: c for c in planned})
        except DataCorrupt:
            raise
        atomic_write_bytes(tx_dir / "DONE", (canonical_json({"tx_id": intent["tx_id"]})).encode("utf-8"))
        _finish_index_and_cleanup(layout, doc_id, tx_dir,
                                  changed=[c.rel_path for c in planned])
        return CommitResult(tx_id=intent["tx_id"], base_revision=int(base_revision),
                            new_revision=new_revision, files=[c.rel_path for c in planned])
