"""Concrete controller binding for the explicitly authorized output-limit incident."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import stat
import time
from pathlib import Path

from .config import ConfigStore
from .errors import AA2Error
from .safe_files import directory_fd, read_regular, write_regular
from .store import Store
from .util import digest

VERSION = "output-limit-runtime-v1"
BUSINESS_ROLES = ("correction", "protected_review", "implementation_review")
PROBE_ROLES = ("material_probe_implement", "material_probe_review")
LIMITS = {role: 1 for role in (*BUSINESS_ROLES, *PROBE_ROLES)}


def budget_for_request(request):
    return {role: (1 if role in BUSINESS_ROLES or request.get("material_probe_authorization_reference") else 0)
            for role in LIMITS}


def controller_identity(root, *, require_owner=True):
    """Use the real flock owner, boot and PID start, never a supplied PID label."""
    root = Path(root)
    pid = os.getpid()
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    identity = {"pid": pid, "start_ticks": int(fields[19]),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    if require_owner:
        info = (root / "controller.lock").stat()
        expected = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
        owned = False
        for line in Path("/proc/locks").read_text().splitlines():
            row = line.split()
            if len(row) < 6 or row[1:4] != ["FLOCK", "ADVISORY", "WRITE"] or row[4] != str(pid):
                continue
            device = row[5].split(":")
            if len(device) == 3 and (int(device[0], 16), int(device[1], 16), int(device[2])) == expected:
                owned = True
        if not owned:
            raise AA2Error("Incident runtime requires this process to own controller.lock")
    return identity


def live_controller(identity):
    try:
        fields = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(") ", 1)[1].split()
        return (int(fields[19]) == identity["start_ticks"] and fields[0] not in {"Z", "X"}
                and Path("/proc/sys/kernel/random/boot_id").read_text().strip() == identity["boot_id"])
    except (OSError, KeyError, ValueError):
        return False


def native_inputs(router, eff):
    transport = router.transports[eff["kind"]]
    native = transport.native_home(eff["environment"])
    from .transports import native_input_bytes
    return {file.relative_to(native).as_posix(): digest(native_input_bytes(file))
            for file in router._native_inputs(transport, eff)}


def prepared_tree(root):
    """Manifest the already filtered view, independently of execution tree identity."""
    from .workspace import file_digest
    result = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise AA2Error("Prepared incident view contains a non-regular file")
        result[path.relative_to(root).as_posix()] = {"sha256": file_digest(path), "mode": stat.S_IMODE(info.st_mode)}
    return result


async def profile_snapshot(router, stage, workspace):
    eff = router.effective(stage, router.config.load().active_provider)
    if eff["kind"] != "codex":
        raise AA2Error("Incident runtime supports only the frozen Codex profiles")
    material = await router._material_identity(eff, workspace)
    from .workspace import file_digest
    sandbox_binary = Path(router.sandbox.binary)
    return {"provider": eff["provider"], "kind": eff["kind"], "level": eff["level"],
            "model": eff["model"], "effort": eff["effort"], "fingerprint": eff["fingerprint"],
            "environment_hash": digest(eff["environment"]), "profile_hashes": eff["profile_hashes"],
            "execution_component": router.execution_identity(stage), "native_inputs": native_inputs(router, eff),
            "binary": material["binary"], "binary_hash": material["binary_hash"],
            "version_hash": material["version_hash"], "capability_key": material["key"],
            "sandbox_binary_hash": file_digest(sandbox_binary) if sandbox_binary.is_file() else None,
            "test_unconfined": router.sandbox.test_unconfined}


def incident_directory(store, request):
    return store.root / "output-limit-runtime" / digest(request)


def quote_paths(store, request, nonce):
    if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise AA2Error("Invalid incident quote nonce")
    directory = incident_directory(store, request)
    return directory / ("quote-" + nonce + ".json"), directory / ("activate-" + nonce + ".json")


def publish_activation(project, quote_path, expected_sha256, authorization_reference):
    """External command writes a bounded message only; the lock owner mutates state."""
    from .output_limit_recovery import KEY, code_identity, raw_hash
    raw = read_regular(Path(quote_path), 65536)
    if raw_hash(raw) != expected_sha256:
        raise AA2Error("Incident quote bytes changed")
    quote = json.loads(raw)
    store = Store(Path(project), readonly=True)
    record = store.get(quote["run_id"], KEY)
    if (not record or record["state"] != "active" or digest(record["request"]) != quote["request_hash"]
            or record["request"]["code_identity"] != code_identity()
            or record["request"]["config_hash"] != digest(ConfigStore(project).load().model_dump(mode="json"))
            or not live_controller(quote["controller"])
            or not isinstance(authorization_reference, str) or not authorization_reference.strip()):
        raise AA2Error("Incident quote or live authorization changed")
    pending = record.get("runtime_quotes", {}).get(quote["nonce"])
    if not pending or pending["quote"] != quote or pending["sha256"] != expected_sha256:
        raise AA2Error("Quote is not the outstanding controller-issued request")
    expected_path, message_path = quote_paths(store, record["request"], quote["nonce"])
    if Path(quote_path).absolute() != expected_path:
        raise AA2Error("Quote belongs to another incident directory")
    message = {"version": VERSION, "request_hash": quote["request_hash"], "nonce": quote["nonce"],
               "quote_sha256": expected_sha256, "controller": quote["controller"],
               "authorization_reference": authorization_reference}
    encoded = json.dumps(message, sort_keys=True).encode()
    if len(encoded) > 4096:
        raise AA2Error("Incident activation message exceeds its bound")
    with directory_fd(message_path.parent) as parent:
        try:
            previous = read_regular(message_path, 4096)
        except FileNotFoundError:
            previous = None
        if previous is not None and previous != encoded:
            raise AA2Error("Incident activation message already differs")
        if previous is None:
            write_regular(parent, message_path.name, encoded)
    return {"request_hash": quote["request_hash"], "nonce": quote["nonce"],
            "message_sha256": raw_hash(encoded), "database_written": False}


async def ensure_runtime(scope, router, context, schema, workspace):
    """Quote the actual initialized controller before any incident model dispatch."""
    from .output_limit_recovery import raw_hash
    from .providers import invocation_identity
    incident = scope.incident
    scope.entry(scope.stage, scope.operation)
    record = incident.require_active()
    request = record["request"]
    owner = controller_identity(incident.store.root)
    if record.get("runtime_binding", {}).get("runtime", {}).get("controller") not in (None, owner):
        incident.reconcile(router.sandbox)
        record = incident.require_active()
    profiles = {stage: await profile_snapshot(router, stage, workspace) for stage in ("implement", "review")}
    current = {"version": VERSION, "controller": owner, "profiles": profiles,
               "config_hash": digest(router.config.load().model_dump(mode="json")),
               "code_identity": request["code_identity"]}
    if scope.role == "correction":
        logical = invocation_identity("implement", context, schema, workspace, router.config.load())
        if logical != request["logical_input"]:
            raise incident.stop("original_logical_input_changed")
    bound = record.get("runtime_binding", {})
    if bound.get("state") == "bound" and bound.get("runtime", {}).get("controller") == owner:
        if bound["runtime"] != current:
            raise incident.stop("bound_runtime_changed")
        return
    group = "implement" if scope.role == "correction" else "review"
    occupied_probe = record["calls"].get("material_probe_" + group)
    if occupied_probe and occupied_probe["capability_key"] != profiles[group]["capability_key"]:
        raise incident.stop("occupied_material_probe_key_changed")
    quote = {"version": VERSION, "run_id": incident.rid, "request_hash": digest(request),
             "nonce": secrets.token_hex(16), "controller": owner, "runtime": current,
             "budget_hash": digest(record["limits"]), "original_logical_input": request["logical_input"],
             "original_schema_hash": request["schema_hash"], "original_source_hash": request["source_hash"],
             "claims_hash": digest(record["calls"]), "issued_monotonic": time.monotonic()}
    raw = json.dumps(quote, sort_keys=True).encode()
    quote_hash = raw_hash(raw)
    incident.store.quote_output_limit_runtime(incident.rid, digest(request), quote, quote_hash)
    quote_path, message_path = quote_paths(incident.store, request, quote["nonce"])
    with directory_fd(quote_path.parent, create=True) as parent:
        write_regular(parent, quote_path.name, raw)
    incident.store.event(incident.rid, "output_limit.runtime_waiting",
        {"quote": str(quote_path), "sha256": quote_hash, "nonce": quote["nonce"], "controller": owner})
    try:
        while True:
            incident.require_active()
            if controller_identity(incident.store.root) != owner:
                raise incident.stop("runtime_controller_changed")
            try:
                message_raw = read_regular(message_path, 4096)
            except FileNotFoundError:
                await asyncio.sleep(0.2)
                continue
            message = json.loads(message_raw)
            if (set(message) != {"version", "request_hash", "nonce", "quote_sha256", "controller", "authorization_reference"}
                    or message["version"] != VERSION or message["request_hash"] != digest(request)
                    or message["nonce"] != quote["nonce"] or message["quote_sha256"] != quote_hash
                    or message["controller"] != owner or not isinstance(message["authorization_reference"], str)
                    or not message["authorization_reference"].strip()):
                raise incident.stop("runtime_activation_message_changed")
            again = {stage: await profile_snapshot(router, stage, workspace) for stage in ("implement", "review")}
            if (again != profiles or digest(router.config.load().model_dump(mode="json")) != current["config_hash"]):
                raise incident.stop("runtime_changed_before_activation")
            incident.store.bind_output_limit_runtime(incident.rid, digest(request), quote["nonce"], quote_hash,
                                                     raw_hash(message_raw), owner)
            return
    except BaseException:
        incident.store.revoke_output_limit_quote(incident.rid, digest(request), quote["nonce"])
        raise


def require_binding(incident):
    record = incident.require_active()
    bound = record.get("runtime_binding", {})
    if bound.get("state") != "bound" or bound["runtime"]["controller"] != controller_identity(incident.store.root):
        raise incident.stop("runtime_not_bound_to_live_controller")
    quote = record.get("runtime_quotes", {}).get(bound["nonce"], {})
    if not quote.get("consumed") or quote.get("revoked"):
        raise incident.stop("runtime_quote_not_consumed")
    return bound


async def prepare_launch(scope, router, stage, context, schema, workspace, eff, argv, env, wire_input, material,
                         reads, readonly_overlays, cache_invocation):
    """Bind actual prepared bytes, not just profile names or mutable source paths."""
    from .models import strict_schema
    from .providers import invocation_identity
    from .workspace import file_digest, tree_hash
    bound = require_binding(scope.incident)
    group = "implement" if scope.role in {"correction", "material_probe_implement"} else "review"
    actual = await profile_snapshot(router, group, workspace)
    if actual != bound["runtime"]["profiles"][group] or eff["fingerprint"] != actual["fingerprint"]:
        raise scope.incident.stop("runtime_profile_changed_before_claim")
    native_root = Path(cache_invocation["native_projection_root"])
    projections = {name: file_digest(native_root / name) for name in actual["native_inputs"]}
    if projections != actual["native_inputs"]:
        raise scope.incident.stop("native_projection_changed_before_claim")
    if eff["environment"] != env or digest(env) != actual["environment_hash"]:
        raise scope.incident.stop("actual_spawn_environment_changed")
    if digest(wire_input) != material["input"]["hash"]:
        raise scope.incident.stop("actual_wire_input_changed")
    files = {str(path): file_digest(path) for path in reads}
    for row in material["materials"]:
        if files.get(row["location"]) != row["hash"]:
            raise scope.incident.stop("material_bytes_changed_after_bundle")
    schema_files = [path for path in reads if path.name == "schema.json"]
    prompt_files = [path for path in reads if path.name == "prompt.txt"]
    if (len(schema_files) != 1 or len(prompt_files) != 1
            or digest(json.loads(schema_files[0].read_bytes())) != material["schema_hash"]
            or file_digest(prompt_files[0]) != material["prompt_hash"]):
        raise scope.incident.stop("wire_prompt_or_schema_changed")
    projected = {str(destination): {str(p.relative_to(source)): file_digest(p)
                    for p in sorted(source.rglob("*")) if p.is_file()}
                 for source, destination in readonly_overlays}
    for source, destination in readonly_overlays:
        expected = {str(Path(name).relative_to(destination)): value for name, value in files.items()
                    if Path(name).is_relative_to(destination)}
        if projected[str(destination)] != expected:
            raise scope.incident.stop("material_projection_bytes_changed")
    view_root = Path(cache_invocation["workspace_view_root"])
    if prepared_tree(view_root) != cache_invocation["workspace_view_manifest"]:
        raise scope.incident.stop("public_workspace_projection_changed")
    projection_manifests = {str(source): prepared_tree(source) for source, _ in readonly_overlays}
    if projection_manifests != cache_invocation["artifact_projection_manifests"]:
        raise scope.incident.stop("artifact_projection_set_changed")
    return {"version": VERSION, "runtime_nonce": bound["nonce"], "runtime_hash": digest(bound["runtime"]),
            "controller": bound["runtime"]["controller"], "profile": actual,
            "role": scope.role, "stage": stage, "operation": scope.operation,
            "logical_input": invocation_identity(stage, context, schema, workspace, router.config.load()),
            "execution_input": invocation_identity(stage, context, schema, workspace, router.config.load(),
                                                    execution=router.execution_identity(stage)),
            "schema_hash": digest(strict_schema(schema)), "source_hash": tree_hash(workspace),
            "argv_hash": digest(argv), "spawn_environment_hash": digest(env), "wire_input_hash": digest(wire_input),
            "material_hash": digest(material), "workspace_view_root": str(view_root),
            "workspace_view_manifest": cache_invocation["workspace_view_manifest"],
            "artifact_projection_manifests": projection_manifests,
            "read_files": files, "readonly_projection_hashes": projected,
            "projection_files": {str(p): file_digest(p) for source, _ in readonly_overlays
                                 for p in sorted(source.rglob("*")) if p.is_file()},
            "native_projection_root": str(native_root), "native_projection_hashes": projections,
            "native_input_patterns": list(router.transports[eff["kind"]].auth_patterns)}


def verify_launch(incident, launch):
    bound = require_binding(incident)
    if (not isinstance(launch, dict) or launch.get("version") != VERSION
            or launch.get("runtime_nonce") != bound["nonce"]
            or launch.get("runtime_hash") != digest(bound["runtime"])
            or launch.get("controller") != bound["runtime"]["controller"]):
        raise incident.stop("launch_receipt_runtime_changed")
    group = "implement" if launch.get("role") in {"correction", "material_probe_implement"} else "review"
    if launch.get("profile") != bound["runtime"]["profiles"][group]:
        raise incident.stop("launch_receipt_profile_changed")


def verify_historical_launch(record, role, claim):
    """A saved receipt remains bound to its own consumed quote, never a new key."""
    launch = claim.get("launch", {})
    entry = record.get("runtime_quotes", {}).get(launch.get("runtime_nonce"), {})
    runtime = entry.get("quote", {}).get("runtime", {})
    group = "implement" if role in {"correction", "material_probe_implement"} else "review"
    if (not entry.get("consumed") or entry.get("revoked") or launch.get("role") != role
            or launch.get("runtime_hash") != digest(runtime)
            or launch.get("controller") != runtime.get("controller")
            or launch.get("profile") != runtime.get("profiles", {}).get(group)
            or launch.get("execution_input") != claim.get("input_identity")
            or launch.get("operation") != claim.get("operation")):
        raise AA2Error("Historical launch no longer matches its consumed runtime quote")
    if role in PROBE_ROLES:
        key = claim.get("capability_key")
        if (not key or key != launch["profile"]["capability_key"]
                or key != claim.get("challenge", {}).get("capability_key")
                or claim.get("operation") != "material-probe/" + key):
            raise AA2Error("Historical material proof capability key changed")


def verify_prepared_launch(incident, launch, argv, env, wire_input, cache_invocation):
    from .workspace import file_digest
    verify_launch(incident, launch)
    native_root = Path(cache_invocation["native_projection_root"])
    actual_native = {path.relative_to(native_root).as_posix(): file_digest(path)
                     for pattern in launch["native_input_patterns"] for path in native_root.glob(pattern) if path.is_file()}
    if (digest(argv) != launch["argv_hash"] or digest(env) != launch["spawn_environment_hash"]
            or digest(wire_input) != launch["wire_input_hash"]
            or str(native_root) != launch["native_projection_root"]
            or actual_native != launch["native_projection_hashes"]
            or prepared_tree(Path(launch["workspace_view_root"])) != launch["workspace_view_manifest"]
            or any(prepared_tree(Path(root)) != manifest for root, manifest in launch["artifact_projection_manifests"].items())
            or any(file_digest(Path(name)) != value for name, value in {
                **launch["read_files"], **launch["projection_files"]}.items())):
        raise incident.stop("prepared_launch_bytes_changed")


class ManagedLaunch:
    """Last validation and unique claim before model supervisor/CLI creation."""
    def __init__(self, scope, router, attempt, input_identity, launch, argv, env, wire_input,
                 semantic_workspace, cache_invocation, writable, reads, overlays, hidden, readonly_overlays):
        self.scope, self.router, self.attempt, self.input_identity = scope, router, attempt, input_identity
        self.launch, self.argv, self.env, self.wire_input = launch, argv, env, wire_input
        self.workspace, self.cache = semantic_workspace, cache_invocation
        self.writable, self.reads, self.overlays = writable, reads, overlays
        self.hidden, self.readonly_overlays = hidden, readonly_overlays
        self.claimed = False

    def claim(self, command, cwd, child_env, process_record):
        from .workspace import file_digest, tree_hash
        incident = self.scope.incident
        if self.claimed:
            raise incident.stop("managed_launch_claim_repeated")
        verify_prepared_launch(incident, self.launch, self.argv, self.env, self.wire_input, self.cache)
        profile = self.launch["profile"]
        group = "implement" if self.scope.role in {"correction", "material_probe_implement"} else "review"
        actual_eff = self.router.effective(group, profile["provider"])
        executable = shutil.which(self.argv[0], path=self.env.get("PATH", os.defpath))
        native_argv = [executable, *self.argv[1:]]
        sandbox_binary = Path(self.router.sandbox.binary)
        if (not executable or str(Path(executable).resolve()) != profile["binary"]
                or file_digest(Path(executable)) != profile["binary_hash"]
                or actual_eff["fingerprint"] != profile["fingerprint"]
                or native_inputs(self.router, actual_eff) != profile["native_inputs"]
                or self.router.execution_identity(group) != profile["execution_component"]
                or child_env != {**self.env, "GIT_OPTIONAL_LOCKS": "0"}
                or str(cwd) != self.launch["workspace_view_root"]
                or tree_hash(self.workspace) != self.launch["source_hash"]
                or (file_digest(sandbox_binary) if sandbox_binary.is_file() else None) != profile["sandbox_binary_hash"]
                or self.router.sandbox.test_unconfined != profile["test_unconfined"]):
            raise incident.stop("managed_child_runtime_changed")
        expected_command = self.router.sandbox.command(native_argv, cwd, self.writable, self.reads,
            self.overlays, self.hidden, None, self.readonly_overlays)
        if command != expected_command or Path(process_record).parent != incident.store.root / "processes":
            raise incident.stop("managed_command_or_record_changed")
        verify_prepared_launch(incident, self.launch, self.argv, self.env, self.wire_input, self.cache)
        self.launch.update(managed_command_hash=digest(command), managed_environment_hash=digest(child_env),
                           resolved_native_argv_hash=digest(native_argv), process_record=str(process_record))
        self.claimed = True
        self.scope.claim(self.attempt, self.input_identity, self.launch)
