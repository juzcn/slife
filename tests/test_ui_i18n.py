"""Tests for slife.ui.i18n — the TUI translation layer.

Covers the contract: ``t()`` returns the right language, ``set_language``
overrides, format placeholders interpolate, and missing keys raise (never
silently fall back — a typo'd key is a bug to surface, not hide).
"""

import pytest; pytestmark = pytest.mark.unit

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
        assert t("restored_partial", n=3, skipped=2) == (
            "✅ Restored exit-time context (3 turns; 2 earlier "
            "turns not loaded — use turn_search to find them)"
        )

    def test_format_placeholders_chinese(self):
        set_language("zh")
        out = t("restored_partial", n=3, skipped=2)
        assert "3 轮" in out
        assert "2 轮" in out

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
            t("restored_partial")  # no n= / skipped=

    def test_all_keys_have_both_languages(self):
        """Every entry ships English + Chinese — no half-translated keys."""
        from slife.ui.i18n import _STRINGS
        for key, entry in _STRINGS.items():
            assert "en" in entry, f"{key} missing English"
            assert "zh" in entry, f"{key} missing Chinese"
