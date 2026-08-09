"""Разведка достижимости источников перед тем, как писать под них адаптеры.

Один GET на landing-страницу каждого source со status: collect. Не качает выпуски —
только проверяет, что издатель вообще отвечает нашему User-Agent. Источники OPEC
проверяются первыми: при разработке реестра opec.org дважды отдал 402 Payment Required на
обычный WebFetch, и если это повторится отсюда — с честным User-Agent и без обхода — OPEC
переносится в registry.yaml как status: skip, reason: blocked, а не получает вручную
подобранный обход.

Использование:
    python -m ingest.probe                # все source со status: collect
    python -m ingest.probe --only OPEC-001 EIA-001
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

from ingest import http
from ingest.console import ensure_utf8_stdout
from ingest.registry import collect_sources

REPORT_PATH = pathlib.Path(__file__).parent.parent / "data" / "corpus" / "probe-report.json"


def _check_url(source: Dict[str, Any]) -> str:
    """URL, по которому проверяем источник — landing-страница, как открыл бы человек."""
    url = source.get("landing")
    if not url:
        params = source.get("params") or {}
        url = params.get("landing") or params.get("file_url")
    if not url:
        raise ValueError(f"{source['id']}: нет landing/params.landing/params.file_url для проверки")
    return url


def _classify(status_code: int) -> str:
    if 200 <= status_code < 300:
        return "ok"
    if status_code in (401, 402, 403, 429):
        return "blocked"
    if status_code >= 500:
        return "server_error"
    return "unexpected"


def probe_one(client, source: Dict[str, Any]) -> Dict[str, Any]:
    url = _check_url(source)
    row: Dict[str, Any] = {
        "id": source["id"],
        "publisher": source["publisher"],
        "series": source["series"],
        "url": url,
        "robots_allowed": http.robots_allowed(url),
    }
    # Любая сетевая ошибка репортится в таблицу, а не роняет весь прогон.
    try:
        response = http.request(client, "GET", url)
    except Exception as exc:  # noqa: BLE001
        row["result"] = "error"
        row["detail"] = f"{type(exc).__name__}: {exc}"
        return row
    row["http_status"] = response.status_code
    row["content_type"] = response.headers.get("content-type", "")
    row["result"] = _classify(response.status_code)
    return row


def run(source_ids: List[str] | None) -> List[Dict[str, Any]]:
    sources = collect_sources()
    if source_ids:
        wanted = set(source_ids)
        sources = [s for s in sources if s["id"] in wanted]
        missing = wanted - {s["id"] for s in sources}
        if missing:
            print(f"Не найдены в реестре (или не status: collect): {sorted(missing)}", file=sys.stderr)

    # OPEC — первым: единственный издатель, для которого уже зафиксирован повод для сомнений.
    sources.sort(key=lambda s: (s["publisher"] != "OPEC", s["id"]))

    rows: List[Dict[str, Any]] = []
    with http.make_client() as client:
        for source in sources:
            row = probe_one(client, source)
            rows.append(row)
            flag = {"ok": "OK", "blocked": "BLOCKED", "server_error": "5xx", "error": "ERROR"}.get(
                row["result"], row["result"].upper()
            )
            detail = row.get("http_status", row.get("detail", ""))
            print(f"{flag:9} {row['id']:10} {row['publisher']:28} {detail}")
    return rows


def main() -> None:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", metavar="SOURCE_ID", help="проверить только эти id")
    args = parser.parse_args()

    rows = run(args.only)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОтчёт: {REPORT_PATH}")

    blocked = [r for r in rows if r["result"] != "ok"]
    if blocked:
        print(f"\n{len(blocked)} источник(ов) не прошли проверку:")
        for r in blocked:
            print(f"  {r['id']} ({r['publisher']}): {r['result']} — {r.get('detail', r.get('http_status'))}")
        opec_blocked = [r for r in blocked if r["publisher"] == "OPEC"]
        if opec_blocked:
            print(
                "\nOPEC не прошёл разведку достижимости даже с честным User-Agent — это "
                "не платный доступ, а вероятная бот-защита landing-страниц. Следующий шаг: "
                "перевести OPEC-* в registry.yaml на status: skip, reason: blocked и сообщить "
                "об этом, а не подбирать обход."
            )


if __name__ == "__main__":
    main()
