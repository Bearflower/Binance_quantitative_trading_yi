"""Bind build artifacts to the reviewed production bases and four consumers."""
import argparse
import hashlib
import json
from pathlib import Path
import re


ORDER = ('ai-tuner-snapshot', 'ai-tuner',
         'trading_system-hrs-acceptance', 'trading_system-hrs')


def prepare(root, artifacts, source, output):
    if not re.fullmatch(r'[0-9a-f]{40}', source):
        raise ValueError('source_sha')
    plan = json.loads((root / 'deploy/history-tolerance/bases.json').read_text())
    receipts = {}
    for group in plan['groups']:
        name = group['name']
        receipt = json.loads((artifacts / f'history-tolerance-{name}' / 'source.json').read_text())
        if (receipt['source'] != source or receipt['base'] != group['base']
                or receipt['repository'] != group['repository']
                or receipt['containers'] != group['containers']
                or set(receipt['hashes']) != set(group['files'])
                or any(receipt['hashes'][path] != hashlib.sha256((root / path).read_bytes()).hexdigest()
                       for path in group['files'])
                or not re.fullmatch(re.escape(group['repository']) + r'@sha256:[0-9a-f]{64}',
                                    receipt.get('digest_ref', ''))):
            raise ValueError(f'unreviewed_artifact:{name}')
        receipts[name] = receipt
    if len({receipt['deploy_id'] for receipt in receipts.values()}) != 1:
        raise ValueError('mixed_build_runs')
    by_container = {container: receipt for receipt in receipts.values()
                    for container in receipt['containers']}
    if set(by_container) != set(ORDER):
        raise ValueError('consumer_scope')
    services = []
    for name in ORDER:
        receipt = by_container[name]
        services.append(dict(name=name, service=name, group=receipt['name'],
                             image='sha256:' + receipt['base'].split('@sha256:')[1],
                             tag=receipt['digest_ref'], digest_ref=receipt['digest_ref'],
                             hashes=receipt['hashes']))
    output.mkdir(parents=True, exist_ok=False)
    (output / 'plan.json').write_text(json.dumps(dict(source=source,
        deploy_id=receipts['hrs']['deploy_id'], services=services),
        sort_keys=True, indent=2) + '\n')
    (output / 'activate.py').write_bytes(
        (root / 'deployment/history-live-gap/activate.py').read_bytes())
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.root, args.artifacts, args.source, args.output))
