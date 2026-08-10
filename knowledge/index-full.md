# Knowledge Base Index

- **calendar**: Когда сюда: проверить перед цитированием, вышел ли уже более свежий выпуск и не выдаётся ли старый за последний.
- **forecasting_readiness**: Перед рыночным прогнозом проверять доступность skills, базы знаний и модулей forecast/supply_shock/demand_shock/macro_sn
- **improvement-backlog**: This topic stores concrete, evidence-backed improvement items discovered during task execution.
- **market_data_validation**: Перед расчётом спредов сверять одинаковые дату, venue, тип котировки (spot/futures), контракт и timestamp; фиксировать и
- **market_price_validation**: Для текущих нефтяных котировок сначала фиксировать тип инструмента (spot/front-month), дату, timestamp и источник; не ср
- **method**: Когда сюда: правила свежести дат, цитирования чисел и модельных прогнозов — перед сборкой любого ответа с числами.
- **metrics**: Когда сюда: что означает метрика и как её корректно называть — сорта, кривая, запасы, крек-спред, буровые, макрофон.
- **patterns**: | Error class | Count | Root cause | Structural fix | Status |
- **research_execution**: Перед многоисточниковым исследованием проверять доступность skills; при SKILLS_UNAVAILABLE явно переходить на документир
- **research_workflow**: For time-sensitive market forecasts, secure and verify the primary issuer report first; record URL, publication date, ex
- **robust_analysis_execution**: Для многоэтапных рыночных расчётов запускать короткие независимые скрипты с обработкой ошибок и сохранять промежуточные 
- **robust_forecasting_runs**: Для многоэтапных прогнозных скриптов сохранять промежуточные результаты, разделять расчёты и источники, перехватывать ис
- **source_fallbacks**: При рыночном исследовании заранее готовить резервный источник для каждого ключевого прогноза; если Keenable возвращает n
- **source_validation**: Для рыночных обзоров проверять доступность skills до начала работы; числовые котировки и прогнозы подтверждать первичными источниками (EIA, OPEC, ICE…
- **source_verification**: При аналитике рынка не полагаться на один keyless KeenAble fetch: при Internal server error или not_read сразу использов
- **sources**: Когда сюда: выбор канала данных и ранга доверия источника, цена каждого класса каналов.
- **web_search_tools**: Prefer native `web_search` over `ext_12_r_duckduckgo_search` for time-sensitive market lookups, as `ext_12_r_duckduckgo_
