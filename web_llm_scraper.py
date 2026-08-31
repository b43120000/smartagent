#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backward-compatible import shim.

The implementation moved to ``agent_core.web_runtime`` in Agent V1 phase 1.
Existing callers may keep ``import web_llm_scraper`` until they are migrated.
"""
from agent_core.web_runtime import *  # noqa: F401,F403
from agent_core.web_runtime import SERVICE_CONFIG, PROFILE_DIR, WebScraperStageError, WebLLMScraper, ScraperManager, get_manager

if __name__ == "__main__":
    import runpy
    runpy.run_module("agent_core.web_runtime", run_name="__main__")
