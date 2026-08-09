"""Загрузка ingest/registry.yaml — единственного списка источников."""

from __future__ import annotations

import pathlib
from typing import Any, Dict, List, Optional

import yaml

REGISTRY_PATH = pathlib.Path(__file__).parent / "registry.yaml"


def load_sources(path: pathlib.Path = REGISTRY_PATH) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["sources"]


def collect_sources(path: pathlib.Path = REGISTRY_PATH) -> List[Dict[str, Any]]:
    return [s for s in load_sources(path) if s["status"] == "collect"]


def source_by_id(source_id: str, path: pathlib.Path = REGISTRY_PATH) -> Optional[Dict[str, Any]]:
    for s in load_sources(path):
        if s["id"] == source_id:
            return s
    return None
