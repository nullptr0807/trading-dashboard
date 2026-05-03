"""Serve the repo README.md as raw markdown for the Intro tab."""
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix='/api/intro', tags=['intro'])

_README = Path(__file__).resolve().parent.parent / 'README.md'


@router.get('', response_class=PlainTextResponse)
async def get_intro():
    """Return README.md content as text/plain (frontend renders with marked.js)."""
    if not _README.exists():
        return PlainTextResponse('# README missing', status_code=404)
    return _README.read_text(encoding='utf-8')
