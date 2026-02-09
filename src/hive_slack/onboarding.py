"""Per-user onboarding state and progressive teaching messages.

Tracks each user's onboarding progress (welcome sent, threads started,
tips shown) and provides context-appropriate suffixes to append to
bot responses. The system is designed to dissolve — after ~6 interactions,
it goes silent forever.

State persisted at ~/.amplifier/hive/users/{user_id}/onboarding.json
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

USERS_DIR = Path("~/.amplifier/hive/users").expanduser()

# --- Message constants ---

THREAD_FOOTER = "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n_New thread, fresh start \u2014 I don't have context from your other conversations._"

CROSS_THREAD_NOTE = (
    "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "_Heads up: each thread is its own conversation, so I don't have context "
    "from other threads. If you're referring to something specific, paste it "
    "here and I'll pick right up._"
)

TIP_REGENERATE = (
    "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "_Tip: React with :arrows_counterclockwise: on any of my responses to get a fresh take._"
)

TIP_FILE_UPLOAD = (
    "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "_Tip: You can drop files into the thread \u2014 code, images, docs. I'll read them._"
)

TIP_MID_EXECUTION = (
    "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "_Tip: When you see the :hourglass_flowing_sand:, you can send follow-up "
    "messages to steer what I'm doing._"
)

# Regex for detecting cross-thread backward references
CROSS_THREAD_PATTERNS = re.compile(
    r"\b("
    r"as (?:I|we) (?:said|mentioned|asked|described|discussed|noted)"
    r"|like (?:I|we) (?:said|discussed|talked about|mentioned)"
    r"|remember (?:when|what|that thing|the)"
    r"|(?:from|going back to) (?:earlier|before|our (?:last|previous))"
    r"|you (?:said|told me|mentioned|suggested|recommended)"
    r"|(?:earlier|previously|last time) (?:you|I|we)"
    r"|(?:in|from) (?:the|that|my) other (?:thread|conversation|chat|channel)"
    r"|continu(?:e|ing) (?:from |our |where )"
    r"|pick(?:ing)? up where"
    r")",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OnboardingState:
    """Serializable per-user onboarding state."""

    user_id: str = ""
    version: int = 1
    first_seen: str = ""
    welcomed: bool = False
    threads_started: int = 0
    recent_threads: list[str] = field(default_factory=list)
    tips_shown: dict[str, str | None] = field(
        default_factory=lambda: {
            "regenerate": None,
            "file_upload": None,
            "mid_execution": None,
        }
    )
    cross_thread_notes_shown: int = 0


class UserOnboarding:
    """Per-user onboarding manager. Load once per message, save after response."""

    def __init__(self, user_id: str, state: OnboardingState) -> None:
        self._user_id = user_id
        self._state = state

    @classmethod
    async def load(cls, user_id: str) -> UserOnboarding:
        """Load onboarding state from disk, or create fresh state for new users."""
        path = USERS_DIR / user_id / "onboarding.json"
        try:
            if path.exists():
                data = json.loads(path.read_text())
                # Ensure tips_shown has all expected keys
                tips = data.get("tips_shown", {})
                for key in ("regenerate", "file_upload", "mid_execution"):
                    tips.setdefault(key, None)
                data["tips_shown"] = tips
                return cls(user_id, OnboardingState(**data))
        except Exception:
            logger.debug(
                "Could not load onboarding state for %s", user_id, exc_info=True
            )

        return cls(
            user_id,
            OnboardingState(user_id=user_id, first_seen=_now()),
        )

    @property
    def is_first_interaction(self) -> bool:
        """True if this user has never interacted with the bot before."""
        return not self._state.welcomed

    def mark_welcomed(self) -> None:
        """Mark the user as having received the welcome DM."""
        self._state.welcomed = True

    def record_thread(self, conversation_id: str) -> bool:
        """Record a thread interaction. Returns True if this is a NEW thread."""
        if conversation_id in self._state.recent_threads:
            return False
        self._state.recent_threads.append(conversation_id)
        self._state.threads_started += 1
        # FIFO cap
        if len(self._state.recent_threads) > 50:
            self._state.recent_threads = self._state.recent_threads[-50:]
        return True

    @staticmethod
    def has_cross_thread_reference(text: str) -> bool:
        """Check if text contains backward references to other conversations."""
        return bool(CROSS_THREAD_PATTERNS.search(text))

    def get_response_suffix(
        self,
        is_new_thread: bool,
        response_duration: float,
        has_cross_thread_ref: bool,
    ) -> str:
        """Get the onboarding suffix to append to a response.

        Returns the appropriate teaching message, or empty string.
        Only one suffix per response. Priority order:
        1. Cross-thread confusion note (reactive)
        2. Thread footer (first 3 threads)
        3. Mid-execution tip (contextual, >20s response)
        4. Regenerate tip (first new thread after footer phase)
        5. File upload tip (next new thread after regenerate)
        """
        s = self._state

        # Priority 1: Cross-thread confusion (reactive, capped at 3)
        if has_cross_thread_ref and is_new_thread and s.cross_thread_notes_shown < 3:
            s.cross_thread_notes_shown += 1
            return CROSS_THREAD_NOTE

        # Priority 2: Thread footer (first 3 threads)
        if is_new_thread and s.threads_started <= 3:
            return THREAD_FOOTER

        # Below here: only after footer phase
        if s.threads_started <= 3:
            return ""

        # Priority 3: Mid-execution tip (contextual, first long response)
        if response_duration > 20.0 and s.tips_shown.get("mid_execution") is None:
            s.tips_shown["mid_execution"] = _now()
            return TIP_MID_EXECUTION

        # Priority 4-5: Count-based tips (only on new threads)
        if not is_new_thread:
            return ""

        for name, text in [
            ("regenerate", TIP_REGENERATE),
            ("file_upload", TIP_FILE_UPLOAD),
        ]:
            if s.tips_shown.get(name) is None:
                s.tips_shown[name] = _now()
                return text

        return ""

    async def save(self) -> None:
        """Persist onboarding state to disk. Best-effort — never raises."""
        try:
            path = USERS_DIR / self._user_id / "onboarding.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self._state), indent=2))
            tmp.rename(path)  # Atomic on POSIX
        except Exception:
            logger.debug(
                "Failed to save onboarding state for %s",
                self._user_id,
                exc_info=True,
            )
