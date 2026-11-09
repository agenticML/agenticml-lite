"""Convert describe-task-definition JSON to registration input, changing one image."""
import argparse
import copy
import json
import sys

RESPONSE_ONLY = {
    'taskDefinitionArn', 'revision', 'status', 'requiresAttributes',
    'compatibilities', 'registeredAt', 'registeredBy', 'deregisteredAt',
}


def swap_image(document, image, container='server'):
    task = copy.deepcopy(document.get('taskDefinition', document))
    for field in RESPONSE_ONLY:
        task.pop(field, None)
    matches = [entry for entry in task['containerDefinitions'] if entry['name'] == container]
    if len(matches) != 1:
        raise ValueError('expected exactly one container named ' + container)
    matches[0]['image'] = image
    return task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('--container', default='server')
    args = parser.parse_args()
    json.dump(swap_image(json.load(sys.stdin), args.image, args.container), sys.stdout)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
