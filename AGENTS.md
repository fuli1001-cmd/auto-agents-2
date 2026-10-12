# auto-agents-2

Implement the contracts in `docs/design.md`. Keep orchestration, provider transport,
verification, environment management and resource collection separate.

- Run Python through the project Conda environment or the explicitly selected
  `autoagents` environment. Never install into system Python or Conda base.
- Use `pytest` for behavioural and fault-injection tests. Test the affected subsystem
  first, and run the complete suite before delivery.
- Models and reasoning settings belong to user profiles. Never select newer models
  automatically or silently weaken execution permissions.
- A provider response is a proposal. Only verified evidence may advance a gate.
- Preserve unfinished iterations, user changes, protected tests and recovery artifacts.
- Do not include credentials or native provider configuration contents in logs.
