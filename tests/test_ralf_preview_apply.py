from pathlib import Path
import importlib.util


_SPEC = importlib.util.spec_from_file_location("ralf_tool", "/tmp/ralf_diff_review_patch/tools/ralf.py")
assert _SPEC is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)


def test_select_preview_pairs_filters_requested_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pairs = [
        (str(repo / "a.txt"), str(tmp_path / ".ralf_preview" / "a.txt")),
        (str(repo / "nested" / "b.txt"), str(tmp_path / ".ralf_preview" / "nested" / "b.txt")),
    ]

    selected, missing = _MODULE.select_preview_pairs(pairs, str(repo), ["nested/b.txt", "missing.txt"])

    assert selected == [pairs[1]]
    assert missing == ["missing.txt"]


def test_apply_preview_pairs_copies_selected_preview_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    preview = tmp_path / ".ralf_preview"
    (repo / "nested").mkdir(parents=True)
    (preview / "nested").mkdir(parents=True)
    (preview / "nested" / "b.txt").write_text("preview-content", encoding="utf-8")

    copied = _MODULE.apply_preview_pairs(
        [(str(repo / "nested" / "b.txt"), str(preview / "nested" / "b.txt"))],
        str(repo),
    )

    assert copied == [str((repo / "nested" / "b.txt").resolve())]
    assert (repo / "nested" / "b.txt").read_text(encoding="utf-8") == "preview-content"
