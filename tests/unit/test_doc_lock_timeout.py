"""per-doc 锁超时回归：doc_lock 的 timeout 必须真正传给 FileLock。

filelock 默认 timeout=-1（无限等待）：持锁方异常卡住时，所有等待提交/追加的请求
会一起挂死。这里用「先占锁 → 短暂超时的 doc_lock 必须抛 Timeout」锁死该行为。
"""

from __future__ import annotations

import pytest
from filelock import FileLock, Timeout

from src.core.transactions import doc_lock


def test_doc_lock_honours_timeout_when_held(workspace):
    held = FileLock(str(workspace.lock_file("doc-timeout")))
    held.acquire()
    try:
        with pytest.raises(Timeout):
            with doc_lock(workspace, "doc-timeout", timeout=0.05):
                pass
    finally:
        held.release()


def test_doc_lock_acquires_when_free(workspace):
    with doc_lock(workspace, "doc-free", timeout=1.0):
        pass
