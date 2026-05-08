"""Tests for CC Telegram doctor migration and startup preflight.

Covers explicit legacy-state migration checks without touching real home dirs.
"""

from pathlib import Path

import pytest

from cctelegram import doctor


class TestMigrationNeeded:
    def test_true_when_legacy_exists_and_target_missing(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"
        legacy.mkdir()

        assert doctor.migration_needed(legacy, target) is True

    def test_false_when_target_exists(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"
        legacy.mkdir()
        target.mkdir()

        assert doctor.migration_needed(legacy, target) is False

    def test_command_points_at_explicit_copy(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"

        assert doctor.migration_command(legacy, target) == (
            f"mkdir -p {target} && cp -R {legacy}/. {target}/"
        )


class TestPreflight:
    def test_blocks_when_legacy_exists_and_target_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"
        legacy.mkdir()
        monkeypatch.delenv("CC_TELEGRAM_DIR", raising=False)

        with pytest.raises(SystemExit) as exc:
            doctor.preflight_or_exit(legacy, target)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "CC Telegram state migration required" in err
        assert f"cp -R {legacy}/. {target}/" in err

    def test_skips_guard_when_env_dir_explicit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"
        legacy.mkdir()
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(target))

        doctor.preflight_or_exit(legacy, target)


class TestDoctorMain:
    def test_migrate_copies_legacy_contents(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"
        legacy.mkdir()
        (legacy / "state.json").write_text('{"ok": true}', encoding="utf-8")
        monkeypatch.setattr(doctor, "_default_legacy_dir", lambda: legacy)
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(target))

        assert doctor.doctor_main(["--migrate"]) == 0

        assert (target / "state.json").read_text(encoding="utf-8") == '{"ok": true}'
        assert f"Migrated {legacy} -> {target}" in capsys.readouterr().out

    def test_reports_migration_without_copying(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"
        legacy.mkdir()
        monkeypatch.setattr(doctor, "_default_legacy_dir", lambda: legacy)
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(target))

        assert doctor.doctor_main([]) == 0

        out = capsys.readouterr().out
        assert "Migration available" in out
        assert f"cp -R {legacy}/. {target}/" in out
        assert not target.exists()
