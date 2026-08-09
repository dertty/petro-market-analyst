# Improvement Backlog

This topic stores concrete, evidence-backed improvement items discovered during task execution.
Items here are advisory backlog nominations, not auto-started work.
Before implementation, run plan_task for non-trivial backlog items.

### ibl-585a2427ecb7
- status: open
- priority: low
- kind: bug
- created_at: 2026-08-09T17:48:12.885075+00:00
- last_seen: 2026-08-09T17:48:12.885075+00:00
- count: 1
- source: task_experience_review
- category: reliability
- task_id: c95e95364fab435e
- requires_plan_review: yes
- fingerprint: 585a2427ecb7
- summary: Improve timeout handling or add fallback mechanism for ext_12_r_duckduckgo_search tool
- evidence: ext_12_r_duckduckgo_search failed with ExtensionProcessError: extension child timed out after 30s
- proposed_next_step: Reduce timeout threshold or auto-fallback to web_search inside ext_12_r_duckduckgo_search extension wrapper.

### ibl-07979eebadf2
- status: open
- priority: high
- kind: improvement
- created_at: 2026-08-09T23:04:23.124212+00:00
- last_seen: 2026-08-09T23:04:23.124212+00:00
- count: 1
- source: task_trace
- category: execution_reliability
- task_id: 8f5d73956fc0489d
- requires_plan_review: yes
- fingerprint: 07979eebadf2
- summary: Добавить надёжный шаблон поэтапного запуска и checkpoint-сохранения для длинных аналитических скриптов
- evidence: run_script завершился exit_code=1; задача осталась best_effort из-за deadline_local, а последующий исправляющий запуск не успел обеспечить проверяемый финал
- proposed_next_step: Сделать reusable wrapper с try/except, промежуточными JSON-checkpoint и финальной валидацией результата

### ibl-fbbfc64d4669
- status: open
- priority: high
- kind: improvement
- created_at: 2026-08-09T23:04:23.124212+00:00
- last_seen: 2026-08-09T23:04:23.124212+00:00
- count: 1
- source: structured_outcome
- category: review_process
- task_id: 8f5d73956fc0489d
- requires_plan_review: yes
- fingerprint: fbbfc64d4669
- summary: Добавить контроль неизменности delivery candidate перед review
- evidence: review status degraded, trigger delivery_candidate_replaced; прежний PASS superseded, acceptance decision revision_requested из-за delivery_binding_superseded
- proposed_next_step: Перед отправкой вычислять и проверять binding/hash финального ответа и повторять review после любой замены кандидата

### ibl-a2fbf1ddc1de
- status: open
- priority: high
- kind: improvement
- created_at: 2026-08-09T23:51:16.502363+00:00
- last_seen: 2026-08-09T23:51:16.502363+00:00
- count: 1
- source: This task
- category: research_tooling
- task_id: a78aaed33916496e
- requires_plan_review: yes
- fingerprint: a2fbf1ddc1de
- summary: Add a source-verification and fallback workflow for inaccessible web pages
- evidence: Keenable fetch returned error_class=not_read for the Reuters/Fidelity page, while the primary SberCIB evidence was not demonstrably secured before finalization.
- proposed_next_step: Implement URL extraction retries, alternate page/PDF discovery, and a pre-finalization check requiring a citable primary source.

### ibl-5ca1be0b64d4
- status: open
- priority: med
- kind: improvement
- created_at: 2026-08-10T02:07:30.212311+00:00
- last_seen: 2026-08-10T02:07:30.212311+00:00
- count: 1
- source: Ошибка verify_and_record: TOOL_ARG_ERROR
- category: tooling
- task_id: 649ea95b110040d8
- requires_plan_review: yes
- fingerprint: 5ca1be0b64d4
- summary: Добавить предварительную валидацию аргументов verify_and_record с понятной подсказкой по допустимой схеме
- evidence: Вызов завершился ошибкой из-за invalid arguments; потребовался recovery call
- proposed_next_step: Сгенерировать или проверить JSON-schema перед отправкой вызова

### ibl-87edadadc89a
- status: open
- priority: high
- kind: capability_idea
- created_at: 2026-08-10T02:07:30.212311+00:00
- last_seen: 2026-08-10T02:07:30.212311+00:00
- count: 1
- source: Несогласованные входы в run_script
- category: market_data
- task_id: 649ea95b110040d8
- requires_plan_review: yes
- fingerprint: 87edadadc89a
- summary: Создать автоматическую проверку сопоставимости рыночных котировок перед расчётом спреда
- evidence: Использованы EIA spot 88.90 от 2026-08-03 и Investing.com futures 84.50 от 2026-08-10
- proposed_next_step: Блокировать расчёт при несовпадении даты, контракта, venue или типа инструмента

### ibl-1435df118000
- status: open
- priority: high
- kind: improvement
- created_at: 2026-08-10T02:18:49.428605+00:00
- last_seen: 2026-08-10T02:18:49.428605+00:00
- count: 1
- source: task_trace
- category: execution_reliability
- task_id: b53562df09804ad8
- requires_plan_review: yes
- fingerprint: 1435df118000
- summary: Добавить надёжный режим выполнения аналитических скриптов с checkpoint-файлами и автоматическим извлечением полного traceback после ненулевого выхода
- evidence: 12-й вызов run_script завершился exit_code=1 после частичного вывода прогноза; stderr/traceback был усечён, точная причина не установлена.
- proposed_next_step: Реализовать wrapper, который сохраняет stdout/stderr в файл, возвращает диагностический фрагмент и позволяет безопасно продолжить с последнего checkpoint.

### ibl-f0dbd2da56da
- status: open
- priority: high
- kind: improvement
- created_at: 2026-08-10T02:50:41.493569+00:00
- last_seen: 2026-08-10T02:50:41.493569+00:00
- count: 1
- source: task_acceptance_review
- category: research_workflow
- task_id: 76b97ab765444833
- requires_plan_review: yes
- fingerprint: f0dbd2da56da
- summary: Добавить процедуру автоматической валидации нефтяных котировок по типу инструмента, дате, timestamp и источнику
- evidence: В одном поиске смешались EIA spot за 3 августа ($88,90), Barchart August futures ($72,95), около $82–83 и слабый Reddit ($83,55); скрипт сравнил spot и futures напрямую.
- proposed_next_step: Создать шаблон проверки и блокирующее предупреждение при сравнении разных типов контрактов или дат.

### ibl-8ab53e238533
- status: open
- priority: med
- kind: improvement
- created_at: 2026-08-10T02:50:41.493569+00:00
- last_seen: 2026-08-10T02:50:41.493569+00:00
- count: 1
- source: execution_trace
- category: tooling
- task_id: 76b97ab765444833
- requires_plan_review: yes
- fingerprint: 8ab53e238533
- summary: Предусмотреть резервный маршрут при блокировке веб-источника
- evidence: Keenable fetch Forbes завершился `The target server denied access to this URL` с классом `not_read`; vendor не предоставил Retry-After.
- proposed_next_step: Добавить fallback к официальным биржевым/EIA API и не тратить повторные вызовы на заблокированный домен.
