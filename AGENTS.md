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
- Store real secrets only in designated secret files (such as `.env` or configured
  `sensitive_paths`). Source, tests, documentation, examples and generated material
  must reference environment or configuration variables, never real secret literals.
  Use unmistakable placeholders in examples. Never copy or link secrets into ordinary
  files, prompts, logs or diagnostics. Model material admission protects secret paths;
  it does not inspect ordinary file contents, hashes or inodes for secrets.
