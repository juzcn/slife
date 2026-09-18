"""Tests for slife.ui.i18n — the TUI translation layer.

Covers the contract: ``t()`` returns the right language, ``set_language``
overrides, format placeholders interpolate, and missing keys raise (never
silently fall back — a typo'd key is a bug to surface, not hide).  Also the
OS query that picks the initial language, since it is hand-rolled stdlib.
"""

import pytest; pytestmark = pytest.mark.unit

from slife.ui import i18n
from slife.ui.i18n import t, set_language, get_language


@pytest.fixture(autouse=True)
def _restore_language():
    """Each test starts in English and restores the prior language after."""
    prev = get_language()
    set_language("en")
    yield
    set_language(prev)


class TestTranslation:
    def test_english_default(self):
        assert t("interrupted") == "⏹ Interrupted"

    def test_chinese_when_set(self):
        set_language("zh")
        assert t("interrupted") == "⏹ 已中断"

    def test_format_placeholders(self):
        set_language("en")
        assert t("restore_failed", err="boom") == "✗ Restore failed: boom"

    def test_format_placeholders_chinese(self):
        set_language("zh")
        out = t("restore_failed", err="boom")
        assert "恢复失败" in out
        assert "boom" in out

    def test_set_language_round_trip(self):
        set_language("zh")
        assert get_language() == "zh"
        set_language("en")
        assert get_language() == "en"

    def test_unknown_key_raises(self):
        """A typo'd key must surface, not render blank."""
        with pytest.raises(KeyError):
            t("this_key_does_not_exist")

    def test_missing_placeholder_raises(self):
        """Strict formatting — a missing field is a call-site bug."""
        with pytest.raises(KeyError):
            t("restore_failed")  # no err=

    def test_all_keys_have_both_languages(self):
        """Every entry ships English + Chinese — no half-translated keys."""
        from slife.ui.i18n import _STRINGS
        for key, entry in _STRINGS.items():
            assert "en" in entry, f"{key} missing English"
            assert "zh" in entry, f"{key} missing Chinese"


class TestOsLocaleDetection:
    """The OS query behind the language choice — not the strings."""

    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch):
        """Always take the *nix branch: the Windows one is a Win32 API call
        that only a Windows box can answer, and this suite also runs on
        Windows CI.  The two branches differ only in where the tag comes
        from, so the mapping below is what both feed into."""
        monkeypatch.setattr(i18n.sys, "platform", "linux")

    @staticmethod
    def _env(monkeypatch, **values):
        """Set exactly *values* — the real environment must not leak in."""
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(var, raising=False)
        for var, value in values.items():
            monkeypatch.setenv(var, value)

    def test_zh_variants_are_all_chinese(self, monkeypatch):
        for value in ("zh", "zh_CN.UTF-8", "zh_TW.UTF-8", "zh-Hans"):
            self._env(monkeypatch, LANG=value)
            assert i18n._detect_language() == "zh", value

    def test_non_chinese_is_english(self, monkeypatch):
        for value in ("en_US.UTF-8", "C", "POSIX", "ja_JP.UTF-8"):
            self._env(monkeypatch, LANG=value)
            assert i18n._detect_language() == "en", value

    def test_more_specific_variable_wins(self, monkeypatch):
        """LC_ALL overrides LANG, as POSIX specifies."""
        self._env(monkeypatch, LC_ALL="zh_CN.UTF-8", LANG="en_US.UTF-8")
        assert i18n._detect_language() == "zh"

    def test_unset_environment_is_english(self, monkeypatch):
        self._env(monkeypatch)
        assert i18n._detect_language() == "en"

    def test_a_failing_query_degrades_to_english(self, monkeypatch):
        """Detection never raises — a broken OS query still yields a TUI."""
        def _boom():
            raise OSError("no such API")

        monkeypatch.setattr(i18n, "_os_language", _boom)
        assert i18n._detect_language() == "en"
