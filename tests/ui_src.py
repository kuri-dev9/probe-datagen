"""gen-ui 소스 읽기 — `ui.py` 가 `datagen/ui/` 패키지로 나뉘었다(2026-11-10).

소스 문자열을 보는 시험이 파일 하나를 가정하지 않게, 패키지 아래 `.py`·`.html`·`.js`·`.css` 를 이름순으로 이어 붙여 준다.
"""
from __future__ import annotations

from pathlib import Path

UI_DIR = Path(__file__).resolve().parent.parent / "src" / "datagen" / "ui"
EXTS = (".py", ".html", ".js", ".css")


def ui_files() -> list[Path]:
    return sorted(p for p in UI_DIR.rglob("*") if p.suffix in EXTS and "__pycache__" not in p.parts)


def ui_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in ui_files())
