# Ordinary legacy state and architecture reintegration coverage

This is a bounded test addition following the demo7 retrospective. It does not
introduce a new production policy, requirement, retry budget or permission.

## Ordinary legacy cache consumption

`IntakeResult` and `DocumentResult` before the shared authority protocol (8ffedaa)
allow the omitted optional fields used by the fixture. A missing
`authority/accepted/clarify` marker, or source-free research without a correction,
does not itself require regeneration or a new research review. The Engine parses
and consumes those retained records under its existing compatibility contract.

The two cases in `test_legacy_interpretation_consumption.py` resume that real
Engine state and verify the next design-review boundary:

* A derived technical choice may be independently adopted. Original input stays
  distinct from the accepted interpretation; actual checks and delivery succeed.
* Labeling a method from the cached interpretation as explicit submitted input is
  rejected by the production authority validator. The bounded correction attempt
  pauses without a design adoption, implementation or delivery.

Both cases retain the original input and cached documents. They do not duplicate
the existing active-research-correction/interruption/review tests in
`test_research_correction_handoff.py`, nor schema-only authority unit tests.

## Architecture change combined with target advance

`test_reintegration_architecture_binding.py` modifies an existing architecture
Markdown file on the real target branch when the first final-review response is
produced, before delivery. Real Git
merge, command execution, authority validation and delivery consume the new tree.

* A fresh final review sees the changed document manifest and new source/basis;
  the old architecture receipt is explicitly unverified. A current independent
  review can approve the merged state under the existing contract.
* Replaying the old, otherwise valid final review fails with stale binding. The
  new user commit and isolated candidate remain; nothing is published and the
  accepted task's implementation round is not repeated or cleared.

This extends `test_target_advance_is_reintegrated_and_reverified`, which changes
`user.txt`. It does not add a rule forcing every architecture change back through
the design author. `_task_approval_context` checks prototype human consent, not
architecture adoption. Architecture evidence comes from `DesignDocuments.context`
and the final review's current authority basis. Checkpoint source identity can
remain unchanged while file/source hashes and the review basis change.

## Verification limits

Model responses are deterministic scenario fixtures. Checkpoints, schema and
authority consumers, Git operations, subprocess checks and delivery are real.
The existing Engine test helper supplies an isolated environment and disables
external toolchain provisioning; these tests do not measure live model semantics,
browser behaviour, environment installation or all legacy formats. Existing
receipt-damage, active-dispute and interruption coverage remains separate.
