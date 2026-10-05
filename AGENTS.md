# SmartAgent repository guidance

When the user asks how to install, configure, start, update, troubleshoot, or remove SmartAgent, read `skills/smartagent-onboarding/SKILL.md` completely before acting.

Treat the repository root as the active release. Files under `legacy/` are retained for reference only and must not be used for a new installation unless the user explicitly requests a legacy version.

Never copy or publish `.venv`, `localdata`, `.agents`, browser profiles, tokens, cookies, personal conversation URLs, or machine-specific debug configuration.
