from types import SimpleNamespace

from src.api.routes.blocks import block_counts
from src.api.routes.docs import search
from src.core.store import DocStore


def make_note(store, title='Linear map', content='Matrix transform', **extra):
    return store.create_block({'type': 'motivation', 'title': title, 'content': content,
                               'basis': 'synthesis', 'order': 1, **extra},
                              author='ai', chapter_id='ch01')


def request_for(store):
    state = SimpleNamespace(layout=store.layout, config={})
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(app_state=state)))


def test_counts_catalog_is_shared_by_instances_and_avoids_repeated_book_copies(store, monkeypatch):
    make_note(store, status='needs_review')
    original = DocStore.all_block_rows
    scans = []

    def counted(self, *args, **kwargs):
        scans.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DocStore, 'all_block_rows', counted)
    first = block_counts(request_for(store), store.doc_id)
    first['by_status']['needs_review'] = 999
    for _ in range(5):
        other = DocStore(store.layout, store.doc_id)
        assert other.block_counts()['by_status']['needs_review'] == 1
        assert other.search_blocks('linear')[0]['title'] == 'Linear map'
        assert other.block_titles()[0]['title'] == 'Linear map'
    assert len(scans) == 1
    assert 'content' not in first and 'items' not in first


def test_catalog_invalidates_after_status_edit_delete_and_restore(store):
    block = make_note(store, status='needs_review')
    assert store.block_counts()['by_status']['needs_review'] == 1
    old_revision = store.data_revision()
    store.update_block(block['id'], {'status': 'manually_edited', 'title': 'Changed'},
                       expected_revision=block['revision'], author='human')
    counts = store.block_counts()
    assert counts['data_revision'] > old_revision
    assert counts['by_status'].get('needs_review', 0) == 0
    assert counts['by_status']['manually_edited'] == 1
    assert store.search_blocks('linear') == []
    assert store.search_blocks('changed')[0]['block_id'] == block['id']
    store.soft_delete_block(block['id'])
    assert store.block_counts()['total'] == 0
    assert store.search_blocks('changed') == []
    store.restore_block(block['id'])
    assert store.block_counts()['total'] == 1
    assert store.search_blocks('changed')[0]['block_id'] == block['id']


def test_search_normalizes_width_case_multiple_terms_and_tags(store):
    block = make_note(store, title='ＬＩＮＥＡＲ 变换', content='Matrix', tags=['重点'])
    assert store.search_blocks('linear 重点')[0]['block_id'] == block['id']
    assert store.search_blocks('MATRIX　变换')[0]['block_id'] == block['id']
    assert store.search_blocks('linear missing') == []
    assert store.search_blocks(' \t ') == []
    result = store.search_blocks('linear')
    result[0]['title'] = 'Poison'
    assert store.search_blocks('linear')[0]['title'] == 'ＬＩＮＥＡＲ 变换'


def test_search_returns_chapter_and_excludes_deleted_notes(store):
    kept = make_note(store, title='Unique location')
    removed = make_note(store, title='Unique removed')
    store.soft_delete_block(removed['id'])
    result = search(request_for(store), store.doc_id, 'Unique')
    assert len(result['blocks']) == 1
    assert result['blocks'][0]['block_id'] == kept['id']
    assert result['blocks'][0]['chapter_id'] == 'ch01'
    assert search(request_for(store), store.doc_id, '  ')['blocks'] == []
