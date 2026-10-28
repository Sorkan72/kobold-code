import json

import pytest

import app


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setitem(app.DEFAULT_SETTINGS, "workspace", str(root))
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")
    return root


def test_safe_path_rejects_parent_traversal(workspace):
    with pytest.raises(ValueError, match="escapes"):
        app.safe_path("../outside.txt")


def test_safe_path_rejects_symlink_escape(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    link = workspace / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is not permitted on this platform")
    with pytest.raises(ValueError, match="escapes"):
        app.safe_path("external/secret.txt")


def test_file_tools_write_read_and_edit(workspace):
    created = app.execute_tool("write_file", {"path": "src/hello.txt", "content": "hello world"})
    assert created["ok"] is True
    assert created["overwrote_existing_file"] is False

    read = app.execute_tool("read_file", {"path": "src/hello.txt"})
    assert read["content"] == "hello world"

    edited = app.execute_tool(
        "edit_file",
        {"path": "src/hello.txt", "old_text": "world", "new_text": "KoboldCode"},
    )
    assert edited["ok"] is True
    assert (workspace / "src" / "hello.txt").read_text(encoding="utf-8") == "hello KoboldCode"


def test_edit_file_refuses_ambiguous_match(workspace):
    target = workspace / "repeat.txt"
    target.write_text("same same", encoding="utf-8")

    result = app.execute_tool(
        "edit_file",
        {"path": "repeat.txt", "old_text": "same", "new_text": "changed"},
    )

    assert "error" in result
    assert target.read_text(encoding="utf-8") == "same same"


def test_tool_result_truncation_stays_valid_json():
    serialized = app._serialize_tool_result({"output": "x" * 5000}, limit=300)
    result = json.loads(serialized)
    assert result["truncated"] is True
    assert result["original_characters"] > 300
    assert isinstance(result["preview"], str)

def test_repeated_identical_tool_failure_is_detected():
    seen = {}
    args = {"path": "index.html", "old_text": "missing", "new_text": "replacement"}
    error = {"error": "Expected old_text to occur exactly once, found 0. No change made."}

    assert app._is_repeated_tool_failure("edit_file", args, error, seen) is False
    assert app._is_repeated_tool_failure("edit_file", args, error, seen) is True


def test_different_tool_args_do_not_trigger_repeated_failure_guard():
    seen = {}
    error = {"error": "File does not exist."}

    assert app._is_repeated_tool_failure("read_file", {"path": "one.txt"}, error, seen) is False
    assert app._is_repeated_tool_failure("read_file", {"path": "two.txt"}, error, seen) is False


def test_successful_tool_result_does_not_count_as_failure():
    seen = {}
    assert app._is_repeated_tool_failure("edit_file", {"path": "index.html"}, {"ok": True}, seen) is False
    assert seen == {}

