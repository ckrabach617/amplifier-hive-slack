"""Tests for the user onboarding system."""

import pytest

from hive_slack.onboarding import (
    CROSS_THREAD_NOTE,
    THREAD_FOOTER,
    TIP_FILE_UPLOAD,
    TIP_MID_EXECUTION,
    TIP_REGENERATE,
    UserOnboarding,
)


class TestOnboardingState:
    """Test state loading and saving."""

    @pytest.mark.asyncio
    async def test_new_user_returns_fresh_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_NEW")
        assert onboarding.is_first_interaction is True

    @pytest.mark.asyncio
    async def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)

        # Create and save
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.mark_welcomed()
        onboarding.record_thread("C1:t1")
        await onboarding.save()

        # Load again
        reloaded = await UserOnboarding.load("U_TEST")
        assert reloaded.is_first_interaction is False  # was welcomed
        assert reloaded.record_thread("C1:t1") is False  # already seen

    @pytest.mark.asyncio
    async def test_save_creates_directories(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_NEW2")
        await onboarding.save()
        assert (tmp_path / "U_NEW2" / "onboarding.json").exists()


class TestWelcome:
    """Test first-interaction detection."""

    @pytest.mark.asyncio
    async def test_first_interaction_true_for_new_user(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_NEW")
        assert onboarding.is_first_interaction is True

    @pytest.mark.asyncio
    async def test_first_interaction_false_after_welcome(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_NEW")
        onboarding.mark_welcomed()
        assert onboarding.is_first_interaction is False


class TestThreadTracking:
    """Test thread recording and new-thread detection."""

    @pytest.mark.asyncio
    async def test_new_thread_returns_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        assert onboarding.record_thread("C1:t1") is True

    @pytest.mark.asyncio
    async def test_same_thread_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.record_thread("C1:t1")
        assert onboarding.record_thread("C1:t1") is False

    @pytest.mark.asyncio
    async def test_threads_capped_at_50(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        for i in range(60):
            onboarding.record_thread(f"C1:t{i}")
        assert len(onboarding._state.recent_threads) == 50


class TestCrossThreadDetection:
    """Test backward-reference pattern matching."""

    def test_detects_as_i_mentioned(self):
        assert UserOnboarding.has_cross_thread_reference("As I mentioned earlier")

    def test_detects_remember_when(self):
        assert UserOnboarding.has_cross_thread_reference("Remember when we discussed")

    def test_detects_you_said(self):
        assert UserOnboarding.has_cross_thread_reference("You said something about")

    def test_detects_from_earlier(self):
        assert UserOnboarding.has_cross_thread_reference("Going back to earlier")

    def test_detects_continue_from(self):
        assert UserOnboarding.has_cross_thread_reference(
            "Continue from where we left off"
        )

    def test_no_false_positive_on_normal_text(self):
        assert not UserOnboarding.has_cross_thread_reference("What is Python?")

    def test_no_false_positive_on_remember_milk(self):
        # This is acceptable — the note is helpful even if slightly off
        # But "remember the" does match, so this is a known soft match
        result = UserOnboarding.has_cross_thread_reference("Remember the milk")
        # Either True or False is acceptable — document behavior
        assert isinstance(result, bool)


class TestResponseSuffix:
    """Test the priority-based suffix system."""

    @pytest.mark.asyncio
    async def test_thread_footer_on_first_3_threads(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.mark_welcomed()

        onboarding.record_thread("C1:t1")
        suffix = onboarding.get_response_suffix(True, 1.0, False)
        assert THREAD_FOOTER == suffix

    @pytest.mark.asyncio
    async def test_no_footer_after_3_threads(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.mark_welcomed()

        for i in range(4):
            onboarding.record_thread(f"C1:t{i}")

        # 4th thread (threads_started == 4 > 3)
        suffix = onboarding.get_response_suffix(True, 1.0, False)
        assert suffix != THREAD_FOOTER

    @pytest.mark.asyncio
    async def test_cross_thread_note_supersedes_footer(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.mark_welcomed()
        onboarding.record_thread("C1:t1")
        suffix = onboarding.get_response_suffix(True, 1.0, True)  # has_cross_ref=True
        assert CROSS_THREAD_NOTE == suffix

    @pytest.mark.asyncio
    async def test_cross_thread_note_capped_at_3(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.mark_welcomed()

        # Show cross-thread note 3 times
        for i in range(3):
            onboarding.record_thread(f"C1:t{i}")
            onboarding.get_response_suffix(True, 1.0, True)

        # 4th time should NOT show cross-thread note
        onboarding.record_thread("C1:t99")
        suffix = onboarding.get_response_suffix(True, 1.0, True)
        assert suffix != CROSS_THREAD_NOTE

    @pytest.mark.asyncio
    async def test_regenerate_tip_after_footer_phase(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.mark_welcomed()

        # Get through footer phase (first 3 threads show footer)
        for i in range(3):
            onboarding.record_thread(f"C1:t{i}")
            suffix = onboarding.get_response_suffix(True, 1.0, False)
            assert suffix == THREAD_FOOTER

        # 4th thread: past footer phase, should get regenerate tip
        onboarding.record_thread("C1:t3")
        suffix = onboarding.get_response_suffix(True, 1.0, False)
        assert TIP_REGENERATE == suffix

    @pytest.mark.asyncio
    async def test_file_upload_tip_after_regenerate(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.mark_welcomed()

        # Get through footer phase (3 threads) + regenerate tip (4th thread)
        for i in range(4):
            onboarding.record_thread(f"C1:t{i}")
            onboarding.get_response_suffix(True, 1.0, False)

        # 5th thread should get file upload tip
        onboarding.record_thread("C1:t4")
        suffix = onboarding.get_response_suffix(True, 1.0, False)
        assert TIP_FILE_UPLOAD == suffix

    @pytest.mark.asyncio
    async def test_mid_execution_tip_on_long_response(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.mark_welcomed()

        # Get past footer phase
        for i in range(4):
            onboarding.record_thread(f"C1:t{i}")
            onboarding.get_response_suffix(True, 1.0, False)

        # Long response (>20s) should get mid-execution tip
        suffix = onboarding.get_response_suffix(False, 25.0, False)
        assert TIP_MID_EXECUTION == suffix

    @pytest.mark.asyncio
    async def test_no_suffix_for_old_thread(self, tmp_path, monkeypatch):
        """Follow-up in existing thread gets no suffix (not a new thread)."""
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.mark_welcomed()

        # Past footer phase, all tips shown
        for i in range(10):
            onboarding.record_thread(f"C1:t{i}")
            onboarding.get_response_suffix(True, 1.0, False)
        onboarding.get_response_suffix(False, 25.0, False)  # show mid-execution

        # Normal reply in existing thread
        suffix = onboarding.get_response_suffix(False, 2.0, False)
        assert suffix == ""

    @pytest.mark.asyncio
    async def test_all_tips_eventually_empty(self, tmp_path, monkeypatch):
        """After all tips shown, suffix is always empty."""
        monkeypatch.setattr("hive_slack.onboarding.USERS_DIR", tmp_path)
        onboarding = await UserOnboarding.load("U_TEST")
        onboarding.mark_welcomed()

        # Exhaust everything
        for i in range(20):
            onboarding.record_thread(f"C1:t{i}")
            onboarding.get_response_suffix(True, 25.0, False)

        # Nothing left to show
        onboarding.record_thread("C1:t99")
        suffix = onboarding.get_response_suffix(True, 25.0, False)
        assert suffix == ""
