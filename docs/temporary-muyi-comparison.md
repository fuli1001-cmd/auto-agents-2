# Temporary prototype comparison

This module exists only for one comparison of a blank Muyi prototype. It is not a
new workflow, provider framework, research path or product configuration option.

The source run must be paused after accepted shared research. The current task has
only requirements.md in its baseline, no earlier prototype or design, and no extra
submitted clarification answers. Other input shapes fail closed instead of being
silently generalized. The importer preserves the original acceptance receipt and
its hash-verified, owned artifact closure, and records the source run separately;
it does not issue another acceptance or alter the source run's history/counters.

Both arms receive the same frozen requirements, accepted research, visual brief,
design catalog and concise-interface instructions. Each has a new Store/run,
worktree and OS process. Codex generation uses gpt-6.1-sol with its configured
reasoning profile. AGY generation uses the existing gemini-3.8-flash-high/high
profile. Only prototype generation and its same-stage corrections select an arm;
review/decision use the unchanged source configuration. No generation fallback
crosses arms. Native input checks and access requirements remain unchanged.

The engine's normal start lifecycle, source bounds, patch checks, browser evidence,
independent review and final automation_current check remain in use. The private
defer_human hook stops before asking for or publishing human approval. Shared-input
re-entry is blocked for reconciliation. No arm may activate a formal prototype,
choose a winner, or continue design/plan/implementation. New questions and failures
retain their evidence and counters; resuming does not create fresh arms.

Entry: python -m auto_agents_2.temporary_prototype_compare --project SOURCE
--run SOURCE_RUN --output SEPARATE_OUTPUT --agy-config EXISTING_APP_CONFIG.
The output contains one frozen input, isolated arm projects and a result index.
A result is ready only when the current arm is paused for comparison and its source
hash still matches. A failed process cannot reuse an old ready label.

After the comparison is complete, remove this temporary module, its dedicated
tests and the two private engine hook options. Keep both output projects, original
source records, previews and review evidence for the user's comparison. Removing
the comparison mechanism must not erase user data or count as prototype approval.
