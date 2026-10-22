"""Regression tests for native Final Evaluation's lightweight Git freshness."""
import asyncio
import json
import sqlite3
import subprocess

import pytest

from qiqi_delegate.final_eval_git_state import review_scope
from qiqi_delegate.final_eval_manifest import manifest_digest
from test_final_evaluation import report, setup_graph


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
    )


@pytest.mark.parametrize("index_flag", ["--skip-worktree", "--assume-unchanged"])
def test_flagged_tracked_edits_invalidate_final_pass(tmp_path, monkeypatch, index_flag):
    """A Git diff may hide edits but Final PASS must not remain current."""
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    path = backend / "orders.py"
    _git(backend, "add", "--", "orders.py")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track evaluator source")
    _git(backend, "update-index", index_flag, "--", "orders.py")

    async def delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "flagged-native")
        scope = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(scope))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("flagged-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", body, 153),
            )
        return {"state": "settled", "turn_id": "flagged-native",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", delegate)
    revision = graph_rt.get_graph(gid)["revision"]
    evaluated = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert evaluated["status"] == "passed"
    before = manifest_digest(review_scope(runtime.repos()))

    path.write_text("def total(n):\n    return n * 999\n", encoding="utf-8")
    after = manifest_digest(review_scope(runtime.repos()))
    assert before != after, "Git index flags must not hide tracked working-tree edits"
    assert coordinator.read(gid, evaluated["evaluation_id"])["effective_status"] == "stale"
    with pytest.raises((RuntimeError, ValueError), match="PASS"):
        coordinator.finalize(gid, evaluated["evaluation_id"], revision)


def test_untracked_content_cannot_cross_file_record_boundaries(tmp_path):
    """Formerly equal byte streams must yield different Git fingerprints."""
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    a = backend / "a"
    b = backend / "b"
    a.write_bytes(b"")
    b.write_bytes(b"")
    # Old encoding was: path length + path + file mode + raw file bytes.
    b_header = (1).to_bytes(8, "big") + b"b" + b.stat().st_mode.to_bytes(8, "big")
    a.write_bytes(b"")
    b.write_bytes(b_header)
    first = manifest_digest(review_scope(runtime.repos()))
    a.write_bytes(b_header)
    b.write_bytes(b"")
    second = manifest_digest(review_scope(runtime.repos()))
    assert first != second


def _add_local_submodule(tmp_path, backend):
    dependency = tmp_path / "dependency-source"
    dependency.mkdir()
    _git(dependency, "init")
    (dependency / "library.py").write_text("CONTRACT = 1\n", encoding="utf-8")
    _git(dependency, "add", "library.py")
    _git(dependency, "-c", "user.name=Test", "-c",
         "user.email=test@example.com", "commit", "-m", "Initial library")
    _git(backend, "-c", "protocol.file.allow=always", "submodule",
         "add", str(dependency), "deps/shared")
    return backend / "deps/shared"


def test_dirty_submodule_edits_have_distinct_fingerprints(tmp_path):
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    subroot = _add_local_submodule(tmp_path, backend)
    source = subroot / "library.py"

    clean = manifest_digest(review_scope(runtime.repos()))
    source.write_text("CONTRACT = 2\n", encoding="utf-8")
    first_dirty = manifest_digest(review_scope(runtime.repos()))
    source.write_text("CONTRACT = 3\n", encoding="utf-8")
    second_dirty = manifest_digest(review_scope(runtime.repos()))

    assert clean != first_dirty
    assert first_dirty != second_dirty  # Parent gitlink HEAD never changed.
    source.write_text("CONTRACT = 2\n", encoding="utf-8")
    assert manifest_digest(review_scope(runtime.repos())) == first_dirty


def test_dirty_submodule_edit_invalidates_final_pass(tmp_path, monkeypatch):
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    subroot = _add_local_submodule(tmp_path, backend)
    source = subroot / "library.py"
    source.write_text("CONTRACT = 2\n", encoding="utf-8")

    async def delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "submodule-native")
        scope = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(scope))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("submodule-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", body, 154),
            )
        return {"state": "settled", "turn_id": "submodule-native",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", delegate)
    revision = graph_rt.get_graph(gid)["revision"]
    evaluated = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert evaluated["status"] == "passed"
    assert evaluated["is_current"]

    source.write_text("CONTRACT = 3\n", encoding="utf-8")
    assert coordinator.read(gid, evaluated["evaluation_id"])["effective_status"] == "stale"
    with pytest.raises((RuntimeError, ValueError), match="PASS"):
        coordinator.finalize(gid, evaluated["evaluation_id"], revision)


def test_uninitialized_submodule_fails_freshness_closed(tmp_path):
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    subroot = _add_local_submodule(tmp_path, backend)
    import shutil
    shutil.rmtree(subroot)
    with pytest.raises((RuntimeError, ValueError), match="Git root|repository root"):
        review_scope(runtime.repos())


def test_clean_filter_is_rejected_before_native_dispatch(tmp_path, monkeypatch):
    """Git diff may normalize changed raw source to identical index bytes."""
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    source = backend / "filtered.py"
    source.write_text("X\n", encoding="utf-8")
    (backend / ".gitattributes").write_text(
        "filtered.py filter=neutral\n", encoding="utf-8",
    )
    _git(backend, "config", "filter.neutral.clean", "sed s/./X/g")
    _git(backend, "add", "--", "filtered.py", ".gitattributes")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track clean-filtered source")
    source.write_text("Y\n", encoding="utf-8")

    invisible = subprocess.run(
        ["git", "-C", str(backend), "diff", "--", "filtered.py"],
        check=True, capture_output=True,
    )
    assert invisible.stdout == b"", "precondition: Git diff hides the raw edit"

    launched = []
    async def forbidden_delegate(**kwargs):
        launched.append(kwargs)
        raise AssertionError("native evaluator must not launch with clean filter")

    monkeypatch.setattr(runtime, "delegate", forbidden_delegate)
    with pytest.raises(ValueError, match="Git clean filter"):
        review_scope(runtime.repos())
    with pytest.raises(ValueError, match="Git clean filter"):
        asyncio.run(coordinator.start(
            gid, "codex-evaluator", graph_rt.get_graph(gid)["revision"],
        ))
    assert not launched
    assert coordinator.store.latest(gid) is None


def test_untracked_embedded_git_repository_rejected_before_dispatch(
    tmp_path, monkeypatch,
):
    """Git collapses an embedded untracked repo to a single directory entry."""
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    nested = backend / "nested"
    nested.mkdir()
    _git(nested, "init")
    (nested / "source.py").write_text("SOURCE = 1\n", encoding="utf-8")

    listed = subprocess.run(
        ["git", "-C", str(backend), "ls-files", "--others",
         "--exclude-standard", "-z"],
        check=True, capture_output=True,
    )
    assert b"nested/\0" in listed.stdout, "precondition: Git hides nested files"

    launched = []
    async def forbidden_delegate(**kwargs):
        launched.append(kwargs)
        raise AssertionError("native evaluator must not launch with hidden source")

    monkeypatch.setattr(runtime, "delegate", forbidden_delegate)
    with pytest.raises(ValueError, match="non-file Git entry"):
        review_scope(runtime.repos())
    with pytest.raises(ValueError, match="non-file Git entry"):
        asyncio.run(coordinator.start(
            gid, "codex-evaluator", graph_rt.get_graph(gid)["revision"],
        ))
    assert not launched
    assert coordinator.store.latest(gid) is None


@pytest.mark.parametrize("attribute", ["text", "eol=lf", "ident", "crlf"])
def test_git_checkin_conversions_are_rejected(tmp_path, monkeypatch, attribute):
    """Git can canonicalize distinct working-tree bytes to the same diff."""
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    path = backend / "run.sh"
    path.write_bytes(b"#!/bin/sh\necho ok\n")
    _git(backend, "add", "run.sh")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track script")
    (backend / ".gitattributes").write_text(
        "run.sh " + attribute + "\n", encoding="utf-8",
    )
    if attribute == "text":
        path.write_bytes(b"#!/bin/sh\r\necho ok\r\n")
        invisible = subprocess.run(
            ["git", "-C", str(backend), "diff", "--", "run.sh"],
            check=True, capture_output=True,
        )
        assert invisible.stdout == b"", "Git text conversion should hide CRLF edit"

    launched = []
    async def forbidden_delegate(**kwargs):
        launched.append(kwargs)
        raise AssertionError("native evaluator must not launch")

    monkeypatch.setattr(runtime, "delegate", forbidden_delegate)
    with pytest.raises(ValueError, match="Git clean filter/check-in conversion"):
        review_scope(runtime.repos())
    with pytest.raises(ValueError, match="Git clean filter/check-in conversion"):
        asyncio.run(coordinator.start(
            gid, "codex-evaluator", graph_rt.get_graph(gid)["revision"],
        ))
    assert not launched
    assert coordinator.store.latest(gid) is None


def test_working_tree_encoding_attribute_rejected(tmp_path):
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    source = backend / "encoded.txt"
    source.write_bytes("example\n".encode("utf-16"))
    _git(backend, "add", "encoded.txt")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track encoded source")
    (backend / ".gitattributes").write_text(
        "encoded.txt working-tree-encoding=UTF-16\n", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Git clean filter/check-in conversion"):
        review_scope(runtime.repos())


def test_disabled_git_filemode_rejected_before_dispatch(tmp_path, monkeypatch):
    """Git diff ignores tracked executable-bit changes with core.fileMode=false."""
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    source = backend / "orders.py"
    _git(backend, "add", "orders.py")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track executable test")
    before = source.stat().st_mode
    _git(backend, "config", "core.fileMode", "false")
    source.chmod(before ^ 0o111)
    assert (source.stat().st_mode ^ before) & 0o111
    invisible = subprocess.run(
        ["git", "-C", str(backend), "diff", "--", "orders.py"],
        check=True, capture_output=True,
    )
    assert invisible.stdout == b"", "Git config should hide mode change"

    launched = []
    async def forbidden_delegate(**kwargs):
        launched.append(kwargs)
        raise AssertionError("native evaluator must not launch")

    monkeypatch.setattr(runtime, "delegate", forbidden_delegate)
    with pytest.raises(ValueError, match="core.fileMode=false"):
        review_scope(runtime.repos())
    with pytest.raises(ValueError, match="core.fileMode=false"):
        asyncio.run(coordinator.start(
            gid, "codex-evaluator", graph_rt.get_graph(gid)["revision"],
        ))
    assert not launched
    assert coordinator.store.latest(gid) is None


@pytest.mark.parametrize("autocrlf", ["true", "input"])
def test_global_auto_crlf_rejected(tmp_path, autocrlf):
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    _git(backend, "config", "core.autocrlf", autocrlf)
    with pytest.raises(ValueError, match="core.autocrlf"):
        review_scope(runtime.repos())


def test_textconv_must_not_hide_stale_native_final_pass(tmp_path, monkeypatch):
    """A custom diff textconv renders two different raw versions identically."""
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path, multi=False)
    backend = runtime.repos()["backend"]
    source = backend / "orders.py"
    attrs = backend / ".gitattributes"
    attrs.write_text("orders.py diff=constant\n", encoding="utf-8")
    _git(backend, "add", "orders.py", ".gitattributes")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Enable source diff textconv")
    _git(backend, "config", "diff.constant.textconv", "sed s/./X/g")

    async def delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "textconv-native")
        captured = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(captured))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("textconv-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", body, 200),
            )
        return {"state": "settled", "turn_id": "textconv-native",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", delegate)
    rev = graph_rt.get_graph(gid)["revision"]
    evaluated = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert evaluated["status"] == "passed"
    before = manifest_digest(review_scope(runtime.repos()))
    source.write_text("def total(n):\n    return n * 3\n", encoding="utf-8")
    hidden = subprocess.run(
        ["git", "-C", str(backend), "diff", "--no-ext-diff", "--binary", "--",
         "orders.py"], check=True, capture_output=True,
    )
    assert hidden.stdout == b"", "Git textconv precondition not reproduced"
    assert before != manifest_digest(review_scope(runtime.repos()))
    assert coordinator.read(gid, evaluated["evaluation_id"])["effective_status"] == "stale"
    with pytest.raises((RuntimeError, ValueError), match="PASS"):
        coordinator.finalize(gid, evaluated["evaluation_id"], rev)


@pytest.mark.parametrize("staged", [False, True])
def test_rename_source_is_valid_git_deletion_evidence(tmp_path, staged):
    """No-renames tombstones include old path under staged or unstaged moves."""
    runtime, _requests, _graph_rt, coordinator, _gid = setup_graph(tmp_path, multi=False)
    backend = runtime.repos()["backend"]
    old = backend / "orders.py"
    new = backend / "orders_v2.py"
    _git(backend, "add", "orders.py")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track original API")
    if staged:
        _git(backend, "mv", "orders.py", "orders_v2.py")
    else:
        old.rename(new)
        _git(backend, "add", "-N", "orders_v2.py")
    # Git can classify the move as R instead of D when renames are enabled.
    renamed = subprocess.run(
        ["git", "-C", str(backend), "diff", "-M", "--name-status", "HEAD", "--"],
        check=True, capture_output=True,
    )
    assert b"R100" in renamed.stdout, "Expected Git rename to be detected"
    manifest = review_scope(runtime.repos())
    assert "orders.py" in manifest["backend"]["deleted_paths"]
    report_payload = report(manifest)
    report_payload["requirement_results"][0]["evidence"] = [{
        "kind": "deleted", "repository": "backend",
        "path": "orders.py", "locator": "git:tracked-rename-source",
    }]
    assert coordinator.store.validate_report(report_payload, manifest, {"R1"})[
        "verdict"
    ] == "pass"


def test_bad_fsmonitor_hook_cannot_hide_native_final_source_edits(
    tmp_path, monkeypatch,
):
    """A lying FSMonitor reports tracked source clean despite changed bytes."""
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path, multi=False)
    backend = runtime.repos()["backend"]
    source = backend / "orders.py"
    _git(backend, "add", "orders.py")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track evaluator source")

    hook = tmp_path / "fsmonitor-always-clean.sh"
    hook.write_text("#!/bin/sh\nprintf 'token\\0'\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(backend, "config", "core.fsmonitor", str(hook))

    async def delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "fsmon-native")
        captured = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(captured))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("fsmon-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", body, 201),
            )
        return {"state": "settled", "turn_id": "fsmon-native",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", delegate)
    rev = graph_rt.get_graph(gid)["revision"]
    evaluated = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert evaluated["status"] == "passed"
    before = manifest_digest(review_scope(runtime.repos()))

    # Reactivate FSMonitor's "clean" index bits after the safe inspection,
    # then mutate tracked source without notifying the monitor hook.
    subprocess.run(["git", "-C", str(backend), "status", "--short"],
                   check=True, capture_output=True)
    tagged = subprocess.run(
        ["git", "-C", str(backend), "ls-files", "-f", "-z"],
        check=True, capture_output=True,
    ).stdout
    assert b"h orders.py\0" in tagged, "FSMonitor precondition was not activated"

    source.write_text("def total(n):\n    return n * 999\n", encoding="utf-8")
    hidden_diff = subprocess.run(
        ["git", "-C", str(backend), "diff", "--no-ext-diff", "--no-textconv",
         "--binary", "--", "orders.py"],
        check=True, capture_output=True,
    )
    assert hidden_diff.stdout == b"", "FSMonitor must reproduce hidden raw edits"
    after = manifest_digest(review_scope(runtime.repos()))
    assert before != after, "Fingerprint must ignore dishonest FSMonitor status"
    assert coordinator.read(gid, evaluated["evaluation_id"])["effective_status"] == "stale"
    with pytest.raises((RuntimeError, ValueError), match="PASS"):
        coordinator.finalize(gid, evaluated["evaluation_id"], rev)
    configured = subprocess.run(
        ["git", "-C", str(backend), "config", "--get", "core.fsmonitor"],
        check=True, capture_output=True,
    ).stdout.strip()
    assert configured == str(hook).encode(), "Fingerprint must not rewrite Git config"


def test_missing_assume_unchanged_file_produces_deletion_evidence(tmp_path):
    """Git hides this deletion, but native v3 can still cite a real HEAD tombstone."""
    runtime, _requests, _graph_rt, coordinator, _gid = setup_graph(
        tmp_path, multi=False,
    )
    backend = runtime.repos()["backend"]
    source = backend / "orders.py"
    _git(backend, "add", "orders.py")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track source before deletion")
    _git(backend, "update-index", "--assume-unchanged", "--", "orders.py")

    before = manifest_digest(review_scope(runtime.repos()))
    source.unlink()
    invisible = subprocess.run(
        ["git", "-C", str(backend), "diff", "--no-renames", "--name-only",
         "--diff-filter=D", "HEAD", "--"],
        check=True, capture_output=True,
    )
    assert invisible.stdout == b"", "Precondition: Git hides the deletion"

    scope = review_scope(runtime.repos())
    assert "orders.py" in scope["backend"]["deleted_paths"]
    assert before != manifest_digest(scope)
    payload = report(scope)
    payload["requirement_results"][0]["evidence"] = [{
        "kind": "deleted", "repository": "backend",
        "path": "orders.py", "locator": "git:assume-unchanged-deletion",
    }]
    assert coordinator.store.validate_report(payload, scope, {"R1"})[
        "verdict"
    ] == "pass"

    source.write_text("def total(n):\n    return n * 2\n", encoding="utf-8")
    assert "orders.py" not in review_scope(runtime.repos())["backend"]["deleted_paths"]


def test_missing_skip_worktree_is_not_a_deletion_tombstone(tmp_path):
    """Sparse-checkout omissions must not masquerade as source deletions."""
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(
        tmp_path, multi=False,
    )
    backend = runtime.repos()["backend"]
    source = backend / "orders.py"
    _git(backend, "add", "orders.py")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track source before sparse omission")
    _git(backend, "update-index", "--skip-worktree", "--", "orders.py")
    source.unlink()
    tagged = subprocess.run(
        ["git", "-C", str(backend), "ls-files", "-v", "--", "orders.py"],
        check=True, capture_output=True,
    ).stdout
    assert tagged.startswith(b"S ") or tagged.startswith(b"s ")
    assert "orders.py" not in review_scope(runtime.repos())["backend"]["deleted_paths"]


def test_missing_newly_staged_assume_unchanged_file_is_not_head_deletion(tmp_path):
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(
        tmp_path, multi=False,
    )
    backend = runtime.repos()["backend"]
    draft = backend / "draft.py"
    draft.write_text("NEW = True\n", encoding="utf-8")
    _git(backend, "add", "draft.py")
    _git(backend, "update-index", "--assume-unchanged", "--", "draft.py")
    draft.unlink()
    assert "draft.py" not in review_scope(runtime.repos())["backend"]["deleted_paths"]


def test_native_pass_uses_assume_unchanged_deletion_evidence_and_stales_on_restore(
    tmp_path, monkeypatch,
):
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(
        tmp_path, multi=False,
    )
    backend = runtime.repos()["backend"]
    source = backend / "orders.py"
    _git(backend, "add", "orders.py")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track removed API")
    _git(backend, "update-index", "--assume-unchanged", "--", "orders.py")
    source.unlink()

    async def delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "assumed-deletion-native")
        scope = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = report(scope)
        body["requirement_results"][0]["evidence"] = [{
            "kind": "deleted", "repository": "backend",
            "path": "orders.py", "locator": "git:removed-api",
        }]
        capture = json.dumps(body)
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("assumed-deletion-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", capture, 202),
            )
        return {"state": "settled", "turn_id": "assumed-deletion-native",
                "agent_response": capture}

    monkeypatch.setattr(runtime, "delegate", delegate)
    rev = graph_rt.get_graph(gid)["revision"]
    approved = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert approved["status"] == "passed"
    assert coordinator.finalize(gid, approved["evaluation_id"], rev)["finalized_at_ns"]
    source.write_text("def total(n):\n    return n * 2\n", encoding="utf-8")
    assert coordinator.read(gid, approved["evaluation_id"])["effective_status"] == "stale"
