SmartAgent + WebAgent standalone release

First-time setup
1. Extract the complete release folder to a writable local directory.
2. Run install_smart_agent.bat.
3. Review PASS / MISSING / BROKEN results. If repair is required, press Enter.
4. The installer creates release\.venv and installs Python packages and Playwright Chromium.

Launchers
- launch_smart_agent.bat: start LocalAgent from this release directory.
- launch_remote_agent.bat: start the independent RemoteAgent receiver.
- launch_webcopilot_chatgpt.bat: paste a ChatGPT /c/... URL and run WebAgent Direct.

Important
- Keep agent_core, RemoteAgent, WebAgent, install_smart_agent, and all root-level Python/BAT files together.
- launch_smart_agent.bat resolves all code and state relative to this release directory; it does not use the parent checkout.
- Runtime state is written to .agents and WebAgent\state; both are ignored by Git.
- No Python binary, cookies, ChatGPT URLs, workspace paths, tokens, or personal state are included.
- install_release.bat is a compatibility alias for install_smart_agent.bat.

Diagnostics
- install_smart_agent.bat -ValidateOnly
- install_smart_agent.bat -SkipOllama
- python verify_release.py
