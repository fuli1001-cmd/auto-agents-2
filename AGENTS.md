# auto-agents-2

Implement the contracts in `docs/design.md`. Keep orchestration, provider transport,
verification, environment management and resource collection separate.

- Run Python through the project Conda environment or the explicitly selected
  `autoagents` environment. Never install into system Python or Conda base.
- Use `pytest` for behavioural and fault-injection tests. Validate the affected
  subsystem. Reuse source- and input-bound evidence for conflict-free integration;
  do not repeat tests or schedule full regression merely because code was merged.
  Resolve conflicts with checks limited to the affected scope. If a textually clean
  merge has a concrete semantic risk, report it before publishing or adding tests.
  Preserve known failures and incomplete observations; never relabel them as passes.
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

- Maintain one official development line (configured `origin/master`) for all
  projects. One coordinator owns product-code edits and integration at a time;
  start each fix from its latest accepted commit and publish fixes in order.
- Running tasks use clean, committed source checkouts from that line. Never edit
  code or shared dependencies that an active task may still load. At a safe paused
  boundary, confirm actual import path, commit, Python, project and managed-process
  state, then continue through the normal entry point with the new version.
- Source synchronization excludes project configuration, `.aa2` state, outputs and
  credentials. Preserve checkpoints, cumulative history, permissions and unresolved
  refusals across upgrades. Unknown denied operations must not be retried.
- Use Git history for code history; do not create dedicated rollback copies or
  reset task progress to hide an upgrade failure. Diagnose and fix forward.
  Never force-push the shared line.
