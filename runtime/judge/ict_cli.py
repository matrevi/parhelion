#!/usr/bin/env python3
"""Multi-file submission adapter."""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

JUDGE_DIR = Path(__file__).resolve().parent
if str(JUDGE_DIR) not in sys.path:
    sys.path.insert(0, str(JUDGE_DIR))
import judge_cli as upstream
from project_files import MANIFEST, locate_root, read_source, template_files


def collect_ict_files(project_dir, template, problem_id):
    items = template_files(template)
    required = [p for p, item in items.items() if item['editable']]
    if not required:
        raise ValueError('Template has no editable files')
    root = locate_root(project_dir, required)
    manifest = root / MANIFEST
    if not manifest.is_file():
        raise ValueError('Missing .judge-project.json; download the official project first')
    if json.loads(manifest.read_text())['problem_id'] != problem_id:
        raise ValueError('Project is bound to a different problem')
    for name, item in items.items():
        if not item['editable']:
            if read_source(root, name).replace('\r\n', '\n') != item['content'].replace('\r\n', '\n'):
                raise ValueError(f'Read-only template changed: {name}')
    names = set(required)
    for directory in ('op_host', 'op_kernel', 'op_api'):
        if not any(p.startswith(directory + '/') for p in items):
            continue
        for path in (root / directory).iterdir():
            name = path.relative_to(root).as_posix()
            if path.is_file() and path.suffix in ('.h', '.cpp') and name not in items:
                names.add(name)
    result = []
    for name in sorted(names):
        content = read_source(root, name)
        if not content.strip():
            raise ValueError(f'Empty source: {name}')
        result.append({'path': name, 'content': content})
    return result


class ICTClient(upstream.JudgeClient):
    def _check_response(self, response):
        if response.status_code == 429:
            try:
                data = response.json()
            except ValueError:
                data = {}
            message = data.get('message') or data.get('msg') or '请求过于频繁'
            retry = response.headers.get('Retry-After') or data.get('retryAfter')
            suffix = f'；等待 {retry} 秒后重试' if retry else ''
            raise upstream.JudgeError(f'提交限流：{str(message)[:300]}{suffix}')
        if response.status_code >= 400 and response.status_code != 401:
            try:
                data = response.json()
            except ValueError:
                data = {}
            message = data.get('message') or data.get('msg')
            if message:
                raise upstream.JudgeError(f'HTTP {response.status_code}: {str(message)[:500]}')
        return super()._check_response(response)

    def resolve_problem(self, value):
        parts = urlsplit(value)
        # Verified canonical deployment: same contest and problem IDs as the IP site.
        if parts.scheme == 'https' and parts.netloc == '123.249.115.173':
            value = urlunsplit(('https', upstream.JUDGE_HOST, parts.path, parts.query, parts.fragment))
        return super().resolve_problem(value)

    def prepare_submission(self, problem_id, project_dir, project_type='auto'):
        problem = self.get_problem(problem_id)
        if problem.get('code_template') in ('gitcode_template', 'gitcode_template_noopapi'):
            return {'problemId': problem_id, 'userId': self.user_id,
                    'files': collect_ict_files(project_dir, self.get_template(problem_id), problem_id)}
        return super().prepare_submission(problem_id, project_dir, project_type)


if __name__ == '__main__':
    upstream.JudgeClient = ICTClient
    sys.exit(upstream.main())
