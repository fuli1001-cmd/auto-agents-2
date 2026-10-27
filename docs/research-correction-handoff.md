# Research source-correction handoff

A source-dispute correction owned by provider_research must be independently
reviewed even when the revised document has no external sources. An empty source
list changes retrieval work; it does not close the correction. Normal source-free
research without a correction retains its existing shortcut.

The accepted research document and the existing authority acceptance/completed
correction update are one checkpoint transaction. Review continues to validate
the previous clauses and retained obligations. No additional editing permission,
retry budget or accepted business requirement is introduced.

On resume, a cached research document with an unfinished research correction is
reviewed as saved, including verified_sources metadata. An old acceptance cannot
settle that active correction. A completed correction does not cause repeated
review. Other legacy research is not automatically reopened.

Counterexamples and acceptance: tests/test_research_correction_handoff.py use the
real Engine, authority validation, stage recovery and Store. Provider responses
and remote retrieval are controlled fixtures; historical demos are untouched.
