#!/usr/bin/env python3
"""P0-P2 Runtime-to-Planner decision snapshot regressions (no side effects).

Use PYTHONPATH=source python -B source/WebAgent/tests/validate_planner_context_bridge_it.py
This suite does not contact GitHub, download/install anything or mutate the E: fixture.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.planner_context_snapshot import (
    build_planner_context_snapshot, render_planner_context_snapshot,
)


class FakeRuntime:
    def __init__(self):
        self.run_id = 'RR-CASE-D'
        self.task_epoch = 'EPOCH-D'
        self.authorized_paths = [r'E:\SmartAgent_PR_BCD_LIVE']
        self.tools = SimpleNamespace(_runtime_semantic_capability_resolution={
            'request_id': self.run_id, 'task_epoch': self.task_epoch,
            'capabilities': ['project.inspect_scope', 'directory.inspect_metadata'],
        })
        self.progress_ledger = SimpleNamespace(
            decision='CONTINUE', current_step=2, total_steps=5,
            strategy_state={'revision': 2, 'history': []},
        )
        self.action_loop_state = {
            'required_transition': 'REPLAN_REQUIRED',
            'allowed_next_actions': ['list_directory', 'inspect_directory', 'run_command'],
            'blockers': [{'blocking_action_id': 'scope-case-d-replan-02',
                          'reason': 'REPLAN_REQUIRED',
                          'release_conditions': [{'kind': 'NEW_OBJECTIVE_EVIDENCE_OR_RESOLVED_FIELDS'}]}],
        }
        self.action_result_ledger = {
            'scope-case-d-replan-02': {
                'tool': 'inspect_project_scope', 'result_status': 'REJECTED',
                'result_reason': 'TOOL_SCOPE_REJECTED', 'executed': False,
                'execution_status': 'FAILED', 'effective_verification_status': 'UNKNOWN',
                'produced_round': 3,
            },
            'inspect-case-d-target-replan-02': {
                'tool': 'inspect_directory', 'result_status': 'FAILED',
                'result_reason': 'DIRECTORY_INSPECTION_INCOMPLETE', 'executed': False,
                'execution_status': 'FAILED', 'effective_verification_status': 'UNKNOWN',
                'produced_round': 3,
            },
        }
        self.action_ledger = {
            'scope-case-d-replan-02': {
                'result': '[TOOL_SCOPE_REJECTED] resolved_scope_expands_beyond_explicit_path:'
                          + r'E:\SmartAgent_PR_BCD_LIVE' + '\nPRIVATE_SECRET_NEVER_SEND',
            },
            'inspect-case-d-target-replan-02': {'result': 'DIRECTORY_INSPECTION_INCOMPLETE'},
        }
        self.condition_result_ledger = {'COND-A': {
            'condition_id': 'COND-A', 'action_id': 'scope-case-d-replan-02', 'status': 'UNKNOWN',
        }}
        self._environment_fact_refs = {}
        self._planner_context_diagnostics = []
        self.pending_result_recovery = {}
        self.pending_progress_repair_actions = []

    def _context_capabilities(self):
        return {'read_tools': ['list_directory', 'inspect_directory'],
                'objective_blockers': [{'action_id': 'scope-case-d-replan-02',
                                        'reason': 'TOOL_SCOPE_REJECTED'}]}


class PlannerContextBridgeIT(unittest.TestCase):
    def setUp(self):
        self.runtime = FakeRuntime()

    def test_case_d_scope_failure_preserved_with_correct_execution_facts(self):
        snapshot = build_planner_context_snapshot(self.runtime)
        self.assertEqual(snapshot['schema'], 'SMARTAGENT_PLANNER_CONTEXT_V1')
        self.assertEqual(snapshot['goal']['strategy_revision'], 2)
        rows = {row['action_id']: row for row in snapshot['recent_action_evidence']}
        rejected = rows['scope-case-d-replan-02']
        self.assertFalse(rejected['executed'])
        self.assertEqual(rejected['result_reason'], 'TOOL_SCOPE_REJECTED')
        self.assertEqual(rejected['scope_error_code'],
                         'resolved_scope_expands_beyond_explicit_path')
        self.assertEqual(rows['inspect-case-d-target-replan-02']['verification_status'], 'UNKNOWN')
        self.assertFalse(snapshot['scope']['scope_expansion_authorized'])

    def test_scr_candidate_is_not_falsely_advertised_as_admitted(self):
        snapshot = build_planner_context_snapshot(self.runtime)
        readiness = {row['tool']: row for row in
                     snapshot['capability_resolution']['tool_readiness']}
        self.assertEqual(readiness['list_directory']['read_capability'], 'directory.list')
        self.assertEqual(readiness['list_directory']['capability_state'], 'SELECTION_REQUIRED')
        self.assertEqual(readiness['inspect_directory']['capability_state'], 'SELECTED')
        self.assertEqual(readiness['run_command']['action_admission'],
                         'REQUIRES_RUNTIME_ADMISSION')
        self.assertFalse(readiness['list_directory']['selection_can_expand_authorization'])
        self.assertEqual(snapshot['capability_resolution']['selected_capabilities'],
                         ['directory.inspect_metadata', 'project.inspect_scope'])

    def test_protocol_reject_does_not_erase_prior_tool_failures(self):
        self.runtime._planner_context_diagnostics = [{
            'reason': 'invalid_progress_payload',
            'field_issues': [{'field': 'outcome', 'expected': 'INTERRUPT requires FAILED | UNKNOWN',
                              'actual': 'PENDING'}],
            'preserved_action_ids': ['ACT-HELD'],
        }]
        self.runtime.pending_progress_repair_actions = [{'action_id': 'ACT-HELD'}]
        for _ in range(3):
            snapshot = build_planner_context_snapshot(self.runtime)
            self.assertEqual(snapshot['latest_protocol_diagnostics'][0]['field_issues'][0]['field'], 'outcome')
            self.assertNotIn('actual', snapshot['latest_protocol_diagnostics'][0]['field_issues'][0])
            self.assertEqual(snapshot['recovery']['pending_progress_action_ids'], ['ACT-HELD'])
            self.assertTrue(any(row['result_reason'] == 'TOOL_SCOPE_REJECTED'
                                for row in snapshot['recent_action_evidence']))
            self.runtime.progress_ledger.current_step += 1

    def test_snapshot_is_bounded_and_never_includes_raw_result(self):
        rendered, payload = render_planner_context_snapshot(self.runtime)
        self.assertEqual(rendered.count('[SMARTAGENT_PLANNER_CONTEXT_V1]'), 1)
        self.assertNotIn('PRIVATE_SECRET_NEVER_SEND', rendered)
        self.assertTrue(payload['context_digest'])
        self.assertFalse(payload['raw_tool_results_included'])
        self.assertEqual(build_planner_context_snapshot(self.runtime)['context_digest'],
                         payload['context_digest'])

    def test_request_epoch_binding_mismatch_is_explicit(self):
        self.runtime.tools._runtime_semantic_capability_resolution['task_epoch'] = 'OLD'
        snapshot = build_planner_context_snapshot(self.runtime)
        self.assertTrue(snapshot['capability_resolution']['binding_identity_mismatch'])
        self.assertFalse(snapshot['capability_resolution']['binding_present'])
        self.assertEqual(snapshot['capability_resolution']['tool_readiness'][0]['capability_state'],
                         'BINDING_IDENTITY_MISMATCH')

    def test_old_active_blockers_are_reported_when_truncated(self):
        self.runtime.action_result_ledger.clear()
        self.runtime.action_ledger.clear()
        for n in range(60):
            aid = f'ACT-{n:03d}'
            self.runtime.action_result_ledger[aid] = {
                'tool': 'inspect_directory', 'result_status': 'REJECTED',
                'result_reason': 'TOOL_SCOPE_REJECTED', 'executed': False,
                'execution_status': 'FAILED', 'effective_verification_status': 'UNKNOWN',
                'evidence_status': 'ACTIVE',
            }
        snapshot = build_planner_context_snapshot(self.runtime)
        self.assertLessEqual(len(snapshot['recent_action_evidence']), 24)
        self.assertGreater(snapshot['omitted_active_blocker_count'], 0)
        self.assertTrue(snapshot['critical_context_omitted'])
        self.assertGreater(len(snapshot['omitted_active_blocker_refs']), 0)

    def test_environment_and_condition_evidence_present_without_claiming_success(self):
        self.runtime._environment_fact_refs = {'ENVFACT-X': {
            'fact': {'state': 'MISSING', 'probe_status': 'VERIFIED'},
        }}
        snapshot = build_planner_context_snapshot(self.runtime)
        self.assertEqual(snapshot['environment_facts'][0]['fact_state'], 'MISSING')
        self.assertEqual(snapshot['condition_evidence'][0]['status'], 'UNKNOWN')
        self.assertEqual(snapshot['goal']['status'], 'INCOMPLETE')


class PlannerDeliveryIT(unittest.TestCase):
    def test_runtime_rejection_next_round_receives_a_fresh_compiled_context(self):
        from WebAgent.protocol_loop import WebAgentProtocolLoop
        prompts, events = [], []
        with tempfile.TemporaryDirectory(prefix='planner-ctx-') as folder:
            root = Path(folder).resolve()
            allowed = root / 'allowed'
            allowed.mkdir()
            def planner(prompt, expected, attachments):
                prompts.append(prompt)
                if len(prompts) == 1:
                    return 'INVALID_V9_TOOL_TRANSPORT'
                raise RuntimeError('STOP_AFTER_CONTEXT_CAPTURE')
            loop = WebAgentProtocolLoop(
                allowed, planner, progress_root=root, max_turns=4,
                event_sink=lambda name, **kw: events.append((name, kw)),
            )
            with patch.dict(os.environ, {
                'SMARTAGENT_GAR_V1': '0',
                'SMARTAGENT_PREREQ_V3_BCD': '0',
                'SMARTAGENT_PREREQ_V3_PR_A': '0',
            }):
                with self.assertRaisesRegex(RuntimeError, 'STOP_AFTER_CONTEXT_CAPTURE'):
                    loop.run('請唯讀檢查已授權資料夾', request_id='RR-PLANNER-CONTEXT-IT',
                             task_id='TASK-PLANNER-CONTEXT-IT', task_epoch='EPOCH-CTX')
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:
            self.assertIn('[SMARTAGENT_PLANNER_CONTEXT_V1]', prompt)
            self.assertIn('"context_digest"', prompt)
        self.assertIn('latest_protocol_diagnostics', prompts[1])
        prepared = [event for event in events if event[0] == 'planner_context_prepared']
        self.assertEqual(len(prepared), 2)
        self.assertEqual(len({row[1]['context_digest'] for row in prepared}), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
