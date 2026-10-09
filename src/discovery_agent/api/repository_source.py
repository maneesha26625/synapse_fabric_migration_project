"""Synapse definitions from a Git repository or a ZIP of one, for one environment.

A Synapse workspace that is Git-integrated keeps its definitions as JSON files,
one folder per kind (``pipeline/``, ``linkedService/``, ``notebook/`` ...),
under a root folder of the repository. The same tree, downloaded as a ZIP,
is the workspace export this accepts.

The collaboration branch holds the values of the environment the workspace
was built in, usually development: its linked services name dev servers.
Synapse CI/CD swaps in another environment's values at deployment from an
ARM parameters file (``TemplateParametersForWorkspace.json``, or a copy per
environment). Given such a file, the matching linked-service values are
applied to a working copy of the tree before discovery reads it, so the
inventory and the migrated Fabric connections both carry that environment's
values. The repository itself, and the uploaded ZIP, are never changed.

Parameter names follow Synapse's default template-parameters definition:
``<linked service>_connectionString`` and
``<linked service>_properties_typeProperties_<property>``. A name that does
not resolve to a property that exists is reported, not guessed at.
"""

from __future__ import annotations

import io
import json
import shutil
import stat
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from discovery_agent.source_strategy import P0Artifact

#: The folders a Synapse repository keeps its definitions in.
SYNAPSE_FOLDERS = (
    "pipeline", "linkedService", "dataset", "notebook", "sqlscript", "sparkJobDefinition", "trigger",
    "integrationRuntime", "dataflow", "credential", "managedVirtualNetwork", "kqlscript",
)
#: Where uploads and per-environment working copies live; gitignored with the clones under input/.
UPLOAD_ROOT = Path("input") / "uploads"
WORK_ROOT = Path("input") / "environments"
MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_UNZIPPED_BYTES = 1024 * 1024 * 1024
MAX_ZIP_FILES = 20000
MAX_PARAMETERS_BYTES = 1024 * 1024
#: How deep under the repository or ZIP root the Synapse folders are looked for.
_ROOT_SEARCH_DEPTH = 3
_ENVIRONMENT_MAX = 40


class RepositoryError(ValueError):
    """The repository, the ZIP or the parameters file cannot be used; the message says why."""


@dataclass
class RepositorySpec:
    """Where one connection's definitions come from, and the environment applied to them."""

    kind: str  # "git" | "zip"
    label: str  # the repository URL, or the ZIP's file name
    environment: str
    root_folder: str  # relative to the repository or ZIP root, "/"-separated; "" for the root itself
    path: Path  # the working copy discovery reads
    ref: Optional[str] = None
    commit: Optional[str] = None
    parameters_name: Optional[str] = None
    applied: List[str] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)

    @property
    def artifact_count(self) -> int:
        return sum(self.counts.values())

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "label": self.label, "environment": self.environment, "rootFolder": self.root_folder,
            "ref": self.ref, "commit": self.commit, "parametersName": self.parameters_name,
            "applied": list(self.applied), "unmatched": list(self.unmatched), "counts": dict(self.counts),
            "artifacts": self.artifact_count,
        }


# ---- ZIP ------------------------------------------------------------------------------------


def _member_path(name: str) -> Optional[PurePosixPath]:
    """A ZIP member's path, or None when it would land outside the extraction folder."""
    clean = name.replace("\\", "/")
    if clean.startswith("/") or (len(clean) > 1 and clean[1] == ":"):
        return None
    path = PurePosixPath(clean)
    if any(part in ("..", "") for part in path.parts):
        return None
    return path


