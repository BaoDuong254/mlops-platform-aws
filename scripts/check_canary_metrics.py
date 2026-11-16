"""Execute real PromQL regression tests against queries from the AnalysisTemplate."""
import os
import subprocess
import tempfile
from pathlib import Path

import yaml


def main():
    gate=next(yaml.safe_load_all(Path('deploy/gitops/inference/analysis-template.yaml').read_text()))
    rules=[]
    names={'error-rate':'canary_error_rate','canary-request-rate':'canary_request_rate'}
    for metric in gate['spec']['metrics']:
        if metric['name'] in names:
            query=metric['provider']['prometheus']['query'].replace('{{args.canary-hash}}','canary')
            rules.append({'record':names[metric['name']],'expr':query})
    with tempfile.TemporaryDirectory(prefix='promql-check-') as temp:
        root=Path(temp)
        (root/'rules.yaml').write_text(yaml.safe_dump({'groups':[{'name':'canary','rules':rules}]}))
        (root/'tests.yaml').write_text(Path('tests/prometheus/canary-gates.test.yaml').read_text())
        subprocess.run(['docker','run','--rm','--network','none','--user',f'{os.getuid()}:{os.getgid()}',
                        '-v',f'{root}:/checks:ro','--entrypoint','/bin/promtool','prom/prometheus:v3.15.1',
                        'test','rules','/checks/tests.yaml'],check=True)


if __name__=='__main__':
    main()
