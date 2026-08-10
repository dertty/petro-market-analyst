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
- last_seen: 2026-08-10T04:47:11.038375+00:00
- count: 3
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

### ibl-e5d7837e0a25
- status: open
- priority: med
- kind: improvement
- created_at: 2026-08-10T04:53:10.686167+00:00
- last_seen: 2026-08-10T04:53:10.686167+00:00
- count: 1
- source: Ошибка ext_10_r_keenable_fetch_page_content при доступе к ht ⚠️ OMISSION NOTE: +20 chars omitted
- category: research_tooling
- task_id: cc7dcb290dbe4d45
- requires_plan_review: yes
- fingerprint: e5d7837e0a25
- summary: Добавить автоматический fallback для недоступных страниц OPEC
- evidence: error_class=not_read; target server denied access to this URL
- proposed_next_step: Реализовать последовательность альтернативных официальных URL/кеша и обязательную проверку вторым надежным источником

### ibl-c6c74ae62f80
- status: open
- priority: med
- kind: improvement
- created_at: 2026-08-10T04:57:04.735802+00:00
- last_seen: 2026-08-10T04:57:04.735802+00:00
- count: 1
- source: Текущая задача
- category: research_tooling
- task_id: bb1119db4b3749aa
- requires_plan_review: yes
- fingerprint: c6c74ae62f80
- summary: Добавить устойчивый fallback для получения еженедельных данных Baker Hughes при сбое официального fetch
- evidence: fetch_page_content для https://rigcount.bakerhughes.com/rig-count-overview завершился Internal server error; девять web_search не устранили расхождения
- proposed_next_step: Сформировать приоритетный список архивов/API и процедуру автоматической сверки даты, периода и значения

### ibl-ad1b1fb8c830
- status: open
- priority: med
- kind: improvement
- created_at: 2026-08-10T05:03:10.888117+00:00
- last_seen: 2026-08-10T05:03:10.888117+00:00
- count: 1
- source: Наблюдение по задаче 2026-08-10
- category: review_process
- task_id: bdc245ad93124756
- requires_plan_review: yes
- fingerprint: ad1b1fb8c830
- summary: Добавить процедуру проверки сопоставимости нефтяных цен и спредов при недоступности первичного источника
- evidence: Marketscreener fetch вернул not_read/server denied; альтернативные источники имели различный тип публикации и потенциально различный ценовой базис.
- proposed_next_step: Создать чек-лист: дата, benchmark, delivery basis, grade, assessment type, независимое подтверждение и маркировка неопределённости.

### ibl-3eefb63d7d0d
- status: open
- priority: high
- kind: improvement
- created_at: 2026-08-10T05:26:19.917002+00:00
- last_seen: 2026-08-10T05:26:19.917002+00:00
- count: 1
- source: task execution and review
- category: tooling
- task_id: f85b967e9f7f4b9a
- requires_plan_review: yes
- fingerprint: 3eefb63d7d0d
- summary: Добавить preflight-проверку forecasting-зависимостей и понятный fallback
- evidence: knowledge_read(topic='robust_forecasting_runs') exit_code=1; list_skills returned SKILLS_UNAVAILABLE; execution degraded with reason_code tool_failure
- proposed_next_step: Проверять OUROBOROS_SKILLS_REPO_PATH, доступность модулей и источников до начала веб-поиска; выдавать диагностический отчёт и шаблон ручного расчёта

### ibl-7961d563012b
- status: open
- priority: high
- kind: bug
- created_at: 2026-08-10T05:38:54.963712+00:00
- last_seen: 2026-08-10T05:38:54.963712+00:00
- count: 1
- source: Ошибка list_skills
- category: tooling
- task_id: d17d16f95e2642a1
- requires_plan_review: yes
- fingerprint: 7961d563012b
- summary: Восстановить discoverable skills и forecasting-модули в data plane
- evidence: SKILLS_UNAVAILABLE: No skills are discoverable; требуется OUROBOROS_SKILLS_REPO_PATH или установка skills
- proposed_next_step: Настроить путь репозитория в Settings → Behavior → External Skills Repo и добавить health-check перед задачами прогнозирования

### ibl-bae70e93dfb9
- status: open
- priority: med
- kind: improvement
- created_at: 2026-08-10T05:40:56.601120+00:00
- last_seen: 2026-08-10T05:40:56.601120+00:00
- count: 1
- source: list_skills error
- category: tooling
- task_id: 0a5c092557774993
- requires_plan_review: yes
- fingerprint: bae70e93dfb9
- summary: Добавить preflight-проверку доступности внешних skills и понятную диагностику конфигурации
- evidence: SKILLS_UNAVAILABLE: No skills are discoverable; требуется OUROBOROS_SKILLS_REPO_PATH или установка skills
- proposed_next_step: Проверять OUROBOROS_SKILLS_REPO_PATH до начала исследования и показывать actionable remediation

