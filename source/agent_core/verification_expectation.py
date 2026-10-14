"""Predeclared negative postcondition assertion; never rewrites observations."""


def validate_expectation(spec, verify):
    if not isinstance(spec, dict) or set(spec) != {'status', 'failed_checks'} or spec.get('status') != 'FAIL':
        raise ValueError('expected_verification requires status=FAIL and failed_checks')
    steps = [verify] if isinstance(verify, dict) else verify
    if not isinstance(steps, list) or not steps or any(not isinstance(step, dict) for step in steps):
        raise ValueError('expected_verification requires non-empty valid verify checks')
    indices = spec.get('failed_checks')
    if (not isinstance(indices, list) or not indices or
            any(type(index) is not int or index < 1 or index > len(steps) for index in indices) or
            len(indices) != len(set(indices))):
        raise ValueError('expected_verification.failed_checks must be unique 1-based verify indices')
    return {'status':'FAIL', 'failed_checks':sorted(indices)}


def evaluate_expectation(spec, verification_status, evidence):
    failed = [index for index, row in enumerate(evidence, 1) if row.get('passed') is not True]
    valid = bool(evidence) and all(observation_valid(row) for row in evidence)
    passed = valid and verification_status == 'FAIL' and failed == spec['failed_checks']
    return ('PASS' if passed else 'FAIL' if valid else 'SPEC_INVALID'), {
        'expected_status':spec['status'], 'observed_status':verification_status,
        'expected_failed_checks':spec['failed_checks'], 'observed_failed_checks':failed,
        'all_specs_valid':valid, 'matched':passed,
    }


def observation_valid(row):
    if not isinstance(row, dict) or row.get('spec_valid') is False or row.get('error'):
        return False
    if row.get('action') == 'run_command':
        result = row.get('result') or {}
        return (type(result.get('exit_code')) is int and result['exit_code'] == 0 and not result.get('timed_out')
                and not result.get('security_rejected') and not result.get('error')
                and result.get('process_state', 'EXITED') == 'EXITED')
    if row.get('action') == 'file_exists':
        return type(row.get('exists')) is bool
    if row.get('action') == 'file_contains':
        return type(row.get('found')) is bool
    return False
