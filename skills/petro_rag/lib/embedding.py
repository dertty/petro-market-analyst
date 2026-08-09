"""Провайдеры векторного представления текста.

Модель выбирается строкой вида `<провайдер>:<модель>` из переменной окружения
`PETRO_RAG_EMBEDDER`, поэтому замена локальной модели на API и обратно — это правка
одной строки в .env, без изменения кода индексатора, поиска и скилла.

Асимметрия query/passage живёт здесь, а не в вызывающем коде: модели семейства e5
обучены на парах с литеральными префиксами `query: ` и `passage: `, и без них качество
поиска заметно падает — это ошибка номер один при работе с e5. Проверено по коду:
fastembed такие префиксы сам НЕ добавляет (в пакете их нет ни одного вхождения), а у
OpenAI-эмбеддингов префиксы, наоборот, не нужны вовсе. Общий код не должен знать об
этой разнице, поэтому каждый провайдер обслуживает её сам.

`id` провайдера записывается в индекс. Разные модели дают несравнимые векторы (у
multilingual-e5-large 1024 измерения, у text-embedding-3-small — 1536), поэтому поиск
по индексу, построенному другой моделью, обязан падать с внятной ошибкой, а не
возвращать правдоподобный мусор.
"""

from __future__ import annotations

import os
from typing import List, Protocol, Sequence

DEFAULT_SPEC = "fastembed:intfloat/multilingual-e5-large"

# Модели, обученные с префиксами. Проверка по подстроке: у e5 нет иного признака,
# а список чекпойнтов меняется чаще, чем это соглашение.
_PREFIXED_FAMILIES = ("e5",)


class Embedder(Protocol):
    """Минимум, который нужен индексатору и поиску."""

    id: str
    dim: int

    def passages(self, texts: Sequence[str]) -> List[List[float]]:
        """Векторы фрагментов документа."""

    def query(self, text: str) -> List[float]:
        """Вектор поискового запроса."""


class FastembedEmbedder:
    """Локальная ONNX-модель. Веса скачиваются один раз в PETRO_RAG_MODELS."""

    def __init__(self, model: str, cache_dir: str | None = None) -> None:
        from fastembed import TextEmbedding

        self.id = f"fastembed:{model}"
        self._model_name = model
        self._prefixed = any(family in model.lower() for family in _PREFIXED_FAMILIES)
        self._model = TextEmbedding(
            model_name=model,
            cache_dir=cache_dir or os.environ.get("PETRO_RAG_MODELS") or None,
        )
        self.dim = int(
            next(
                item["dim"]
                for item in TextEmbedding.list_supported_models()
                if item["model"] == model
            )
        )

    def passages(self, texts: Sequence[str]) -> List[List[float]]:
        prepared = [f"passage: {t}" if self._prefixed else t for t in texts]
        return [vector.tolist() for vector in self._model.embed(prepared)]

    def query(self, text: str) -> List[float]:
        prepared = f"query: {text}" if self._prefixed else text
        return next(iter(self._model.embed([prepared]))).tolist()


class OpenAIEmbedder:
    """Эмбеддинги через API. Ничего не грузит в память, но требует ключа и сети."""

    _DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}

    def __init__(self, model: str) -> None:
        from openai import OpenAI

        self.id = f"openai:{model}"
        self._model = model
        self._client = OpenAI()
        self.dim = self._DIMS.get(model, 1536)

    def _embed(self, texts: Sequence[str]) -> List[List[float]]:
        response = self._client.embeddings.create(model=self._model, input=list(texts))
        return [item.embedding for item in response.data]

    def passages(self, texts: Sequence[str]) -> List[List[float]]:
        return self._embed(texts)

    def query(self, text: str) -> List[float]:
        return self._embed([text])[0]


def make_embedder(spec: str | None = None) -> Embedder:
    """Создаёт провайдер по строке `<провайдер>:<модель>`.

    Без аргумента берёт PETRO_RAG_EMBEDDER, иначе локальную e5.
    """
    spec = (spec or os.environ.get("PETRO_RAG_EMBEDDER") or DEFAULT_SPEC).strip()
    provider, _, model = spec.partition(":")
    if not model:
        raise ValueError(
            f"неполная спецификация эмбеддера '{spec}', нужен вид '<провайдер>:<модель>', "
            f"например '{DEFAULT_SPEC}'"
        )

    if provider == "fastembed":
        return FastembedEmbedder(model)
    if provider == "openai":
        return OpenAIEmbedder(model)
    raise ValueError(
        f"неизвестный провайдер эмбеддингов '{provider}', доступны: fastembed, openai"
    )
