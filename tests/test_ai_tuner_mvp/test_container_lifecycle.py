import copy

import pytest

from ai_tuner.ops.container_inventory import partition


def sample(name='probe', ident='a', state='exited', code=0):
    return {'Id': ident * 64, 'Name': '/' + name, 'Image': 'sha256:' + 'd' * 64,
            'Created': '2026-11-28T00:00:00Z',
            'Config': {'Entrypoint': ['python'], 'Cmd': ['-B', '-']},
            'State': {'Status': state, 'ExitCode': code, 'OOMKilled': False, 'Error': '',
                      'FinishedAt': '2026-11-28T00:01:00Z', 'StartedAt': '2026-11-28T00:00:01Z',
                      'Health': {'Status': 'healthy'}},
            'HostConfig': {'RestartPolicy': {'Name': 'no'}}}


def entry(item, lifecycle='completed_task'):
    from ai_tuner.ops.container_inventory import lifecycle_entry
    return lifecycle_entry(item, successor='live', lifecycle=lifecycle,
                           reason='已核验发布结果', evidence='release-evidence.json#successful',
                           result={'success': True, 'error_count': 0, 'logs_sha256': 'f' * 64})


def test_successful_task_requires_explicit_identity_registration():
    task, live = sample(), sample('live', 'b', 'running')
    unknown = sample('unregistered-probe', 'c')
    registry = {'version': 1, 'entries': [entry(task)]}
    assert partition([task, live, unknown], registry) == ([live, unknown], [task], [])


@pytest.mark.parametrize('field,value', [
    ('ExitCode', 1), ('OOMKilled', True), ('Error', 'execution failed'),
    ('Status', 'running'), ('FinishedAt', '2026-11-28T00:02:00Z'),
])
def test_completed_task_drift_is_never_hidden(field, value):
    task, live = sample(), sample('live', 'b', 'running')
    registry = {'version': 1, 'entries': [entry(task)]}
    task['State'][field] = value
    active, retired, issues = partition([task, live], registry)
    assert task in active and retired == [] and issues


@pytest.mark.parametrize('change', ['image', 'command', 'restart', 'successor_health', 'successor_stopped'])
def test_explicit_lifecycle_requires_unchanged_source_and_healthy_successor(change):
    task, live = sample(), sample('live', 'b', 'running')
    registry = {'version': 1, 'entries': [entry(task)]}
    if change == 'image': task['Image'] = 'sha256:' + 'e' * 64
    if change == 'command': task['Config']['Cmd'] = ['run-trading']
    if change == 'restart': task['HostConfig']['RestartPolicy']['Name'] = 'always'
    if change == 'successor_health': live['State']['Health']['Status'] = 'unhealthy'
    if change == 'successor_stopped': live['State']['Status'] = 'exited'
    active, retired, issues = partition([task, live], registry)
    assert task in active and retired == [] and issues


def test_created_standby_can_be_registered_but_starting_it_invalidates_exclusion():
    item, live = sample(state='created'), sample('live', 'b', 'running')
    item['State']['StartedAt'] = '0001-01-01T00:00:00Z'
    registry = {'version': 1, 'entries': [entry(item, 'standby')]}
    assert partition([item, live], registry) == ([live], [item], [])
    item['State']['Status'] = 'running'
    assert partition([item, live], registry)[2]


def test_reviewed_retired_exit_is_distinct_from_successful_task():
    item, live = sample(code=137), sample('live', 'b', 'running')
    registry = {'version': 1, 'entries': [entry(item, 'retired')]}
    assert partition([item, live], registry) == ([live], [item], [])
    with pytest.raises(ValueError): entry(item, 'completed_task')


@pytest.mark.parametrize('missing', ['image', 'command_sha256', 'finished_at', 'evidence'])
def test_partial_lifecycle_registration_fails_closed(missing):
    task = sample()
    row = entry(task)
    del row[missing]
    with pytest.raises(ValueError): partition([], {'version': 1, 'entries': [row]})


