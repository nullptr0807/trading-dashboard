"""Explore section: serve research articles (markdown + assets).

Layout:
  static/explore/
    index.json                          # list of posts (manifest)
    <slug>/
      article.en.md
      article.zh.md
      <image>.png  ...                  # served by /static/ mount

API:
  GET /api/explore                      → manifest JSON (list)
  GET /api/explore/{slug}?lang=en|zh    → markdown text
"""
import json
from pathlib import Path
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

router = APIRouter(prefix='/api/explore', tags=['explore'])

_ROOT = Path(__file__).resolve().parent.parent / 'static' / 'explore'
_INDEX = _ROOT / 'index.json'


@router.get('')
async def list_posts():
    """Return the list of published posts (newest first by date)."""
    if not _INDEX.exists():
        return JSONResponse({'posts': []})
    try:
        posts = json.loads(_INDEX.read_text(encoding='utf-8'))
    except Exception as e:
        return JSONResponse({'posts': [], 'error': str(e)}, status_code=500)
    posts = sorted(posts, key=lambda p: p.get('date', ''), reverse=True)
    return JSONResponse({'posts': posts})


@router.get('/{slug}', response_class=PlainTextResponse)
async def get_post(slug: str, lang: str = Query('en')):
    """Return the markdown for a single post in the requested language."""
    # Path-traversal guard
    if '/' in slug or '..' in slug or not slug.replace('-', '').replace('_', '').isalnum():
        raise HTTPException(status_code=400, detail='invalid slug')
    lang = (lang or 'en').lower()
    base = _ROOT / slug
    if not base.is_dir():
        raise HTTPException(status_code=404, detail='post not found')
    target = base / ('article.zh.md' if lang in ('zh', 'cn') else 'article.en.md')
    if not target.exists():
        # fall back to whichever exists
        target = (base / 'article.en.md') if (base / 'article.en.md').exists() else (base / 'article.zh.md')
    if not target.exists():
        raise HTTPException(status_code=404, detail='article missing')
    return target.read_text(encoding='utf-8')
