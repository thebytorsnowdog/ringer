"""Disposable execution adapter. Never assigns a correctness score."""
import base64
import copy
import importlib.util
import json
import subprocess
import sys

sys.dont_write_bytecode = True


def decode(value):
    if isinstance(value, dict):
        if set(value) == {'__bytes__'}:
            return base64.b64decode(value['__bytes__'])
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


def main():
    request = json.load(sys.stdin)
    args = decode(request['args'])
    before = copy.deepcopy(args)
    calls = []
    original_popen = subprocess.Popen

    def guarded_popen(command, *positional, **kwargs):
        calls.append({'list': isinstance(command, list), 'shell_false': kwargs.get('shell') is False,
                      'same_command': command == args[0]})
        if not all(calls[-1].values()):
            raise ValueError('argv contract: explicit list and shell=False required')
        return original_popen(command, *positional, **kwargs)

    if request['op'] == 'argv':
        subprocess.Popen = guarded_popen
    try:
        spec = importlib.util.spec_from_file_location('candidate', sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        function = getattr(module, request['function'])
        if request['op'] == 'defaults':
            first = function()
            second = function(None)
            initial = first == [] and second == []
            distinct = first is not second
            first.append('mutated')
            third = function()
            supplied = ['keep']
            output = {'initial_empty': initial, 'distinct': distinct, 'second': second,
                      'third': third, 'supplied_identity': function(supplied) is supplied,
                      'supplied': supplied}
        elif request['op'] == 'fresh_list':
            items = function(*args)
            output = {'items': items, 'fresh_list': isinstance(items, list) and items is not args[0]}
        elif request['op'] == 'merge_alias':
            output = function(*args)
            output['ui']['tabs'].append('changed')
            output['engines']['a']['labels'].append('changed')
            output = 'mutated output'
        else:
            output = function(*args)
        result = {'value': output, 'exception': None}
    except BaseException as exc:
        result = {'value': None, 'exception': type(exc).__name__}
    result.update(input_preserved=args == before, calls=calls)
    print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    main()
