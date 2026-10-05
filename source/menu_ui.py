"""Lightweight WebGPT-only model selector compatibility module."""


def run_model_selector(prompt_text: str, downloaded_models: dict, has_internet: bool, *,
                       web_options=None, cloud_options=None, fetch_catalog_fn=None,
                       pull_model_fn=None, models_dict=None, default_key: str = "") -> str:
    """Select only a supported WebGPT model; local/cloud model catalogs are not used."""
    web_options = list(web_options or [])
    models_dict = models_dict or {}
    if default_key and default_key in models_dict and models_dict[default_key].get("type") == "web":
        return default_key
    if not has_internet:
        return ""
    for key, cfg in web_options:
        if cfg.get("type") == "web":
            return key
    for key in ("web_chatgpt", "web_gemini"):
        if key in models_dict:
            return key
    return ""
