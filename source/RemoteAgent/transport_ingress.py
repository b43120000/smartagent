#!/usr/bin/env python3
"""Backward-compatible import path for the unified Agent ingress gateway."""

from agent_core.agent_gateway import (
    AgentIngressGateway,
    IngressResult,
    TransportIngressAdapter,
)

__all__ = ["AgentIngressGateway", "IngressResult", "TransportIngressAdapter"]
