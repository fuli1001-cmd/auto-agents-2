"""Fixed stage ownership and controller-bound correction routing, not a workflow DSL."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from .errors import PolicyViolation
from .faults import StateConflict
from .util import digest

GATES = ('clarify', 'provider_research', 'prototype', 'design', 'plan', 'develop', 'readme', 'verify', 'deliver')
STAGE_REENTRY_ACTIONS = frozenset((*GATES[:4], 'readme'))
CORRECTION_VERSION = 'stage-correction-v1'
ACTION_OWNERS = {**{name: name for name in STAGE_REENTRY_ACTIONS}, 'plan_patch': 'plan',
                 'repair': 'develop', 'recheck': 'checks', 'prototype_recheck': 'prototype',
                 'preview_recheck': 'checks', 'diagnose': 'diagnosis', 'blocked': 'diagnosis'}
PRESERVE = ['original_user_input', 'user_constraints_and_consent', 'substantive_acceptance',
            'historical_assertions_and_collection', 'task_origins_and_cumulative_counts',
            'user_changes', 'candidate_history', 'failure_and_no_progress_history']
OWNERSHIP = {
    'clarify': 'Interpret captured user goals; do not replace the original input or invent user consent.',
    'provider_research': 'Research and cite sources; advice is not environment or acceptance evidence.',
    'prototype': 'Propose design/prototypes/** and DESIGN.md; browser review and consent remain separate.',
    'design': 'Propose architecture Markdown in docs/aa2/design.md, design-*.md or design/**.',
    'plan': 'Own task scopes, dependencies and executable checks; amendments require independent review.',
    'develop': 'Propose source changes inside the accepted task/repair scope; no upstream contract edits.',
    'readme': 'Propose README.md, ordinary docs and .env.example; no upstream contract or product edits.',
    'environment': 'Install the accepted dependency inputs and record actual identity; no business edits.',
    'checks': 'Execute accepted commands and record actual observations; no acceptance edits.',
    'review': 'Read and independently judge the candidate; never modify it or grant new permissions.',
    'deliver': 'Use the existing publication journal and verified candidate; no new design authority.',
}


def correction_contract():
    record = {'version': CORRECTION_VERSION, 'authority': 'stage_ownership_only',
              'owners': OWNERSHIP, 'actions': ACTION_OWNERS, 'preserve': PRESERVE}
    return {**record, 'hash': digest(record)}


def current_correction_contract(value):
    return value == correction_contract()


def _owned_path(path):
    if path == 'DESIGN.md' or path == 'design/prototypes' or path.startswith('design/prototypes/'):
        return 'prototype'
    if (path == 'docs/aa2/design.md' or path.startswith('docs/aa2/design-')
            or path.startswith('docs/aa2/design/')):
        return 'design'
    if path == 'docs/requirements' or path.startswith('docs/requirements/'):
        return 'clarify'
    if path == 'docs/aa2' or path.startswith('docs/aa2/'):
        return 'controller'
    return None


def validate_stage_writes(owner, changes, scopes):
    """An additional owner check; canonical paths, sensitive filtering and scopes still apply."""
    from .workspace import path_allowed
    if owner == 'implement':
        owner = 'develop'
    if owner not in {'prototype', 'design', 'develop', 'readme'}:
        raise PolicyViolation('This stage cannot propose file writes: ' + owner)
    for change in changes:
        path = change['path'] if isinstance(change, dict) else change.path
        posix = PurePosixPath(path)
        if (posix.is_absolute() or '..' in posix.parts or '\\' in path or str(posix) != path
                or not path_allowed(path, scopes)):
            raise PolicyViolation('Out-of-scope or noncanonical stage write: ' + path)
        actual = _owned_path(path)
        if owner in {'prototype', 'design'}:
            if actual != owner or owner == 'design' and not path.endswith('.md'):
                raise PolicyViolation('Stage does not own proposed artifact: ' + path)
        elif actual:
            raise PolicyViolation('Upstream artifact must return to its owning stage: ' + path)
        elif owner == 'readme' and not (path in {'README.md', '.env.example'} or path.startswith('docs/')):
            raise PolicyViolation('Documentation stage cannot edit product source: ' + path)


def _bindings(get, action):
    names = ['capture', 'clarify', 'provider_research', 'prototype', 'design', 'plan']
    # This handler produces new automatic approval evidence. Its existing permit
    # binds prototype identity and human consent; recheck_prototype revalidates
    # those live inputs and the exact published report/review on every resume.
    # Freezing the old automatic evidence here rejects the handler's own output.
    if action != 'prototype_recheck':
        names.append('prototype_approval')
    return {name: digest(get(name)) for name in names}


def _permit_identity(saved):
    return digest({key: saved.get(key) for key in ('action', 'source_hash', 'plan_hash', 'task_scopes',
                                                  'prototype_binding', 'preview_binding')})



def _route(action, has_plan):
    # A rejected first plan has no accepted contract to patch. Its existing
    # controller retry re-enters the plan author; an accepted plan never does.
    initial_plan = action == 'plan_patch' and not has_plan
    reentry = action in STAGE_REENTRY_ACTIONS or initial_plan
    stage = 'plan' if initial_plan else action
    return ('stage_reentry' if reentry else action,
            list(GATES[GATES.index(stage):]) if reentry else [])


def correction_request(store, rid, *, action, reason, evidence_refs, permit=None):
    """Normalize a selected/authorized action. This record itself never grants authority."""
    if action not in ACTION_OWNERS:
        raise StateConflict('Unsupported correction action; preserve the request')
    def get(key):
        return store.get(rid, key)
    handler, invalidated = _route(action, get('plan') is not None)
    request = {'version': CORRECTION_VERSION, 'action': action, 'owner': ACTION_OWNERS[action],
               'handler': handler,
               'reason': reason, 'evidence_refs': list(evidence_refs), 'permit': permit,
               'plan_revision': (get('plan_revision/current') or {}).get('revision', 1),
               'bindings': _bindings(get, action), 'preserve': list(PRESERVE),
               'contract_hash': correction_contract()['hash'],
               'invalidate_stages': invalidated,
               'mapping_source': 'bound PlanPatch.contract_corrections or timing_corrections'}
    if permit:
        saved = get(permit)
        if not saved or saved['action']['action'] != action:
            raise StateConflict('Correction owner does not match its existing action permit')
        request['permit_hash'] = _permit_identity(saved)
    return request


def validate_correction_request(request, get, *, action=None):
    """Validate against live records (including inside activation transactions)."""
    selected = request.get('action')
    expected, invalidated = _route(selected, get('plan') is not None)
    if (request.get('version') != CORRECTION_VERSION or selected not in ACTION_OWNERS
            or action is not None and selected != action or request.get('owner') != ACTION_OWNERS[selected]
            or request.get('handler') != expected or request.get('invalidate_stages') != invalidated
            or request.get('preserve') != PRESERVE
            or request.get('contract_hash') != correction_contract()['hash']):
        raise StateConflict('Invalid or obsolete stage correction responsibility contract')
    revision = (get('plan_revision/current') or {}).get('revision', 1)
    if request.get('plan_revision') != revision or request.get('bindings') != _bindings(get, selected):
        raise StateConflict('Stage correction inputs changed; preserve the request and candidate')
    if request.get('permit'):
        saved = get(request['permit'])
        if (not saved or saved['action']['action'] != selected or
                request.get('permit_hash') != _permit_identity(saved)):
            raise StateConflict('Stage correction no longer matches its authorized action')


def prepare_reentry(store, rid, pending, work):
    """Adapt legacy authorized re-entry without changing a question, answer or permission."""
    from .workspace import tree_hash
    stage = pending['stage']
    # Initial plan author retries have no accepted contract to amend.
    action = 'plan_patch' if stage == 'plan' else stage
    if stage == 'plan' and store.get(rid, 'plan'):
        raise StateConflict('An accepted plan must use PlanPatch, not whole-plan reset')
    if action not in STAGE_REENTRY_ACTIONS | {'plan_patch'} or not pending.get('granted'):
        raise StateConflict('Stage re-entry has no existing authorization')
    request = pending.get('correction_request')
    if request is None:
        request = correction_request(store, rid, action=action, reason=pending['reason'],
                                     evidence_refs=['replanning/pending'], permit=pending.get('correction_permit') or pending.get('permit'))
        pending = {**pending, 'correction_request': request,
                   'correction_source': {'workspace': str(work), 'hash': tree_hash(work)}}
        store.checkpoint(rid, 'replanning/pending', pending)
    validate_correction_request(request, lambda key: store.get(rid, key), action=action)
    source = pending['correction_source']
    if Path(source['workspace']) != work or tree_hash(work) != source['hash']:
        raise StateConflict('Stage correction source changed before re-entry')
    return pending


def responsibility_instructions():
    return '''
Stage responsibility is enforced by the controller. Return an evidenced defect to the owner:
requirements -> clarify; research -> provider_research; UI prototype -> prototype;
architecture -> design; accepted task/check contract -> plan_patch; scoped product defect -> repair.
The owner receives retained prior contracts and the failure reason, not a blank slate.
Review is read-only. Environment/check execution cannot rewrite business assertions. No role may
silently change original user goals, explicit constraints, consent, historical test assertions or budgets.
A transition only authorizes the existing handler; its candidate still needs its normal independent
review, applicable user gate, source/environment-bound checks and final acceptance.
Do not escape a rejected PlanPatch via another owner without evidence of that owner's defect.
'''
