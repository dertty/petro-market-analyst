Prefer native `web_search` over `ext_12_r_duckduckgo_search` for time-sensitive market lookups, as `ext_12_r_duckduckgo_search` can encounter 30s extension child process timeouts.

Always pass `search_context_size="low"` to `web_search`. The backend default (`medium`) makes the provider fetch up to 10 sources per call (15–60K prompt tokens), so a single search can take minutes. Escalate to `"medium"` in a repeat call only when the low-context answer clearly lacks the needed facts.
