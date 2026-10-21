"""The existing task lifecycle, shared by model roles; not a programmable workflow."""
from typing import Literal

STAGE_CONTRACT_VERSION = 'task-stages-v1'
TimingProducer = Literal['protected_review', 'environment', 'checks', 'acceptance']

STAGE_CONTRACT = """
<controller_stage_contract version="task-stages-v1">
The controller owns this fixed task lifecycle:
1. prerequisites: accepted requirements/design, applicable human approval and permissions,
   and necessary source/history material with its actual coverage boundaries.
2. candidate_generation: the implementation agent reads material and proposes changes,
   including a historical assertion/configuration/node correspondence when required.
   It does not apply files, approve changes, execute controller checks or certify its mapping.
3. protected_review: for protected test/config changes, the existing independent review examines the candidate
   and the supplied historical evidence BEFORE application. It may reject missing material,
   weakened assertions, changed collection behaviour or unauthorized dependencies.
4. application: the controller applies only a valid, authorized candidate.
5. environment: EnvironmentManager prepares dependencies and their actual resolution evidence.
6. checks: the controller runs the accepted commands against the current source/environment.
7. acceptance: independent implementation review and controller evidence checks accept the task.
Final integration checks and delivery acceptance remain separate and mandatory.

A plan describes business obligations inside these stages; prose cannot create another
controller gate, grant authority or move a downstream result ahead of its producer.
The protected_review verdict for a candidate cannot be required before candidate_generation.
Fresh checks and environment resolution for proposed changes are not pre-application results.
Missing necessary source material is a genuine prerequisite: identify exact unavailable inputs
and their relevance. Do not invent absence proof, ignore original user approval/order, or treat
archived proposals, old checks, a plan approval or a diagnosis as implementation acceptance.
An unsupported or ambiguous blocker is a request for evidence-backed diagnosis, not a new gate.
If an accepted plan explicitly contradicts this lifecycle, request a formal plan_patch; do not
silently ignore it. A new plan or plan amendment must not introduce the contradiction.
Reviewers check the actual phase and substantive obligations, not just the author's claim.
</controller_stage_contract>
"""


def stage_instructions(stage, context, purpose):
    if purpose not in {'plan', 'plan_patch', 'implement', 'implementation', 'test_changes',
                       'recovery_diagnosis', 'decision', 'verify'}:
        return ''
    if stage not in {'plan', 'implement', 'review', 'recovery_diagnosis', 'decision', 'verify'}:
        return ''
    text = STAGE_CONTRACT
    if purpose == 'plan_patch':
        text += """
For a proven scheduling defect in accepted task.acceptance, use PlanPatch.timing_corrections.
Each item identifies task_id, zero-based acceptance_index, exact previous text, full replacement,
the existing downstream producer (protected_review/environment/checks/acceptance), reason and
supplied evidence_refs. This is a TIMING-ONLY amendment: retain every substantive obligation,
historical assertion/configuration/collection requirement, environment proof and review.
Keep all task IDs, origins, requirements, dependencies, allowed_paths, checks and test minima,
and all environment/delivery contracts exactly unchanged. No task additions/removals, no new
acceptance obligations and no unrelated edits. upsert_tasks must contain exactly the corrected
tasks, with acceptance in the same order; change only the mapped clauses and related description.
Align document and descriptions with the corrected timing without hiding old obligations.
If a separate scope/check change is necessary, request a separate ordinary amendment first.
Never use timing_corrections to move an actual user-required prerequisite or missing source
material out of the prerequisite stage. Unknown semantics require diagnosis or clarification.
Without timing_corrections, the existing literal acceptance-preservation rule still applies.
"""
        if stage == 'review':
            text += """
For a timing amendment return TimingPlanReview with one timing_reviews item per correction,
matching task_id and acceptance_index. Independently assess timing_only, obligations_preserved
and user_order_preserved, with a specific reason and evidence_refs to previous_plan,
specification/design or other supplied controller evidence. Do not cite only patch/plan claims.
Reject substantive weakening, legitimate prerequisites moved later, unsupported equivalence,
contradictory descriptions or missing historical obligations, even if structured checks match.
An approved timing review changes scheduling only; it does not certify the candidate's historical
mapping, dependency compatibility, passing checks or final acceptance. Those original gates remain.
"""
    if stage in {'recovery_diagnosis', 'decision'}:
        text += """
Use plan_patch for a supported phase conflict in an accepted contract, repair for an actual
in-scope candidate defect, and recheck only when the existing commands have executable inputs.
Another diagnose is useful only if it can answer a specific unresolved question from available
evidence. A request to diagnose again does not run an audit or produce an approval receipt.
Do not evade a controller-excluded repeated action by changing wording or moving to another
owning stage without evidence of a genuine defect in that stage.
"""
    return text
