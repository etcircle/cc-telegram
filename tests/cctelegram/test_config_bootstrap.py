"""Hermetic pytest/CI bootstrap coverage for import-time Config use."""

import tempfile
from pathlib import Path

from cctelegram.config import config
from cctelegram.utils import app_dir

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOOTSTRAP_TOKEN = config.telegram_bot_token
_BOOTSTRAP_ALLOWED_USERS = set(config.allowed_users)
_BOOTSTRAP_CONFIG_DIR = config.config_dir


def test_pytest_bootstrap_uses_dummy_config_for_import_time_singleton():
    assert _BOOTSTRAP_TOKEN == "0000000000:pytest-dummy-token"
    assert _BOOTSTRAP_ALLOWED_USERS == {12345}
    assert _BOOTSTRAP_CONFIG_DIR != Path.home() / ".cc-telegram"
    assert _BOOTSTRAP_CONFIG_DIR.name.startswith("cc-telegram-pytest-")
    assert _BOOTSTRAP_CONFIG_DIR.is_dir()


def test_the_suite_never_reads_or_writes_the_LIVE_config_dir():
    """``app_dir()`` must resolve to this run's throwaway directory.

    The same rationale as ``conftest._no_live_tmux``: a test must never read or
    write the LIVE bot's state. This one is not theoretical —
    ``conftest._reset_all_handler_state`` UNLINKS ``session_map.json``, the AUQ
    action ledger, the pick-intent store and the ``auq_pending`` /
    ``notify_pending`` side files under ``app_dir()`` between every single test.
    Pointed at a real config dir it would wipe the running bot's state on every
    reset, and the monitor's next poll would unregister every live session until
    each one's ``SessionStart`` hook re-fired.

    The root ``conftest.py`` pins ``CC_TELEGRAM_DIR`` to a per-run ``mkdtemp``
    before any collection, which is what makes that safe — including when the
    developer's shell exports ``CC_TELEGRAM_DIR`` at the live directory, because
    the pin OVERWRITES it. This test is the permanent guard on that pin.

    ``app_dir()`` is asserted SEPARATELY from ``config.config_dir`` above and
    then asserted EQUAL to it, because they are different seams: ``config``
    resolves once at import, ``app_dir()`` re-reads the environment on every
    call. A second ``CC_TELEGRAM_DIR`` pin added later (in ``tests/conftest.py``,
    say) would silently split them — production modules would then disagree with
    each other about where state lives, and the new directory would leak every
    run since only the root pin registers an ``atexit`` cleanup.
    """
    resolved = app_dir()

    assert resolved != Path.home() / ".cc-telegram", (
        "the suite resolved app_dir() to the LIVE default config directory"
    )
    # Strictly stronger than "not the shell's CC_TELEGRAM_DIR": this proves the
    # directory is THIS run's throwaway, so no externally-supplied path — the
    # shell's or anything else's — can be what we are about to delete files in.
    assert resolved.name.startswith("cc-telegram-pytest-"), resolved
    # Both sides resolved: on macOS the system temp dir is reached through the
    # ``/var`` -> ``/private/var`` symlink, so the raw paths differ.
    assert resolved.resolve().parent == Path(tempfile.gettempdir()).resolve(), resolved
    assert resolved.is_dir(), resolved
    assert resolved == _BOOTSTRAP_CONFIG_DIR, (
        "app_dir() and config.config_dir must resolve to the SAME directory; "
        "a second CC_TELEGRAM_DIR pin has split them"
    )


def test_check_workflow_supplies_dummy_config_env():
    workflow = (_REPO_ROOT / ".github" / "workflows" / "check.yml").read_text()

    assert 'TELEGRAM_BOT_TOKEN: "0000000000:ci-dummy-token"' in workflow
    assert 'ALLOWED_USERS: "12345"' in workflow
    assert "CC_TELEGRAM_DIR=$RUNNER_TEMP/cc-telegram-config" in workflow
