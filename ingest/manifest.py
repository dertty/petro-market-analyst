"""data/corpus/manifest.jsonl — единственный вход для будущего индексатора.

Только дописывается, никогда не переписывается (см. план, раздел «Манифест»): индексатор
на следующей итерации сможет запомнить позицию и читать лишь новые строки. Дедупликация —
по doc_id (для raw/, где doc_id детерминирован из реестра) и по sha256 (обнаружение того,
что single-источник не изменился).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Optional

CORPUS_ROOT = pathlib.Path(__file__).parent.parent / "data" / "corpus"
MANIFEST_PATH = CORPUS_ROOT / "manifest.jsonl"


class ManifestStore:
    def __init__(self, path: pathlib.Path = MANIFEST_PATH) -> None:
        self.path = path
        self._entries: List[Dict[str, Any]] = self._load()
        self._by_doc_id = {e["doc_id"]: e for e in self._entries}
        self._paths = {e["path"] for e in self._entries}

    def _load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def has_doc_id(self, doc_id: str) -> bool:
        return doc_id in self._by_doc_id

    def has_path(self, path: str) -> bool:
        """Для uploads/: файлы пользователя не переименовываются, поэтому path — более
        естественный ключ идемпотентности, чем doc_id (который для них не детерминирован
        при отсутствии даты в имени файла)."""
        return path in self._paths

    def get(self, doc_id: str) -> Optional[Dict[str, Any]]:
        return self._by_doc_id.get(doc_id)

    def latest_for_source(self, source_id: str) -> Optional[Dict[str, Any]]:
        """Последняя по retrieved_at запись source_id — для single-источников:
        сравнить новый sha256 с тем, что уже записан, прежде чем плодить дубликат."""
        candidates = [e for e in self._entries if e.get("source_id") == source_id]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e["retrieved_at"])

    def append(self, entry: Dict[str, Any]) -> None:
        if self.has_doc_id(entry["doc_id"]):
            raise ValueError(f"doc_id {entry['doc_id']!r} уже есть в манифесте")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._entries.append(entry)
        self._by_doc_id[entry["doc_id"]] = entry
        self._paths.add(entry["path"])

    @property
    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)
