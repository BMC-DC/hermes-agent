"""Regression test (2026-09-21) for the bug ``root_config.py`` fixes: a plain
``load_config()`` call inside a profile-scoped worker process (e.g. Stylus
completing a task) used to read *that profile's own* ``config.yaml``, not
root's -- causing real WhatsApp notifications to silently route to the
wrong group. ``load_root_config``/``load_pipeline_extra`` must always
resolve the root/default profile's config, regardless of what ``HERMES_HOME``
the calling process happens to have.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import config as hermes_config
from plugins.platforms.event_post_pipeline import root_config


@pytest.fixture
def root_and_profile_homes(tmp_path, monkeypatch):
    """A root ``.hermes`` dir with its own config.yaml, plus a profile dir
    (``.hermes/profiles/stylus``) with a *different* config.yaml -- mirrors
    ``resolve_profile_env``'s real ``<root>/profiles/<name>`` shape."""
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text(
        "platforms:\n  event_post_pipeline:\n    extra:\n      review_base_url: https://root.example\n",
        encoding="utf-8",
    )
    profile = root / "profiles" / "stylus"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "platforms:\n  event_post_pipeline:\n    extra:\n      review_base_url: https://stale-stylus-copy.example\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return root, profile


def test_load_pipeline_extra_reads_root_even_from_a_profile_scoped_process(root_and_profile_homes, monkeypatch):
    root, profile = root_and_profile_homes
    # Simulate exactly what a Stylus worker subprocess looks like: HERMES_HOME
    # pointed at the profile dir, not root.
    monkeypatch.setenv("HERMES_HOME", str(profile))
    hermes_config._LOAD_CONFIG_CACHE.clear()

    extra = root_config.load_pipeline_extra()

    assert extra["review_base_url"] == "https://root.example"


def test_load_pipeline_extra_still_works_when_already_at_root(root_and_profile_homes, monkeypatch):
    root, _profile = root_and_profile_homes
    monkeypatch.setenv("HERMES_HOME", str(root))
    hermes_config._LOAD_CONFIG_CACHE.clear()

    extra = root_config.load_pipeline_extra()

    assert extra["review_base_url"] == "https://root.example"


def test_load_root_config_restores_the_override_afterward(root_and_profile_homes, monkeypatch):
    """The context-local override must not leak into code that runs after this call."""
    root, profile = root_and_profile_homes
    monkeypatch.setenv("HERMES_HOME", str(profile))
    hermes_config._LOAD_CONFIG_CACHE.clear()

    root_config.load_root_config()

    from hermes_constants import get_hermes_home_override
    assert get_hermes_home_override() is None