def test_absent_registered_task_still_requires_successor_health():
    task, live = sample(), sample('live', 'b', 'exited')
    assert partition([live], {'version': 1, 'entries': [entry(task)]})[2]


def test_planner_rejects_failed_task_and_does_not_change_existing_registry():
    from scripts.ops.plan_container_lifecycle import plan
    task, live = sample(code=2), sample('live', 'b', 'running')
    registry = {'version': 1, 'entries': []}
    request = {'id': task['Id'], 'name': 'probe', 'successor': 'live',
               'lifecycle': 'completed_task', 'reason': '发布探针', 'evidence': 'probe.json',
               'result': {'success': True, 'error_count': 0, 'logs_sha256': 'f' * 64}}
    before = copy.deepcopy(registry)
    with pytest.raises(ValueError): plan([task, live], registry, [request])
    assert registry == before


def test_planner_pins_snapshot_id_and_rejects_duplicate_requests():
    from scripts.ops.plan_container_lifecycle import plan
    task, live = sample(), sample('live', 'b', 'running')
    request = {'id': task['Id'], 'name': 'probe', 'successor': 'live',
               'lifecycle': 'completed_task', 'reason': '发布探针', 'evidence': 'probe.json',
               'result': {'success': True, 'error_count': 0, 'logs_sha256': 'f' * 64}}
    assert plan([task, live], {'version': 1, 'entries': []}, [request])['entries'][0]['id'] == task['Id']
    with pytest.raises(ValueError): plan([task, live], {'version': 1, 'entries': []}, [request, request])
    request['id'] = 'e' * 64
    with pytest.raises(ValueError): plan([task, live], {'version': 1, 'entries': []}, [request])


def failed_entry(item):
    from ai_tuner.ops.container_inventory import lifecycle_entry
    return lifecycle_entry(item, successor='live', lifecycle='archived_failed_task',
                           reason='旧预检已结束，显式保留失败归档', evidence='failure-log.json',
                           result={'success': False, 'error_count': 2, 'logs_sha256': 'f' * 64},
                           failure_summary='ModuleNotFoundError: apscheduler',
                           failure_observed_at='2026-11-27T09:06:22+00:00')


def test_archived_failure_is_explicit_and_remains_visible_as_history():
    from ai_tuner.ops.container_inventory import historical_failures
    task, live = sample(code=0), sample('live', 'b', 'running')
    registry = {'version': 1, 'entries': [failed_entry(task)]}
    assert partition([task, live], registry) == ([live], [task], [])
    history = historical_failures(registry)
    assert len(history) == 1
    assert history[0]['failure_summary'] == 'ModuleNotFoundError: apscheduler'
    assert history[0]['result']['success'] is False
    assert partition([task, live], {'version': 1, 'entries': []}) == ([task, live], [], [])
    task['State']['Status'] = 'running'
    assert partition([task, live], registry)[2]


@pytest.mark.parametrize('missing', ['result', 'failure_summary', 'failure_observed_at'])
def test_archived_failure_requires_proof_and_date(missing):
    row = failed_entry(sample())
    del row[missing]
    with pytest.raises(ValueError): partition([], {'version': 1, 'entries': [row]})


@pytest.mark.parametrize('result', [None, {'success': True, 'error_count': 2, 'logs_sha256': 'f' * 64},
                                    {'success': False, 'error_count': 0, 'logs_sha256': 'f' * 64}])
def test_zero_exit_code_does_not_override_failed_or_missing_result(result):
    from scripts.ops.plan_container_lifecycle import plan
    task, live = sample(), sample('live', 'b', 'running')
    request = {'id': task['Id'], 'name': 'probe', 'successor': 'live',
               'lifecycle': 'completed_task', 'reason': '发布探针', 'evidence': 'probe.json', 'result': result}
    with pytest.raises(ValueError): plan([task, live], {'version': 1, 'entries': []}, [request])
