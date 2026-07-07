"""Tests for skill archive installer — extraction, validation, security scan, error handling."""

import io
import zipfile

import pytest

from harness.skills.installer import (
    ensure_safe_support_path,
    _extract_archive,
    _ALLOWED_TOP_LEVEL,
)
from pathlib import Path


# ===================================================================
# ensure_safe_support_path
# ===================================================================


class TestEnsureSafeSupportPath:
    def test_valid_references_path(self):
        p = ensure_safe_support_path("references/my_doc.md")
        assert str(p) == "references/my_doc.md"

    def test_valid_templates_path(self):
        p = ensure_safe_support_path("templates/report.md")
        assert str(p) == "templates/report.md"

    def test_valid_scripts_path(self):
        p = ensure_safe_support_path("scripts/helper.sh")
        assert str(p) == "scripts/helper.sh"

    def test_valid_assets_path(self):
        p = ensure_safe_support_path("assets/logo.png")
        assert str(p) == "assets/logo.png"

    def test_valid_nested_path(self):
        p = ensure_safe_support_path("references/dir/subdir/deep.md")
        assert str(p) == "references/dir/subdir/deep.md"

    def test_rejects_empty_path(self):
        with pytest.raises(ValueError, match="must not be empty"):
            ensure_safe_support_path("")

    def test_rejects_whitespace_path(self):
        with pytest.raises(ValueError, match="must not be empty"):
            ensure_safe_support_path("   ")

    def test_rejects_absolute_path(self):
        with pytest.raises(ValueError, match="Absolute paths"):
            ensure_safe_support_path("/etc/passwd")

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="traversal"):
            ensure_safe_support_path("../../../etc/passwd")

    def test_rejects_path_traversal_mid(self):
        with pytest.raises(ValueError, match="traversal"):
            ensure_safe_support_path("references/../../../etc/passwd")

    def test_rejects_disallowed_top_level(self):
        with pytest.raises(ValueError, match="must be under one of"):
            ensure_safe_support_path("evil/dangerous.sh")

    def test_rejects_skil_md_top_level(self):
        with pytest.raises(ValueError, match="must be under one of"):
            ensure_safe_support_path("SKILL.md")

    def test_rejects_empty_top_level(self):
        with pytest.raises(ValueError, match="must be under one of"):
            ensure_safe_support_path(".")

    def test_handles_windows_separators(self):
        p = ensure_safe_support_path("references\\windows_path.md")
        assert "/" in str(p) or "\\" in str(p)


# ===================================================================
# _extract_archive
# ===================================================================


def _make_zip(files: dict[str, str]) -> bytes:
    """Create an in-memory ZIP archive from a dict of filename → content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestExtractArchive:
    def test_valid_minimal_archive(self, tmp_path):
        """A minimal archive with only SKILL.md extracts fine."""
        data = _make_zip({
            "SKILL.md": "---\nname: test\ndescription: A test\n---\n# Test",
        })
        archive = tmp_path / "test.skill"
        archive.write_bytes(data)

        staging = tmp_path / "staging"
        staging.mkdir()
        _extract_archive(archive, staging)
        assert (staging / "SKILL.md").exists()

    def test_archive_with_support_dirs(self, tmp_path):
        """Archive with all allowed directories extracts fine."""
        data = _make_zip({
            "SKILL.md": "---\nname: full\ndescription: Full skill\n---\n# Full",
            "references/readme.md": "# Ref",
            "templates/report.md": "# Template",
            "scripts/setup.sh": "#!/bin/bash\necho ok",
            "assets/icon.png": "fake-png",
        })
        archive = tmp_path / "full.skill"
        archive.write_bytes(data)

        staging = tmp_path / "staging"
        staging.mkdir()
        _extract_archive(archive, staging)

        assert (staging / "SKILL.md").exists()
        assert (staging / "references" / "readme.md").exists()
        assert (staging / "templates" / "report.md").exists()
        assert (staging / "scripts" / "setup.sh").exists()
        assert (staging / "assets" / "icon.png").exists()

    def test_rejects_disallowed_top_level_entry(self, tmp_path):
        """Archives with files outside allowed dirs are rejected."""
        data = _make_zip({
            "SKILL.md": "---\nname: test\ndescription: test\n---\n# Test",
            "evil/malware.sh": "bad",
        })
        archive = tmp_path / "bad.skill"
        archive.write_bytes(data)

        staging = tmp_path / "staging"
        staging.mkdir()
        with pytest.raises(ValueError, match="Disallowed top-level entry"):
            _extract_archive(archive, staging)

    def test_rejects_path_traversal_in_archive(self, tmp_path):
        """ZIP slip / path traversal is rejected."""
        data = _make_zip({
            "SKILL.md": "---\nname: test\ndescription: test\n---\n# Test",
            "references/../../../etc/passwd": "evil",
        })
        archive = tmp_path / "traversal.skill"
        archive.write_bytes(data)

        staging = tmp_path / "staging"
        staging.mkdir()
        with pytest.raises(ValueError, match="traversal"):
            _extract_archive(archive, staging)

    def test_rejects_corrupted_archive(self, tmp_path):
        """Corrupted/invalid ZIP → ValueError."""
        archive = tmp_path / "corrupt.skill"
        archive.write_text("not a zip file")

        staging = tmp_path / "staging"
        staging.mkdir()
        with pytest.raises(ValueError, match="Invalid or corrupted"):
            _extract_archive(archive, staging)

    def test_skips_directory_entries(self, tmp_path):
        """Directory entries inside ZIP are safely ignored."""
        data = _make_zip({
            "SKILL.md": "---\nname: test\ndescription: test\n---\n# Test",
            "references/": "",  # directory entry
        })
        archive = tmp_path / "with_dir.skill"
        # Can't easily create dir entries with ZipFile.writestr, but the
        # extract code handles them. Test that a trailing-slash entry doesn't crash.
        archive.write_bytes(data)

        staging = tmp_path / "staging"
        staging.mkdir()
        _extract_archive(archive, staging)
        assert (staging / "SKILL.md").exists()

    def test_handles_hidden_files_in_archive(self, tmp_path):
        """Files like .gitkeep inside allowed dirs are fine."""
        data = _make_zip({
            "SKILL.md": "---\nname: test\ndescription: test\n---\n# Test",
            "references/.gitkeep": "",
        })
        archive = tmp_path / "hidden.skill"
        archive.write_bytes(data)

        staging = tmp_path / "staging"
        staging.mkdir()
        _extract_archive(archive, staging)
        assert (staging / "references" / ".gitkeep").exists()


# ===================================================================
# ALLOWED_TOP_LEVEL constant
# ===================================================================


class TestAllowedTopLevel:
    def test_contains_expected_dirs(self):
        assert _ALLOWED_TOP_LEVEL == {"SKILL.md", "references", "templates", "scripts", "assets"}

    def test_no_extra_dirs(self):
        """Ensure only the 5 allowed entries exist (no accidental additions)."""
        assert len(_ALLOWED_TOP_LEVEL) == 5
