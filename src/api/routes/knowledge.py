"""Local-token protected book maps, exchange downloads, and clipping inbox."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from src.core.knowledge import (CLIPPING_SOURCES, add_clipping, export_knowledge,
                                get_clipping, knowledge_tree, list_clippings)

router = APIRouter()


@router.get('/api/v1/docs/{doc_id}/knowledge')
def book_knowledge(request: Request, doc_id: str):
    return knowledge_tree(request.app.state.app_state.layout, doc_id)


@router.get('/api/v1/docs/{doc_id}/knowledge/export')
def book_knowledge_export(request: Request, doc_id: str, format: str = 'markdown'):
    return export_knowledge(knowledge_tree(request.app.state.app_state.layout, doc_id), format)


@router.get('/api/v1/integrations')
def integrations():
    return {'mode': 'file_and_paste', 'sources': list(CLIPPING_SOURCES),
            'formats': ['markdown', 'freemind', 'opml'],
            'description': '使用通用文件导出和纯文本摘录导入；不连接外部账号，不自动同步。'}


class ClippingInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=512 * 1024)
    source: str = '通用文本'
    source_url: str = Field(default='', max_length=2048)


@router.get('/api/v1/integrations/clippings')
def clipping_list(request: Request):
    return list_clippings(request.app.state.app_state.layout)


@router.post('/api/v1/integrations/clippings')
def clipping_create(request: Request, body: ClippingInput):
    return add_clipping(request.app.state.app_state.layout, **body.model_dump())


@router.get('/api/v1/integrations/clippings/{clipping_id}')
def clipping_detail(request: Request, clipping_id: str):
    return get_clipping(request.app.state.app_state.layout, clipping_id)
