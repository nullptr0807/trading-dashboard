"""Serve the repo README.md (zh) or README.en.md (en) for the Intro tab."""
from pathlib import Path
from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix='/api/intro', tags=['intro'])

_REPO = Path(__file__).resolve().parent.parent
_README_ZH = _REPO / 'README.md'
_README_EN = _REPO / 'README.en.md'


@router.get('', response_class=PlainTextResponse)
async def get_intro(lang: str = Query('en')):
    """Return README content (markdown) for the requested language.

    `lang=zh` -> README.md (Chinese, primary).
    `lang=en` -> README.en.md (English, falls back to zh if missing).
    """
    lang = (lang or 'en').lower()
    target = _README_ZH if lang in ('zh', 'cn') else _README_EN
    if not target.exists():
        target = _README_ZH if _README_ZH.exists() else _README_EN
    if not target.exists():
        return PlainTextResponse('# README missing', status_code=404)
    return target.read_text(encoding='utf-8')
