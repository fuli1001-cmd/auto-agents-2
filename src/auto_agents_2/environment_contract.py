"""Finite description of the existing dependency observer; no configurable collectors."""
import json


def resolution_capture_scope():
    """Describe coverage, not package compatibility or a successful historical solve."""
    return {
        "version": 1,
        "kind": "installed_dependency_snapshot",
        "observed": [
            "Complete recorded pip/Conda locks and their identity hashes",
            "Selected interpreter/runtime and installed Python distribution metadata",
            "Installed Conda package identity and depends/constrains edges",
        ],
        "conditional_records": {
            "installation": "Retained pip report/transcript with its actual recorded or not_recorded status",
            "shared_browsers": "Verified shared-browser records when shared caching is enabled",
        },
        "not_captured": {
            "effective_solver_configuration": {
                "status": "not_collected",
                "includes": ["solver backend", "effective channel ordering and priority",
                             "merged solver settings and configured pins"],
            },
        },
        "interpretation": "Complete locks describe the installed resolution, not a complete solve-time "
            "configuration snapshot. Package URLs and dependency constrains do not prove effective "
            "channel priority, solver selection or all configured constraints. not_collected is not "
            "evidence that a setting is absent, default, safe or unnecessary.",
        "recheck": "Recheck prepares the environment and executes the accepted checks using existing "
            "collectors. A recovery reason or plan sentence cannot add a collector, read host configuration "
            "or reconstruct unrecorded historical settings.",
    }


def environment_evidence_instructions(purpose):
    if purpose not in {'design', 'plan', 'plan_patch', 'implement', 'implementation',
                       'test_changes', 'verify', 'recovery_diagnosis', 'decision'}:
        return ''
    return ("\nThe environment_resolution collector has this finite coverage:\n"
            + json.dumps(resolution_capture_scope(), ensure_ascii=False)
            + "\nThis is a capability description, not an evidence ID or project acceptance. Read each "
              "actual artifact and its recorded scope; an older artifact without capture_scope has no "
              "new historical observations. Distinguish a missing/corrupt promised record from information "
              "the collector never records. Never infer missing values or waive integrity checks. "
              "Trace a disputed obligation to original user requirements and the exact approved plan "
              "revision. A diagnosis suggestion, an approved plan or successful checks do not prove a "
              "new controller collection capability exists. Do not add unsupported controller work as "
              "a project acceptance obligation. For a proven MODEL-DERIVED erroneous technical clause "
              "in an accepted plan, request the existing formal plan_patch.contract_corrections, with "
              "old/new mapping, preserved substantive duties and independent semantic review. Do not "
              "silently ignore or delete it. An explicit user obligation or genuinely necessary missing "
              "evidence remains blocking; identify the exact unsupported capability instead of inventing "
              "evidence or repeatedly rechecking. Preserve installed version/source evidence, package "
              "compatibility checks, historical assertions and all genuine business acceptance.\n")
