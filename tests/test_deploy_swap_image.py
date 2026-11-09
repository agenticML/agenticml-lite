import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('swap_image', Path(__file__).parents[1] / 'deploy/aws/swap_image.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_registration_changes_only_server_image():
    task = {
        'family': 'hosted', 'cpu': '512', 'memory': '1024',
        'executionRoleArn': 'execution', 'taskRoleArn': 'task',
        'volumes': [{'name': 'data', 'efsVolumeConfiguration': {'fileSystemId': 'fs-1'}}],
        'containerDefinitions': [
            {'name': 'server', 'image': 'old', 'environment': [{'name': 'X', 'value': 'y'}],
             'secrets': [{'name': 'PGPASSWORD', 'valueFrom': 'secret:password::'}], 'stopTimeout': 120},
            {'name': 'sidecar', 'image': 'unchanged'},
        ],
        **{name: 'response-only' for name in module.RESPONSE_ONLY},
    }
    original = copy.deepcopy(task)
    expected = {k: v for k, v in copy.deepcopy(task).items() if k not in module.RESPONSE_ONLY}
    expected['containerDefinitions'][0]['image'] = 'new'
    assert module.swap_image({'taskDefinition': task}, 'new') == expected
    assert task == original
    assert module.swap_image(task, 'new') == expected


@pytest.mark.parametrize('containers', [[], [{'name': 'other'}], [{'name': 'server'}, {'name': 'server'}]])
def test_missing_or_ambiguous_container_fails(containers):
    with pytest.raises(ValueError, match='exactly one'):
        module.swap_image({'containerDefinitions': containers}, 'new')
