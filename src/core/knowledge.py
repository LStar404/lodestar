"""Book-wide derived knowledge trees and explicit, portable file interchange.

The tree never calls an LLM and never changes original notes. Its bounded LRU
tracks the actual source files, including edits made outside DocStore.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from filelock import FileLock

from .block_types import REGISTRY
from .errors import InputError, NotFound
from .jsonl import load_json, load_jsonl, rewrite_json
from .paths import Layout, check_doc_id
from .store import sort_blocks
from .text import utc_now_iso
from .transactions import doc_lock

CACHE_MAX_BYTES = 16 * 1024 * 1024
CACHE_MAX_DOCS = 16
_CACHE: OrderedDict[tuple[str, str], tuple[tuple, bytes]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_BYTES = 0


def _signature(layout: Layout, doc_id: str) -> tuple:
    paths = [layout.manifest(doc_id)]
    for directory in (layout.blocks_dir(doc_id), layout.anchors_dir(doc_id)):
        paths.extend(sorted(directory.glob('*.jsonl')))
    signatures = []
    for path in paths:
        try:
            stat = path.stat()
            signatures.append((str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except FileNotFoundError:
            signatures.append((str(path), None))
    return (tuple(signatures), tuple(sorted((key, value.cn_name) for key, value in REGISTRY.items())))


def clear_knowledge_cache() -> None:
    global _CACHE_BYTES
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0


def knowledge_tree(layout: Layout, doc_id: str) -> dict:
    check_doc_id(doc_id)
    if not layout.manifest(doc_id).is_file():
        raise NotFound(f'文档不存在: {doc_id}')
    key = (str(layout.root), doc_id)
    signature = _signature(layout, doc_id)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == signature:
            _CACHE.move_to_end(key)
            return json.loads(cached[1])
    # Readers see one committed document; no derived files are written.
    with doc_lock(layout, doc_id):
        signature = _signature(layout, doc_id)
        result = _build_tree(layout, doc_id)
        stable = signature == _signature(layout, doc_id)
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    global _CACHE_BYTES
    with _CACHE_LOCK:
        previous = _CACHE.pop(key, None)
        if previous:
            _CACHE_BYTES -= len(previous[1])
        if stable and len(encoded) <= CACHE_MAX_BYTES:
            while _CACHE and (_CACHE_BYTES + len(encoded) > CACHE_MAX_BYTES or len(_CACHE) >= CACHE_MAX_DOCS):
                _, evicted = _CACHE.popitem(last=False)
                _CACHE_BYTES -= len(evicted[1])
            _CACHE[key] = (signature, encoded)
            _CACHE_BYTES += len(encoded)
    return result


def _build_tree(layout: Layout, doc_id: str) -> dict:
    manifest = load_json(layout.manifest(doc_id))
    if not manifest:
        raise NotFound(f'文档不存在: {doc_id}')
    chapters = []
    note_count = 0
    missing = []
    failed_pages = {p['page'] for p in manifest.get('pages', []) if p.get('parse_status') == 'failed'}
    for chapter in manifest.get('chapters', []):
        chapter_id = chapter['chapter_id']
        anchors = {row['anchor_id']: row for row in load_jsonl(layout.anchors(doc_id, chapter_id), strict=True)}
        blocks = sort_blocks(row for row in load_jsonl(layout.blocks(doc_id, chapter_id), strict=True) if not row.get('deleted_at'))
        groups: dict[str, dict] = {}
        for block in blocks:
            type_id = block.get('type', 'note')
            spec = REGISTRY.get(type_id)
            group = groups.setdefault(type_id, {
                'id': f'{chapter_id}:{type_id}', 'kind': 'type',
                'title': spec.cn_name if spec else type_id, 'children': [],
            })
            sources = []
            for ref in block.get('sources', []):
                anchor = anchors.get(ref.get('anchor_id'), {})
                sources.append({'anchor_id': ref.get('anchor_id'), 'page': anchor.get('page'),
                                'quote': ref.get('quote', ''), 'available': bool(anchor)})
            group['children'].append({
                'id': block['id'], 'kind': 'note', 'title': block.get('title') or '未命名笔记',
                'content': block.get('content', ''), 'chapter_id': chapter_id,
                'type': type_id, 'sources': sources, 'status': block.get('status'),
            })
        incomplete_pages = sorted(p for p in failed_pages
                                  if chapter.get('page_start', 0) <= p <= chapter.get('page_end', 0))
        if not blocks:
            missing.append(chapter_id)
        note_count += len(blocks)
        chapters.append({
            'id': chapter_id, 'kind': 'chapter', 'title': chapter.get('title') or chapter_id,
            'page_start': chapter.get('page_start'), 'page_end': chapter.get('page_end'),
            'note_count': len(blocks), 'source_anchor_count': len(anchors),
            'status': 'source_incomplete' if incomplete_pages else ('has_notes' if blocks else 'missing_notes'),
            'failed_pages': incomplete_pages, 'children': list(groups.values()),
        })
    return {
        'id': doc_id, 'kind': 'book', 'title': manifest.get('title') or doc_id,
        'data_revision': manifest.get('data_revision'), 'children': chapters,
        'coverage': {'chapters_total': len(chapters), 'chapters_with_notes': len(chapters) - len(missing),
                     'notes_total': note_count, 'missing_chapter_ids': missing,
                     'failed_source_pages': sorted(failed_pages), 'full_book_verified': False,
                     'scope': '以已导入文件的章节目录为准；有笔记不代表该章知识点已全部覆盖。'},
    }


def _xml_text(value: object) -> str:
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', str(value))


def _source_text(node: dict) -> str:
    return '\n'.join(f"来源：第 {source['page']} 页 · {source.get('quote', '')}" if source.get('page')
                     else f"来源待定位 · {source.get('quote', '')}" for source in node.get('sources', []))


def export_knowledge(tree: dict, format: str) -> dict:
    """Return a download payload; XML text is escaped by ElementTree."""
    title = tree['title']
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)[:100].strip('. ') or '读书笔记'
    if format == 'markdown':
        lines = ['# ' + ' '.join(title.splitlines()), '', '> ' + tree['coverage']['scope'], '']
        for chapter in tree['children']:
            lines += ['## ' + ' '.join(chapter['title'].splitlines()), '']
            if not chapter['note_count']:
                lines += ['> 本章尚无笔记。', '']
            if chapter['failed_pages']:
                lines += ['> 原文解析失败页：' + ', '.join(map(str, chapter['failed_pages'])), '']
            for group in chapter['children']:
                lines += ['### ' + ' '.join(group['title'].splitlines()), '']
                for note in group['children']:
                    lines += ['#### ' + ' '.join(note['title'].splitlines()), '', note['content'], '']
                    if note['sources']:
                        lines += [_source_text(note), '']
        return {'filename': stem + '.md', 'media_type': 'text/markdown;charset=utf-8', 'content': '\n'.join(lines)}
    if format not in ('freemind', 'opml'):
        raise InputError('导出格式仅支持 markdown、freemind、opml')
    if format == 'freemind':
        root = ET.Element('map', {'version': '1.0.1'})
        parent = root
    else:
        root = ET.Element('opml', {'version': '2.0'})
        head = ET.SubElement(root, 'head')
        ET.SubElement(head, 'title').text = _xml_text(title)
        parent = ET.SubElement(root, 'body')

    def append(parent_node: ET.Element, node: dict) -> None:
        label = node['title']
        if node.get('kind') == 'chapter' and not node['note_count']:
            label += '（尚无笔记）'
        if node.get('failed_pages'):
            label += '（原文部分解析失败）'
        attrs = {'TEXT' if format == 'freemind' else 'text': _xml_text(label)}
        details = '\n'.join(filter(None, [node.get('content', ''), _source_text(node)]))
        if node['kind'] == 'book':
            details = node['coverage']['scope']
        if format == 'opml' and details:
            attrs['_note'] = _xml_text(details)
        element = ET.SubElement(parent_node, 'node' if format == 'freemind' else 'outline', attrs)
        if format == 'freemind' and details:
            rich = ET.SubElement(element, 'richcontent', {'TYPE': 'NOTE'})
            body = ET.SubElement(ET.SubElement(rich, 'html'), 'body')
            ET.SubElement(body, 'p').text = _xml_text(details)
        for child in node.get('children', []):
            append(element, child)

    append(parent, tree)
    content = ET.tostring(root, encoding='utf-8', xml_declaration=True).decode('utf-8')
    return {'filename': stem + ('.mm' if format == 'freemind' else '.opml'),
            'media_type': 'application/xml;charset=utf-8', 'content': content}


CLIPPING_SOURCES = ('通用文本', '微信读书', 'Zotero', 'Notion', '飞书', '语雀', 'WPS', 'Obsidian')
CLIPPING_ID_RE = re.compile(r'^clip_[0-9a-f]{24}$')


def _clipping_path(layout: Layout, clipping_id: str) -> Path:
    if not CLIPPING_ID_RE.fullmatch(clipping_id):
        raise InputError('摘录 ID 非法')
    return layout.data_dir / 'clippings' / (clipping_id + '.json')


def add_clipping(layout: Layout, *, title: str, content: str, source: str = '通用文本', source_url: str = '') -> dict:
    title, content, source_url = title.strip(), content.strip(), source_url.strip()
    if not title or len(title) > 200:
        raise InputError('摘录标题须为 1–200 字')
    if not content or len(content.encode('utf-8')) > 512 * 1024:
        raise InputError('摘录内容不能为空，且不能超过 512 KiB')
    if source not in CLIPPING_SOURCES:
        raise InputError('未知摘录来源，请选择通用文本')
    if len(source_url) > 2048 or (source_url and (urlparse(source_url).scheme not in ('http', 'https') or not urlparse(source_url).netloc)):
        raise InputError('来源链接须为 http/https 网址，最长 2048 字')
    fingerprint = json.dumps([title, content, source, source_url], ensure_ascii=False).encode('utf-8')
    clipping_id = 'clip_' + hashlib.sha256(fingerprint).hexdigest()[:24]
    path = _clipping_path(layout, clipping_id)
    layout.locks_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(layout.locks_dir / 'clippings.lock'), timeout=10):
        if path.is_file():
            return {**load_json(path), 'duplicate': True}
        record = {'id': clipping_id, 'title': title, 'content': content, 'source': source,
                  'source_url': source_url, 'created_at': utc_now_iso()}
        rewrite_json(path, record)
    return {**record, 'duplicate': False}


def list_clippings(layout: Layout, limit: int = 100) -> dict:
    paths = sorted((layout.data_dir / 'clippings').glob('clip_*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    records = []
    for path in paths[:limit]:
        item = load_json(path)
        records.append({key: value for key, value in item.items() if key != 'content'})
    return {'items': records, 'total': len(paths)}


def get_clipping(layout: Layout, clipping_id: str) -> dict:
    path = _clipping_path(layout, clipping_id)
    if not path.is_file():
        raise NotFound('摘录不存在')
    return load_json(path)
