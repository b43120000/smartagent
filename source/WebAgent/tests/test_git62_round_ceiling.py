"""Git62 PokeClaw Executor round ceiling regression.

Enforce a 1000 round execution ceiling for the default Telegram and WebCopilot
routes; retain explicit smaller budgets and independent Finish repair limits.
This test is source-only and never invokes user tools or native brokers.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_executor_uses_1000_hard_cap_with_caller_lower_bound():
    path = ROOT / "source/WebAgent/pokeclaw_executor_loop.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    runner = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "run_pokeclaw_executor")
    cap = next(n.value for n in ast.walk(runner)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "max_rounds" for t in n.targets))
    assert isinstance(cap, ast.Call)
    assert isinstance(cap.func, ast.Name) and cap.func.id == "min"
    assert len(cap.args) == 2
    assert isinstance(cap.args[1], ast.Constant) and cap.args[1].value == 1000
    assert eval(compile(ast.Expression(cap), str(path), "eval"),
                {"min": min, "int": int, "loop": type("Task", (), {"max_turns": 1200})()}) == 1000
    assert eval(compile(ast.Expression(cap), str(path), "eval"),
                {"min": min, "int": int, "loop": type("Task", (), {"max_turns": 15})()}) == 15


def test_default_web_and_telegram_loop_budget_is_1000():
    path = ROOT / "source/WebAgent/protocol_loop.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "WebAgentProtocolLoop")
    init = next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    index = [a.arg for a in init.args.kwonlyargs].index("max_turns")
    value = init.args.kw_defaults[index]
    assert isinstance(value, ast.Constant) and value.value == 1000
    for entry in ("source/WebAgent/controller.py",
                  "source/RemoteAgent/telegram_webagent_worker.py"):
        source = (ROOT / entry).read_text(encoding="utf-8")
        assert "WebAgentProtocolLoop(" in source


def test_finish_and_rejection_limits_are_unchanged():
    path = ROOT / "source/WebAgent/pokeclaw_executor_loop.py"
    text = path.read_text(encoding="utf-8")
    assert "finish_grace_rounds = 2" in text
    assert "max_finish_repairs = 2" in text
    assert "max_native_precheck_replans = 3" in text
    assert "max_consecutive_admission_rejections = 10" in text
    assert "MUTATION_REPLAY_DENIED_WITHOUT_INDEPENDENT_PROOF" in text
