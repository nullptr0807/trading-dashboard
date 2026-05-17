"""Frontier section: serve daily arXiv quant-research digests.

Layout (mirrors Explore):
  static/frontier/
    index.json                            # list of papers (manifest)
    <arxiv_id>/
      article.en.md
      article.zh.md
      hero.png  (optional)

API:
  GET /api/frontier                       → manifest JSON (list, newest first)
  GET /api/frontier/{arxiv_id}?lang=en|zh → markdown text
"""
import json
import re
from pathlib import Path
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

router = APIRouter(prefix='/api/frontier', tags=['frontier'])

_ROOT = Path(__file__).resolve().parent.parent / 'static' / 'frontier'
_INDEX = _ROOT / 'index.json'
_ID_RE = re.compile(r'^[\w.\-]+$')


@router.get('')
async def list_papers():
    """Return the list of digested papers (newest first by date)."""
    if not _INDEX.exists():
        return JSONResponse({'papers': []})
    try:
        papers = json.loads(_INDEX.read_text(encoding='utf-8'))
    except Exception as e:
        return JSONResponse({'papers': [], 'error': str(e)}, status_code=500)
    papers = sorted(papers, key=lambda p: p.get('date', ''), reverse=True)
    return JSONResponse({'papers': papers})


@router.get('/{arxiv_id}', response_class=PlainTextResponse)
async def get_paper(arxiv_id: str, lang: str = Query('en')):
    """Return the markdown digest for a single paper in the requested language."""
    if not _ID_RE.match(arxiv_id) or '..' in arxiv_id:
        raise HTTPException(status_code=400, detail='invalid id')
    lang = (lang or 'en').lower()
    base = _ROOT / arxiv_id
    if not base.is_dir():
        raise HTTPException(status_code=404, detail='paper not found')
    target = base / ('article.zh.md' if lang in ('zh', 'cn') else 'article.en.md')
    if not target.exists():
        target = (base / 'article.en.md') if (base / 'article.en.md').exists() else (base / 'article.zh.md')
    if not target.exists():
        raise HTTPException(status_code=404, detail='article missing')
    return target.read_text(encoding='utf-8')
