# Final evidence at delivery recovery boundaries

The delivery journal retains the isolated candidate, plan, environment and final
validation identity. Runtime probes in the primary directory are not a substitute
for the complete final checks performed before publication.

Before every ready-to-publishing transition, the Engine adapter checks the saved
plan/validation/source and reuses the actual candidate command construction and
pure evidence.reusable gate. This includes a new candidate after a previous
publication; publication_started is not an exemption.

Before runtime and a new success receipt, the current final checks and visual
artifacts must still be readable and content-identical. Required machine reports,
attachment references and screenshot references cannot disappear into a generic
reference walk. Retired ordinary diagnostics remain allowed by ArtifactStore's
existing policy. Unrelated historical failed observations are not new gates.

A saved completion receipt must match the same current final snapshot. Reader
holds span receipt completion. Missing evidence raises StateConflict and retains
the candidate, journal, history and consumed budgets. Restoration of the exact
bound evidence permits normal recovery; this change grants no automatic repair,
recheck, deletion or rollback authority. Publication already in progress is
reconciled by the existing transaction before further progress. Previously
SUCCEEDED runs retain their historical result and cleanup behaviour.

This does not add a policy to rerun all final commands for every environment
change after publication. Current main-directory runtime validation is unchanged.
Any new automatic evidence regeneration policy requires separate review.

Tests: tests/test_delivery_final_evidence.py exercises actual Engine recovery,
Git and evidence consumers with controlled provider responses in temporary
projects. Screenshot corruption injections test archive integrity, not rendering
semantics. Existing browser protocol, publication, no-change, reintegration and
legacy recovery suites cover the related normal paths.
