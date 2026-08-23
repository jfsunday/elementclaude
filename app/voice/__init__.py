"""Speech-to-text (inbound voice messages) and text-to-speech (spoken answers).

Both backends are optional: heavy dependencies (`edge-tts`, `faster-whisper`) are
imported lazily inside the functions so a base install without the `voice` extra
keeps working — voice just stays unavailable and says so in the room.
"""

from __future__ import annotations