### ibl-ca821c77f471
- status: open
- priority: high
- kind: improvement
- created_at: 2026-08-10T05:40:56.601120+00:00
- last_seen: 2026-08-10T05:40:56.601120+00:00
- count: 1
- source: task_acceptance_review
- category: review_process
- task_id: 0a5c092557774993
- requires_plan_review: yes
- fingerprint: ca821c77f471
- summary: Ввести обязательный финальный чек-лист источников и артефакта для аналитических задач
- evidence: objective best_effort, review degraded, finalized_unaccepted; валидный quorum не достигнут
- proposed_next_step: Блокировать завершение без зафиксированных расчётов, источников и проверенного итогового артефакта

### ibl-d2d0c8945141
- status: open
- priority: high
- kind: improvement
- created_at: 2026-08-10T05:42:14.134215+00:00
- last_seen: 2026-08-10T05:42:14.134215+00:00
- count: 1
- source: Ошибка list_skills и degraded execution
- category: tooling
- task_id: 12a9eded7a9649ca
- requires_plan_review: yes
- fingerprint: d2d0c8945141
- summary: Добавить preflight-проверку доступности skills и явный резервный маршрут
- evidence: list_skills вернул SKILLS_UNAVAILABLE; итоговые outcome_axes: execution=degraded, objective=best_effort
- proposed_next_step: Перед аналитической задачей проверять OUROBOROS_SKILLS_REPO_PATH и сообщать о блокировке либо активировать проверенный fallback

### ibl-caeb07b04191
- status: open
- priority: high
- kind: improvement
- created_at: 2026-08-10T05:42:14.134215+00:00
- last_seen: 2026-08-10T05:42:14.134215+00:00
- count: 1
- source: Неполная валидация источников
- category: review_process
- task_id: 12a9eded7a9649ca
- requires_plan_review: yes
- fingerprint: caeb07b04191
- summary: Ввести автоматическую проверку происхождения рыночных чисел
- evidence: brent_current = 84.31 был задан в run_script; использован secondary source houseofsaud.com; review завершился finalized_unaccepted
- proposed_next_step: Требовать для каждой котировки URL, timestamp, первичный источник и независимое подтверждение перед финализацией

### ibl-3178c9c292c7
- status: open
- priority: high
- kind: bug
- created_at: 2026-08-10T05:49:04.852463+00:00
- last_seen: 2026-08-10T05:49:04.852463+00:00
- count: 1
- source: Execution trace: list_skills() error SKILLS_UNAVAILABLE
- category: environment/tooling
- task_id: b4317d98976245e7
- requires_plan_review: yes
- fingerprint: 3178c9c292c7
- summary: Восстановить обнаружение и диагностику внешних skills
- evidence: No skills are discoverable; требуется OUROBOROS_SKILLS_REPO_PATH или установка skills в data plane.
- proposed_next_step: Проверить настройку OUROBOROS_SKILLS_REPO_PATH, наличие skills в data plane и добавить preflight-тест с понятным remediation.

### ibl-864e6075b3c4
- status: open
- priority: med
- kind: improvement
- created_at: 2026-08-10T05:49:04.852463+00:00
- last_seen: 2026-08-10T05:49:04.852463+00:00
- count: 1
- source: Agent notes и усечённые tool logs
- category: process
- task_id: b4317d98976245e7
- requires_plan_review: yes
- fingerprint: 864e6075b3c4
- summary: Добавить preflight и receipt-аудит для рыночных прогнозов
- evidence: Недоступность модулей привела к ручному fallback; OMISSION NOTE ограничил проверку полного задания и источников.
- proposed_next_step: Перед поиском запускать проверку модулей, а в финальный пакет сохранять полный URL, timestamp, цитату и привязку каждого числа к источнику.

### ibl-2f29fc8f891e
- status: open
- priority: med
- kind: improvement
- created_at: 2026-08-10T06:58:41.716622+00:00
- last_seen: 2026-08-10T06:58:41.716622+00:00
- count: 1
- source: Repeated fetch failures during the 2026-08-10 oil forecast t ⚠️ OMISSION NOTE: +3 chars omitted
- category: research_tooling
- task_id: 70ccde3244224c85
- requires_plan_review: yes
- fingerprint: 2f29fc8f891e
- summary: Add an EIA STEO source-resolution fallback for changed or missing browser-data URLs
- evidence: STEO_m.xlsx and STEO_m.json both returned 'The requested URL could not be found' from fetch_page_content
- proposed_next_step: Resolve the official STEO release page first, extract its current download links, and fall back to HTML/PDF tables
