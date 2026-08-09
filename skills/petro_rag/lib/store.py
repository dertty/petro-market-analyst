"""Векторное хранилище поверх Chroma.

Векторы считает провайдер из embedding.py, а не сама Chroma: иначе выбор модели
оказался бы зашит в хранилище и менялся бы вместе с ним. Chroma здесь — только
персистентный индекс и метаданные.

Идентификатор эмбеддера хранится в метаданных коллекции и сверяется при открытии.
Разные модели дают векторы разной размерности и несравнимой геометрии, поэтому поиск
по чужому индексу должен падать с внятной просьбой пересобрать, а не молча выдавать
правдоподобный мусор.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence

from .chunking import Chunk

COLLECTION = "petro_reports"
DEFAULT_INDEX_DIR = "/workspace/reports/index"

# Косинус, а не евклидово расстояние по умолчанию: для текстовых эмбеддингов значима
# только направленность вектора. Для нормализованных векторов порядок совпадает, но
# полагаться на то, что провайдер нормализует, не стоит — он сменный.
_SPACE = "cosine"

# Батч записи. Чем крупнее, тем быстрее, но у Chroma есть предел на размер партии.
_ADD_BATCH = 256


def index_path(path: Optional[str] = None) -> Path:
    return Path(path or os.environ.get("PETRO_RAG_INDEX") or DEFAULT_INDEX_DIR)


def _client(path: Optional[str] = None):
    import chromadb

    directory = index_path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(directory))


def build(chunks: Sequence[Chunk], embedder, *, path: Optional[str] = None) -> int:
    """Собирает индекс с нуля. Возвращает число записанных чанков.

    Пересборка всегда полная: коллекция удаляется целиком. Инкрементальное
    обновление потребовало бы сверки по содержимому файлов, а корпус маленький и
    пересобирается за минуты — сложность не окупается.
    """
    client = _client(path)
    try:
        client.delete_collection(COLLECTION)
    except Exception:
        # Коллекции ещё нет — обычный случай первого запуска.
        pass

    collection = client.create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": _SPACE, "embedder": embedder.id, "dim": embedder.dim},
    )

    written = 0
    for start in range(0, len(chunks), _ADD_BATCH):
        batch = list(chunks[start : start + _ADD_BATCH])
        collection.add(
            ids=[chunk.chunk_id for chunk in batch],
            embeddings=embedder.passages([chunk.text for chunk in batch]),
            documents=[chunk.text for chunk in batch],
            metadatas=[chunk.metadata() for chunk in batch],
        )
        written += len(batch)
    return written


def open_collection(embedder, *, path: Optional[str] = None):
    """Открывает существующую коллекцию, проверив, что она построена тем же эмбеддером."""
    client = _client(path)
    try:
        collection = client.get_collection(COLLECTION)
    except Exception as exc:
        raise RuntimeError(
            f"индекс не найден в {index_path(path)} — соберите его: "
            f"python scripts/build_index.py"
        ) from exc

    stored = (collection.metadata or {}).get("embedder")
    if stored and stored != embedder.id:
        raise RuntimeError(
            f"индекс построен эмбеддером '{stored}', а сейчас выбран '{embedder.id}'. "
            f"Векторы несравнимы — пересоберите индекс: python scripts/build_index.py"
        )
    return collection
