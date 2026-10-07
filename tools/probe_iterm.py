"""Read-only iTerm2 connectivity probe. Never captures or writes session contents."""
import asyncio
import json
import sys

from cliretry.adapters.iterm2_adapter import Iterm2Adapter
from cliretry.config import Config


async def main():
    adapter = Iterm2Adapter(Config())
    try:
        await asyncio.wait_for(adapter.connect(), 15)
        sessions = []
        for window in adapter.app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    sessions.append({"session_id": session.session_id,
                                     "window_id": window.window_id, "tab_id": tab.tab_id})
        print(json.dumps({"connected": True, "transport": adapter.transport,
                          "iterm_version": adapter.app_version(), "sdk_version": adapter.sdk_version,
                          "sessions": sessions}))
    except Exception as exc:
        print(json.dumps({"connected": False, "transport": adapter.transport,
                          "error_type": type(exc).__name__,
                          "message": str(exc)}))
        sys.exit(1)
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
