"""Sealed pytest observer. Load before adding any reviewed repository imports."""
import json
import os
import sys


def main(case, packages):
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    sys.path.insert(0, packages)
    import pytest
    sys.path.insert(0, os.getcwd())
    counts = {'collected': 0, 'passed': 0, 'failed': 0, 'skipped': 0}

    class Observer:
        def pytest_collection_finish(self, session):
            counts['collected'] = len(session.items)

        def pytest_collectreport(self, report):
            if report.failed:
                counts['failed'] += 1

        def pytest_runtest_logreport(self, report):
            if report.failed:
                counts['failed'] += 1
            elif report.skipped:
                counts['skipped'] += 1
            elif report.when == 'call' and report.passed:
                counts['passed'] += 1

    arguments = ['-q', '-p', 'no:cacheprovider', '--strict-markers']
    if os.path.isabs(case.split('::', 1)[0]):
        # Independent baseline collection must not enumerate private ancestors
        # or inherit repository configuration/conftest outside its public directory.
        directory = os.path.dirname(case.split('::', 1)[0])
        arguments += ['-c', '/dev/null', '--rootdir', directory, '--confcutdir', directory]
    status = int(pytest.main([*arguments, case], plugins=[Observer()]))
    counts['pytest_exit_code'] = status
    counts['execution_uid'] = os.getuid()
    # A typed observer record accompanies an independently captured process exit.
    # No marker from the proposed script is used by the protected transaction.
    print('\nNATIVE_PYTEST_OBSERVATION=' + json.dumps(counts, sort_keys=True))
    return status
