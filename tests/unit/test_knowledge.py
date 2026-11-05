from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from src.core.errors import InputError
from src.core.jsonl import rewrite_json, rewrite_jsonl
from src.core import knowledge


@pytest.fixture()
def book(workspace):
    doc = 'book-test'
    rewrite_json(workspace.manifest(doc), {
        'doc_id': doc, 'title': '测试 <书> & 笔记', 'data_revision': 1,
        'chapters': [
            {'chapter_id': 'ch01', 'title': '已有章节', 'page_start': 1, 'page_end': 3},
            {'chapter_id': 'ch02', 'title': '未整理章节', 'page_start': 4, 'page_end': 8}],
        'pages': [{'page': 6, 'parse_status': 'failed'}],
    })
    rewrite_jsonl(workspace.anchors(doc, 'ch01'), [
        {'anchor_id': 'anchor-a', 'page': 2, 'content': '原始摘录'}])
    rewrite_jsonl(workspace.blocks(doc, 'ch01'), [
        {'id': 'b_000000000001', 'title': '定义 <甲>', 'content': '正文 & <script>文字</script>',
         'order': 2, 'type': 'definition', 'sources': [{'anchor_id': 'anchor-a', 'quote': '原始摘录'}]},
        {'id': 'b_000000000002', 'title': '已删除', 'content': '不能导出', 'deleted_at': '2026-01-01'},
    ])
    knowledge.clear_knowledge_cache()
    yield workspace, doc
    knowledge.clear_knowledge_cache()


def test_tree_keeps_missing_chapters_sources_and_deleted_notes_out(book):
    layout, doc = book
    tree = knowledge.knowledge_tree(layout, doc)
    assert tree['coverage']['chapters_total'] == 2
    assert tree['coverage']['chapters_with_notes'] == 1
    assert tree['coverage']['missing_chapter_ids'] == ['ch02']
    assert tree['coverage']['notes_total'] == 1
    assert tree['coverage']['full_book_verified'] is False
    assert tree['children'][1]['status'] == 'source_incomplete'
    assert tree['children'][1]['failed_pages'] == [6]
    note = tree['children'][0]['children'][0]['children'][0]
    assert note['sources'][0]['page'] == 2


def test_cache_reuses_parsing_and_invalidates_direct_file_edits(book, monkeypatch):
    layout, doc = book
    original = knowledge._build_tree
    calls = []
    def spy(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(knowledge, '_build_tree', spy)
    first = knowledge.knowledge_tree(layout, doc)
    first['title'] = '外部误改缓存副本'
    assert knowledge.knowledge_tree(layout, doc)['title'] != first['title']
    assert len(calls) == 1
    rewrite_jsonl(layout.anchors(doc, 'ch01'), [{'anchor_id': 'anchor-a', 'page': 3}])
    tree = knowledge.knowledge_tree(layout, doc)
    assert tree['children'][0]['children'][0]['children'][0]['sources'][0]['page'] == 3
    assert len(calls) == 2
    rewrite_jsonl(layout.blocks(doc, 'ch01'), [])
    assert knowledge.knowledge_tree(layout, doc)['coverage']['notes_total'] == 0
    assert len(calls) == 3


def test_oversized_result_is_not_retained(book, monkeypatch):
    monkeypatch.setattr(knowledge, 'CACHE_MAX_BYTES', 8)
    tree = knowledge.knowledge_tree(*book)
    assert tree['coverage']['notes_total'] == 1
    assert knowledge._CACHE_BYTES == 0
    assert not knowledge._CACHE


def test_cache_evicts_old_documents_at_count_limit(book, monkeypatch):
    layout, doc = book
    monkeypatch.setattr(knowledge, 'CACHE_MAX_DOCS', 1)
    knowledge.knowledge_tree(layout, doc)
    rewrite_json(layout.manifest('other-book'), {'title': '另一本书', 'chapters': []})
    knowledge.knowledge_tree(layout, 'other-book')
    assert len(knowledge._CACHE) == 1
    assert next(iter(knowledge._CACHE))[1] == 'other-book'
    assert knowledge._CACHE_BYTES <= knowledge.CACHE_MAX_BYTES


def test_xml_and_markdown_exchange_preserve_text_and_coverage(book):
    tree = knowledge.knowledge_tree(*book)
    for format, tag in [('freemind', 'map'), ('opml', 'opml')]:
        out = knowledge.export_knowledge(tree, format)
        root = ET.fromstring(out['content'])
        assert root.tag == tag
        assert root.find('.//script') is None
        assert '尚无笔记' in out['content']
        assert '原始摘录' in out['content']
        assert '不能导出' not in out['content']
        assert '/' not in out['filename']
    markdown = knowledge.export_knowledge(tree, 'markdown')['content']
    assert '## 未整理章节' in markdown
    assert '原文解析失败页：6' in markdown
    assert '来源：第 2 页' in markdown
    with pytest.raises(InputError):
        knowledge.export_knowledge(tree, 'html')


def test_clippings_are_durable_deduplicated_and_independent(book):
    layout, doc = book
    before = layout.manifest(doc).read_bytes()
    fields = {'title': '读书摘录', 'content': '来自其他软件的正文', 'source': '微信读书',
              'source_url': 'https://example.org/book'}
    first = knowledge.add_clipping(layout, **fields)
    second = knowledge.add_clipping(layout, **fields)
    assert first['id'] == second['id']
    assert not first['duplicate'] and second['duplicate']
    listing = knowledge.list_clippings(layout)
    assert listing['total'] == 1
    assert 'content' not in listing['items'][0]
    assert knowledge.get_clipping(layout, first['id'])['content'] == fields['content']
    assert layout.manifest(doc).read_bytes() == before
    with pytest.raises(InputError):
        knowledge.add_clipping(layout, **{**fields, 'source_url': 'javascript:alert(1)'})
    with pytest.raises(InputError):
        knowledge.get_clipping(layout, '../manifest')


def test_knowledge_api_contract_and_authorization(book):
    from fastapi.testclient import TestClient
    from src.api.main import create_app
    from src.api.routes.knowledge import router
    layout, doc = book
    app = create_app(layout.root)
    # Parent integrates the router independently; avoid duplicate registration.
    if not any(getattr(route, 'path', None) == '/api/v1/integrations' for route in app.routes):
        app.include_router(router)
    with TestClient(app, base_url='http://127.0.0.1:8787') as client:
        assert client.get(f'/api/v1/docs/{doc}/knowledge').status_code == 403
        client.headers.update({'X-Local-Token': app.state.app_state.token_value})
        assert client.get(f'/api/v1/docs/{doc}/knowledge').json()['coverage']['notes_total'] == 1
        out = client.get(f'/api/v1/docs/{doc}/knowledge/export?format=opml')
        assert out.status_code == 200 and out.json()['filename'].endswith('.opml')
        assert client.get('/api/v1/integrations').json()['mode'] == 'file_and_paste'
        result = client.post('/api/v1/integrations/clippings', json={'title': 'test', 'content': 'excerpt'})
        assert result.status_code == 200
        clipping_id = result.json()['id']
        assert client.get('/api/v1/integrations/clippings').json()['total'] == 1
        assert client.get('/api/v1/integrations/clippings/' + clipping_id).json()['content'] == 'excerpt'
