"""Private supervised worker entry point. No business/provider/model authority."""
import json
from pathlib import Path
import sys

from .worker import execute


def main():
    work = Path(sys.argv[1]).resolve(strict=True)
    job = json.loads((work/'job.json').read_text(encoding='utf-8'))
    decoded = []
    for row in job['rows']:
        record = {'id': row['id']}
        for side in ('old', 'new') if 'new' in row else ('old',):
            path = (work/row[side]).resolve(strict=True)
            if path.parent != work:
                raise ValueError('job_path_scope')
            record[side] = path.read_bytes()
        decoded.append(record)
    pack, metrics = execute(decoded, job['policy'], work/'scratch')
    for name, content in pack.items():
        (work/name).write_bytes(content)
    (work/'metrics.json').write_text(json.dumps(metrics), encoding='utf-8')


if __name__ == '__main__':
    main()
