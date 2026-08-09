# Pattern Register

| Error class | Count | Root cause | Structural fix | Status |
|-------------|-------|------------|----------------|--------|
| `RESOURCE_POLICY_BLOCKED` | 1 | Attempting static introspection/file access on code protected by `task_contract.resource_policy` | Respect black-box resource boundaries; avoid probing blocked system paths and use authorized task context/tools | Active |
| `IndexError: string index out of range` | 1 | Unchecked string slicing or indexing in script executed via `run_script` | Add boundary/length checks before indexing strings in custom Python scripts | Active |
| `ExtensionProcessError: extension child timed out` | 1 | External tool (`ext_12_r_duckduckgo_search`) child process timed out after 30s during network search request | Implement retry logic with exponential backoff on timeout | Active |
| `SHELL_EXIT_ERROR` | 2 | Recurrent unhandled shell/script failure (`run_script` exited with code 1) during the multi-source Brent forecast pipeline, after collecting data from `ext_20_r_oil_price_forecast_forecast`, `ext_20_r_oil_price_forecast_macro_snapshot`, RAG, `ext_10_r_keenable_search_web_pages`, and EIA | Decompose forecasting into independently validated stages; check subprocess return codes and `STDERR`; isolate web/RAG/tool outputs from statistical calculations; add exception handling and persist validated intermediate datasets before continuing | Active |
