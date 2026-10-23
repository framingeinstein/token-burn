"""Attribution for the factory transcript mirror (~/.token-burn/factory-transcripts).

Layout: `{top}/{issue}/{execution}/{session}.jsonl`, or a session chunked into
`{top}/{issue}/{execution}/{session}.parts/NNNNNN.jsonl` (parts of one session;
message.id dedupe handles overlap). `top` is a repo (`factory`, `memory`,
`spine`, ...) or a non-repo test/dev harness folder (`runner-a`..`runner-e`,
`signed-in-run`, `test-runner`). Execution names come in two shapes: new
`wb-<repo>-<verb>-<id>` (`wb-factory-review-xlsg5`) and old
`wb-<verb>-<repo>-<id>` (`wb-impl-factory-ztbwc`).

Rules (controller ruling, 2026-10-22):
- repo: from the execution name (`synkhos/<repo>`, terpsichore ->
  terpsichore-core); else the top folder only when it is a known repo; else None.
- issue: from the session's gitBranch (issue-branch pattern) when present --
  resolved by the caller, which has the messages; else the folder number ONLY
  when the top folder is the repo itself (`folder_issue`); else None.
- actor: `factory:<repo>` when the repo is known (all runners for one repo
  aggregate), else `factory:<top>`. Test-run folders keep `factory:<top>` so
  their records stay identifiable once archived (they're excluded from
  metrics by actor -- `split_test_runs`).
- kind: implement/impl -> impl, review, rework, ci-fix, conflict(-fix) ->
  conflict, else unknown.

Stdlib only; imports nothing from the rest of the project (parse and
usage_records both use it).
"""
import fnmatch
import glob
import os
import re
from pathlib import PurePath

ORG = "synkhos"

# top folders that are repos (the fallback when the execution name doesn't parse);
# a `repo_map` key also counts as known.
DEFAULT_FACTORY_REPOS = ("factory", "memory", "spine", "hub", "lattice", "knitr", "nexus",
                         "harness", "terpsichore", "synkhos")

# non-repo test/dev harness folders: excluded from every metric/total by default
# and reported separately (efficiency `factory_test_runs_usd`). Glob patterns.
DEFAULT_FACTORY_TEST_FOLDERS = ("runner-*", "signed-in-run", "test-runner")

REPO_OVERRIDES = {"terpsichore": "terpsichore-core"}

# verb (as it appears in an execution name) -> usage-record `kind`
VERB_KINDS = {
    "implement": "impl", "impl": "impl",
    "review": "review",
    "rework": "rework",
    "ci-fix": "ci-fix",
    "conflict-fix": "conflict", "conflict": "conflict",
}
_VERBS_LONGEST_FIRST = sorted(VERB_KINDS, key=len, reverse=True)

_EXECUTION_RE = re.compile(r"^wb-(.+)-[A-Za-z0-9]+$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def parse_execution(name):
    """`(repo, verb)` from an execution folder name in either shape, or
    `(None, None)` when it isn't one."""
    m = _EXECUTION_RE.match(name or "")
    if not m:
        return None, None
    middle = m.group(1)
    for verb in _VERBS_LONGEST_FIRST:           # new: wb-<repo>-<verb>-<id>
        if middle.endswith("-" + verb):
            repo = middle[:-len(verb) - 1]
            if _REPO_NAME_RE.match(repo):
                return repo, verb
    for verb in _VERBS_LONGEST_FIRST:           # old: wb-<verb>-<repo>-<id>
        if middle.startswith(verb + "-"):
            repo = middle[len(verb) + 1:]
            if _REPO_NAME_RE.match(repo):
                return repo, verb
    return None, None


def is_test_folder(top, test_folders=None):
    patterns = DEFAULT_FACTORY_TEST_FOLDERS if test_folders is None else test_folders
    return any(fnmatch.fnmatchcase(top, p) for p in patterns)


def resolve_repo(name, repo_map=None, org=ORG):
    """Short repo / runner name -> `org/repo` (`repo_map` overrides; the
    legacy `wb-impl-<repo>` runner name is stripped; terpsichore override)."""
    if repo_map and name in repo_map:
        return repo_map[name]
    if name.startswith("wb-impl-"):
        name = name[len("wb-impl-"):]
    return f"{org}/{REPO_OVERRIDES.get(name, name)}"


def session_id(rel_parts):
    """`<session>` of `.../<session>.jsonl` or `.../<session>.parts/NNNNNN.jsonl`."""
    p = PurePath(*rel_parts)
    if p.parent.name.endswith(".parts"):
        return p.parent.name[:-len(".parts")]
    return p.stem


def attribute(rel_parts, repo_map=None, known_repos=None, test_folders=None):
    """Path-derived attribution for one mirror file (`rel_parts`: its path
    relative to the mirror root, as a tuple). Returns {top, repo_name, repo,
    actor, kind, folder_issue, is_test, session}; the branch-derived issue is
    the caller's (it has the messages)."""
    top = rel_parts[0]
    execution = rel_parts[2] if len(rel_parts) >= 4 else None
    exec_repo, verb = parse_execution(execution)
    known = set(DEFAULT_FACTORY_REPOS if known_repos is None else known_repos) | set(repo_map or ())
    repo_name = exec_repo or (top if top in known else None)
    is_test = is_test_folder(top, test_folders)
    folder = rel_parts[1] if len(rel_parts) >= 3 else ""
    folder_issue = (int(folder) if folder.isdigit() and int(folder) >= 1
                    and repo_name is not None and top == repo_name else None)
    return {
        "top": top,
        "repo_name": repo_name,
        "repo": resolve_repo(repo_name, repo_map) if repo_name else None,
        "actor": f"factory:{top if is_test or not repo_name else repo_name}",
        "kind": VERB_KINDS.get(verb, "unknown"),
        "folder_issue": folder_issue,
        "is_test": is_test,
        "session": session_id(rel_parts),
    }


def is_test_actor(actor, test_folders=None):
    return bool(actor) and actor.startswith("factory:") and is_test_folder(
        actor[len("factory:"):], test_folders)


def split_test_runs(records, test_folders=None):
    """`(production, test_runs)`: usage records whose actor is a test-run
    folder (`factory:runner-a`, ...) are split off so they're excluded from
    metrics and totals but can still be reported on their own."""
    prod, test = [], []
    for r in records:
        (test if is_test_actor(r.get("actor"), test_folders) else prod).append(r)
    return prod, test


def expand_parts(files):
    """Sorted `files` plus every sibling part of any chunked session among
    them, so a session whose older parts fall outside an mtime window is
    still read whole (dedupe and call numbering span all of its parts)."""
    files = list(files)
    out = set(files)
    for d in {os.path.dirname(f) for f in files if os.path.dirname(f).endswith(".parts")}:
        out.update(glob.glob(os.path.join(glob.escape(d), "*.jsonl")))
    return sorted(out)
