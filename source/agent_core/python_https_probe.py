"""Exact positive grammar for read-only Python urllib HTTPS diagnostic commands.

Not a general python -c approval. Native Broker, scope, ACL, executable trust,
and network enforcement are unchanged. No network activity occurs here.
"""
from __future__ import annotations

import ast
import ipaddress
import re
from urllib.parse import urlsplit

_PYTHON_PROBE_COMMAND = re.compile(
    r'(?is)^\s*(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?'
    r'\s+-c\s+"(?P<code>[^"$\r\n\\]*)"\s*$'
)
_PUBLIC_HOST = re.compile(
    r'(?i)^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    r'(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$'
)


def _name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _attr(node: ast.AST, variable: str, field: str) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == field and _name(node.value, variable)


def _https_get(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or len(node.args) != 1 or len(node.keywords) != 1:
        return False
    func = node.func
    if not (
        isinstance(func, ast.Attribute) and func.attr == "urlopen"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "request"
        and _name(func.value.value, "urllib")
    ):
        return False
    url = node.args[0]
    if not isinstance(url, ast.Constant) or not isinstance(url.value, str) or len(url.value) > 512:
        return False
    try:
        parsed = urlsplit(url.value)
        host = parsed.hostname or ""
        if (
            parsed.scheme.lower() != "https" or not parsed.netloc
            or not _PUBLIC_HOST.fullmatch(host)
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)
            or host.endswith((".local", ".internal", ".localhost", ".test"))
            or len(host) > 253
        ):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
    except ValueError:
        return False
    kw = node.keywords[0]
    return (
        kw.arg == "timeout" and isinstance(kw.value, ast.Constant)
        and isinstance(kw.value.value, int)
        and not isinstance(kw.value.value, bool)
        and 1 <= kw.value.value <= 30
    )


def _bounded_print(node: ast.AST, variable: str) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    printing = node.value
    if not _name(printing.func, "print") or len(printing.args) != 2 or printing.keywords:
        return False
    if not _attr(printing.args[0], variable, "status"):
        return False
    length = printing.args[1]
    if not isinstance(length, ast.Call) or not _name(length.func, "len") or len(length.args) != 1 or length.keywords:
        return False
    read = length.args[0]
    if (
        not isinstance(read, ast.Call) or not _attr(read.func, variable, "read")
        or len(read.args) != 1 or read.keywords
    ):
        return False
    n = read.args[0]
    return (
        isinstance(n, ast.Constant) and isinstance(n.value, int)
        and not isinstance(n.value, bool) and 1 <= n.value <= 1024
    )


def is_bounded_python_https_probe(command: str) -> bool:
    """Only import urllib.request; response=urlopen(https,timeout); print(status,len(read))."""
    if not isinstance(command, str) or len(command) > 600 or chr(96) in command:
        return False
    match = _PYTHON_PROBE_COMMAND.fullmatch(command)
    if not match:
        return False
    try:
        body = ast.parse(match.group("code"), mode="exec").body
    except (SyntaxError, ValueError, TypeError):
        return False
    if len(body) != 3:
        return False
    imported, bound, output = body
    if not (
        isinstance(imported, ast.Import) and len(imported.names) == 1
        and imported.names[0].name == "urllib.request"
        and imported.names[0].asname is None
        and isinstance(bound, ast.Assign) and len(bound.targets) == 1
        and isinstance(bound.targets[0], ast.Name)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,20}", bound.targets[0].id)
        and _https_get(bound.value)
        and _bounded_print(output, bound.targets[0].id)
    ):
        return False
    return True


__all__ = ["is_bounded_python_https_probe"]
