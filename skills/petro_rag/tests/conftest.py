"""Общие фикстуры тестов ядра.

Каталог скилла кладётся в sys.path, чтобы `from lib import ...` работал так же, как
в scripts/. Внутри самого пакета импорты относительные, поэтому загрузчику Ouroboros
это не мешает.
"""

from __future__ import annotations

import hashlib
import math
import sys
from pathlib import Path
from typing import List, Sequence

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SKILL_DIR.parent.parent
sys.path.insert(0, str(SKILL_DIR))

PDF_DIR = PROJECT_ROOT / "reports" / "pdf"


class FakeEmbedder:
    """Детерминированный эмбеддер без модели: мешок слов, разложенный по измерениям.

    Нужен, чтобы механика хранилища и поиска проверялась быстро и без сети. Качество
    поиска им мерить нельзя — для этого есть scripts/eval_rag.py на настоящей модели.
    """

    id = "fake:bag-of-words"
    dim = 64

    def _vector(self, text: str) -> List[float]:
        vector = [0.0] * self.dim
        for word in text.lower().split():
            digest = hashlib.md5(word.encode("utf-8")).digest()
            vector[digest[0] % self.dim] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def passages(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._vector(text) for text in texts]

    def query(self, text: str) -> List[float]:
        return self._vector(text)


@pytest.fixture(scope="session")
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture(scope="session")
def strategy_pdf() -> Path:
    """Презентация SberCIB — единственный отчёт с достоверной датой и 63 страницами."""
    path = PDF_DIR / "SberCIB-Strategiya-2026.pdf"
    if not path.exists():
        pytest.skip(f"нет {path}: тесты нарезки требуют реальный отчёт")
    return path
