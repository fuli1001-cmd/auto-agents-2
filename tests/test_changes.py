import pytest

from auto_agents_2.errors import PolicyViolation
from auto_agents_2.models import Change
from auto_agents_2.util import digest
from auto_agents_2.workspace import apply_changes, apply_prepared, prepare_changes


def replacement(path, old, new):
    return Change(path=path, operation="replace", old_text=old, content=new)


def test_multiple_replacements_compose_before_writing_and_replay_after_interruption(tmp_path):
    source = b"const a = 1;\r\nconst b = 2;\r\n"
    (tmp_path / "prototype.js").write_bytes(source)
    (tmp_path / "other.js").write_text("old")
    changes = [replacement("prototype.js", "a = 1", "a = 3"),
               replacement("other.js", "old", "new"),
               replacement("prototype.js", "b = 2", "b = 4"),
               replacement("prototype.js", "a = 3", "a = 5")]
    prepared = prepare_changes(tmp_path, changes, ["*.js"])
    assert len(prepared) == 2
    final = b"const a = 5;\r\nconst b = 4;\r\n"
    assert prepared[0]["before"] == digest(source)
    assert prepared[0]["after"] == digest(final)
    assert (tmp_path / "prototype.js").read_bytes() == source
    apply_prepared(tmp_path, prepared[:1])
    apply_prepared(tmp_path, prepared)
    apply_prepared(tmp_path, prepared)
    assert (tmp_path / "prototype.js").read_bytes() == final
    assert (tmp_path / "other.js").read_text() == "new"


@pytest.mark.parametrize("old,new", [("missing", "new"), ("duplicate", "duplicate duplicate"), ("", "new")])
def test_invalid_later_replacement_leaves_entire_batch_untouched(tmp_path, old, new):
    (tmp_path / "one.js").write_text("old")
    (tmp_path / "two.js").write_text("old")
    changes = [replacement("one.js", "old", "new"), replacement("two.js", "old", new),
               replacement("two.js", old, "final")]
    with pytest.raises(PolicyViolation, match="match exactly once"):
        apply_changes(tmp_path, changes, ["*.js"])
    assert (tmp_path / "one.js").read_text() == (tmp_path / "two.js").read_text() == "old"


@pytest.mark.parametrize("first,second", [("write", "write"), ("delete", "delete"), ("write", "replace"),
                                         ("replace", "write"), ("delete", "replace"), ("replace", "delete")])
def test_mixed_or_duplicate_whole_file_changes_still_rejected(tmp_path, first, second):
    (tmp_path / "app.js").write_text("old")
    changes = [Change(path="app.js", operation=first, content="new", old_text="old"),
               Change(path="./app.js", operation=second, content="final", old_text="new")]
    with pytest.raises(PolicyViolation, match="Duplicate change"):
        apply_changes(tmp_path, changes, ["*"])
    assert (tmp_path / "app.js").read_text() == "old"


def test_composed_replacement_does_not_overwrite_user_changes(tmp_path):
    (tmp_path / "one.js").write_text("old")
    (tmp_path / "two.js").write_text("old")
    prepared = prepare_changes(tmp_path, [replacement("one.js", "old", "middle"),
        replacement("one.js", "middle", "final"), replacement("two.js", "old", "new")], ["*.js"])
    (tmp_path / "one.js").write_text("user edit")
    with pytest.raises(PolicyViolation, match="File changed"):
        apply_prepared(tmp_path, prepared)
    assert (tmp_path / "one.js").read_text() == "user edit"
    assert (tmp_path / "two.js").read_text() == "old"


@pytest.mark.parametrize("path,scopes,options", [
    ("test_app.py", ["*.py"], {}),
    ("app.py", ["other.py"], {}),
    ("design/prototypes/app.js", ["design/prototypes"], {"protected": False}),
])
def test_multiple_replacements_keep_scope_test_and_contract_protection(tmp_path, path, scopes, options):
    file = tmp_path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("old")
    with pytest.raises(PolicyViolation):
        apply_changes(tmp_path, [replacement(path, "old", "middle"), replacement(path, "middle", "new")],
                      scopes, **options)
    assert file.read_text() == "old"