def extract_zip(data: bytes, upload_id: Optional[str] = None, root: Optional[Path] = None) -> Tuple[str, Path]:
    """Extract an uploaded ZIP safely. Returns (upload id, folder).

    Refused: anything over the size or file-count limits, and any member that would land
    outside the folder (an absolute path or ``..``). Symbolic links are skipped, not followed.
    """
    if len(data) > MAX_ZIP_BYTES:
        raise RepositoryError(f"The ZIP is larger than {MAX_ZIP_BYTES // (1024 * 1024)} MB.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise RepositoryError("The file is not a valid ZIP archive.") from exc
    members = [m for m in archive.infolist() if not m.is_dir()]
    if len(members) > MAX_ZIP_FILES:
        raise RepositoryError(f"The ZIP holds more than {MAX_ZIP_FILES:,} files.")
    if sum(m.file_size for m in members) > MAX_UNZIPPED_BYTES:
        raise RepositoryError(f"The ZIP expands to more than {MAX_UNZIPPED_BYTES // (1024 * 1024)} MB.")
    upload_id = upload_id or uuid.uuid4().hex
    dest = (root or UPLOAD_ROOT) / upload_id
    dest.mkdir(parents=True, exist_ok=False)
    for member in members:
        if stat.S_ISLNK(member.external_attr >> 16):
            continue
        relative = _member_path(member.filename)
        if relative is None:
            shutil.rmtree(dest, ignore_errors=True)
            raise RepositoryError(f"The ZIP has a file outside its own folder ({member.filename!r}), so it was refused.")
        target = dest.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
    return upload_id, dest


def upload_folder(upload_id: str, root: Optional[Path] = None) -> Path:
    """The folder an earlier upload was extracted to."""
    if not upload_id.isalnum() or len(upload_id) != 32:
        raise RepositoryError("The upload is not known. Upload the ZIP again.")
    folder = (root or UPLOAD_ROOT) / upload_id
    if not folder.is_dir():
        raise RepositoryError("The upload is not known. Upload the ZIP again.")
    return folder


# ---- the Synapse root folder ----------------------------------------------------------------


def _synapse_folders(path: Path) -> List[str]:
    return [f for f in SYNAPSE_FOLDERS if (path / f).is_dir()]


def find_root(base: Path, hint: str = "") -> Tuple[Path, str]:
    """The folder holding the Synapse definitions: the one named, or the shallowest that has them.

    Returns (folder, its path relative to ``base``). A repository downloaded as a ZIP usually
    wraps everything in one top folder, and Synapse lets the root folder be any sub-folder,
    so both are looked through.
    """
    base = base.resolve()
    if hint.strip().strip("/\\"):
        relative = _member_path(hint.strip().strip("/\\"))
        if relative is None:
            raise RepositoryError("The root folder must be a folder inside the repository.")
        folder = base.joinpath(*relative.parts)
        if not folder.is_dir():
            raise RepositoryError(f"There is no folder {hint.strip()!r} in the repository.")
        if not _synapse_folders(folder):
            raise RepositoryError(f"The folder {hint.strip()!r} holds no Synapse folders such as pipeline/ or linkedService/.")
        return folder, relative.as_posix()
    level = [base]
    for _ in range(_ROOT_SEARCH_DEPTH + 1):
        found = sorted((p for p in level if _synapse_folders(p)), key=lambda p: (-len(_synapse_folders(p)), str(p)))
        if found:
            relative = found[0].relative_to(base).as_posix()
            return found[0], "" if relative == "." else relative
        level = sorted(c for p in level for c in p.iterdir() if c.is_dir() and not c.name.startswith("."))
    raise RepositoryError("No Synapse definitions were found: no pipeline/, linkedService/, notebook/ or similar folder "
                          "within three levels of the top. Name the root folder the workspace uses.")


def counts(root: Path) -> Dict[str, int]:
    """JSON files per Synapse folder."""
    out: Dict[str, int] = {}
    for folder in SYNAPSE_FOLDERS:
        n = len(list((root / folder).glob("*.json"))) if (root / folder).is_dir() else 0
        if n:
            out[folder] = n
    return out


# ---- environment parameters -----------------------------------------------------------------


def parse_parameters(text: str) -> Dict[str, Any]:
    """Parameter values from an ARM parameters file, or a flat ``{"name": value}`` object.

    Only literal values are kept; a Key Vault reference has no value to apply.
    """
    if len(text.encode("utf-8")) > MAX_PARAMETERS_BYTES:
        raise RepositoryError("The parameters file is larger than 1 MB.")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise RepositoryError("The parameters file is not valid JSON.") from exc
    if not isinstance(doc, dict):
        raise RepositoryError("The parameters file must be a JSON object.")
    raw = doc.get("parameters") if isinstance(doc.get("parameters"), dict) else doc
    out: Dict[str, Any] = {}
    for name, value in raw.items():
        if name.startswith("$") or name == "contentVersion":
            continue
        if isinstance(value, dict):
            if "value" not in value:
                continue  # a Key Vault reference, or metadata
            value = value["value"]
        if isinstance(value, (str, int, float, bool)):
            out[str(name)] = value
    if not out:
        raise RepositoryError("The parameters file holds no parameter values.")
    return out


def _resolve(node: Any, tokens: List[str]) -> Optional[Tuple[Any, str]]:
    """(parent, key) for a property path written with "_" between its parts, matching real keys only.

    Keys may themselves contain "_", so each step tries the longest run of tokens that is a key.
    """
    if not tokens or not isinstance(node, dict):
        return None
    for end in range(len(tokens), 0, -1):
        key = "_".join(tokens[:end])
        if key in node:
            if end == len(tokens):
                return node, key
            found = _resolve(node[key], tokens[end:])
            if found is not None:
                return found
    return None


def apply_parameters(root: Path, values: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Write the environment's values into the linked services under ``root``. Returns (applied, unmatched)."""
    folder = root / "linkedService"
    services: Dict[str, Tuple[Path, dict]] = {}
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("name"):
            services[str(doc["name"])] = (path, doc)
    names = sorted(services, key=len, reverse=True)
    applied: List[str] = []
    unmatched: List[str] = []
    changed: Dict[str, bool] = {}
    for param, value in sorted(values.items()):
        service = next((n for n in names if param.startswith(n + "_")), None)
        if service is None:
            unmatched.append(param)
            continue
        rest = param[len(service) + 1:]
        doc = services[service][1]
        if rest == "connectionString":  # Synapse names it without the path
            type_properties = (doc.get("properties") or {}).get("typeProperties")
            found = (type_properties, "connectionString") if isinstance(type_properties, dict) and "connectionString" in type_properties else None
        else:
            found = _resolve(doc, rest.split("_"))
        if found is None:
            unmatched.append(param)
            continue
        parent, key = found
        current = parent[key]
        if isinstance(current, dict) and current.get("type") == "SecureString" and "value" in current:
            current["value"] = value  # a secure string keeps its wrapper
        elif isinstance(current, (str, int, float, bool)) or current is None:
            parent[key] = value
        else:
            unmatched.append(param)  # an expression or a Key Vault reference: not a literal to replace
            continue
        applied.append(param)
        changed[service] = True
    for service in changed:
        path, doc = services[service]
        path.write_text(json.dumps(doc, indent=4), encoding="utf-8")
    return applied, unmatched


# ---- one environment's working copy ---------------------------------------------------------


def clean_environment(name: str) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise RepositoryError("Choose the environment these definitions are for.")
    if len(text) > _ENVIRONMENT_MAX:
        raise RepositoryError(f"The environment name is longer than {_ENVIRONMENT_MAX} characters.")
    return text


def prepare(spec_root: Path, parameters: Optional[Dict[str, Any]], work_root: Optional[Path] = None) -> Tuple[Path, List[str], List[str]]:
    """Copy the Synapse folder for discovery and apply the environment's parameters to the copy."""
    dest = (work_root or WORK_ROOT) / uuid.uuid4().hex
    shutil.copytree(spec_root, dest, ignore=shutil.ignore_patterns(".git"))
    applied, unmatched = apply_parameters(dest, parameters) if parameters else ([], [])
    return dest, applied, unmatched


def discard(path: Optional[Path], work_root: Optional[Path] = None) -> None:
    """Remove a working copy this module made. Never anything outside ``work_root``."""
    if path is None:
        return
    try:
        if (work_root or WORK_ROOT).resolve() in path.resolve().parents:
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# ---- the definitions, for migration ---------------------------------------------------------

#: Repository folder -> the artifact kind migration looks payloads up by.
_P0_FOLDERS = {
    "pipeline": P0Artifact.PIPELINE, "dataset": P0Artifact.DATASET, "linkedService": P0Artifact.LINKED_SERVICE,
    "notebook": P0Artifact.NOTEBOOK, "sqlscript": P0Artifact.SQL_SCRIPT, "sparkJobDefinition": P0Artifact.SPARK_JOB_DEFINITION,
}


def _documents(folder: Path) -> List[dict]:
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("name"):
            out.append(doc)
    return out


def artifacts(root: Path) -> Dict[P0Artifact, Dict[str, dict]]:
    """Every definition migration converts, by kind and name, as the files hold them."""
    return {kind: {str(d["name"]): d for d in _documents(root / folder)} for folder, kind in _P0_FOLDERS.items()}


def triggers(root: Path) -> List[dict]:
    return _documents(root / "trigger")


def integration_runtimes(root: Path) -> List[dict]:
    return _documents(root / "integrationRuntime")
