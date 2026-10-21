# M365 provider wiring

This package implements the provider-neutral WebUI contract directly through
BaseWebUIAdapter. It never inherits another provider's adapter or borrows its DOM.

The built-in `m365_bootstrap_v1` profile contains **candidate selectors**, not a
claim of live compatibility. Use install_smart_agent/launchers/adapterUI.bat with a working planner and a
signed-in `https://m365.cloud.microsoft/chat/conversation/<id>` target. Calibration
publishes a versioned M365 profile only after its existing live checks pass.
Edit_workspace can bind the URL before calibration; binding is not validation.

Implemented wiring: routing, workspace/conversation identity, INDEX_ONLY receiver
identity, composer integrity, request-scoped turn pairing, activity and final text,
profile publication/loading, and trusted input bridge using the active profile.
Remote browser sessions use a separate profile and localhost CDP port 1275;
workers consume the explicit --remote-cdp endpoint via the existing legacy env key.
That port must be free. Cross-instance port allocation is not introduced here.

Attachments fail closed with M365_ATTACHMENTS_NOT_CALIBRATED. Image/media observations
use the neutral base; live artifact download, tenant-specific authentication,
iframes/shadow DOM and M365 DOM end-to-end remain NOT_TESTED. Successful wiring UT
does not imply these capabilities or selector calibration have passed.
