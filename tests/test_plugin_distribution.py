"""Build/install smoke: proves the examples work without an editable checkout."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


SMOKE = r'''
import json, pathlib, sys, time
import agent_hub
from agent_hub.config import adapters, private_dir
from agent_hub.service import Service
root = pathlib.Path(sys.argv[1])
assert pathlib.Path(agent_hub.__file__).is_relative_to(pathlib.Path(sys.argv[2]))
config = {'agents': {
    'writer': {'kind': 'plugin', 'plugin': 'echo', 'options': {'prefix': 'hello:'}},
    'second': {'kind': 'plugin', 'plugin': 'echo', 'options': {'prefix': 'second:'}},
    'bridge': {'kind': 'command', 'args': ['-m', 'agent_hub_echo', '{request_file}'],
               'resume_args': ['-m', 'agent_hub_echo', '{request_file}'],
               'env_from': {'PYTHONPATH': 'HUB_SMOKE_PACKAGES'}}}}
def wait(service, task):
    for _ in range(200):
        result = service.call('get_result', {'task_id': task['id']})
        if result['ready']:
            assert result['state'] == 'succeeded', result
            return result
        time.sleep(.03)
    raise AssertionError('task did not finish')
for device in ('device-one', 'device-two'):
    data = root / device / 'private-state'
    work = root / device / 'workspace with spaces'
    work.mkdir(parents=True)
    private_dir(data)
    (data / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    service = Service(data, adapters(data))
    try:
        assert set(service.call('list_agents', {})) == {'writer', 'second', 'bridge'}
        for name, prefix in (('writer', 'hello:'), ('second', 'second:'), ('bridge', '')):
            task = service.call('submit_task', dict(request_id=name, agent=name, prompt='中文 payload', cwd=str(work), mode='delegate'))
            assert service.call('submit_task', dict(request_id=name, agent=name, prompt='中文 payload', cwd=str(work), mode='delegate'))['id'] == task['id']
            first = wait(service, task)
            assert first['answer'] == prefix + '中文 payload', first
            child = service.call('continue_task', dict(request_id=name+'-next', task_id=task['id'], prompt='next'))
            second = wait(service, child)
            assert second['session_id'] == first['session_id']
            assert second['answer'] == prefix + 'next'
        assert not list(data.rglob('request-*.json'))
        assert (data / 'plugins' / 'writer').is_dir()
        assert (data / 'plugins' / 'second').is_dir()
    finally:
        service.close()
print(json.dumps({'devices': 2, 'completed_tasks': 12, 'installed_package': True}))
'''


class DistributionTests(unittest.TestCase):
    def test_external_distribution_and_relocated_hub(self):
        project = Path(__file__).resolve().parents[1]
        # This is a source test: build tools are developer/CI requirements only.
        self.assertIsNotNone(importlib.util.find_spec('setuptools'))
        example = project / 'examples' / 'echo-plugin'
        self.assertTrue((example / 'pyproject.toml').is_file(), 'Standalone plugin package must be shipped')
        with tempfile.TemporaryDirectory(prefix='hub packaging ') as folder:
            root = Path(folder)
            build = root / 'source'
            build.mkdir()
            for name in ('pyproject.toml', 'MANIFEST.in', 'README.md', 'README.zh-CN.md', 'config.example.json'):
                shutil.copy2(project / name, build / name)
            shutil.copytree(project / 'src', build / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))
            shutil.copytree(example, build / 'examples' / 'echo-plugin',
                            ignore=shutil.ignore_patterns('__pycache__', '*.egg-info', 'build', 'dist'))
            (build / 'docs').mkdir()
            shutil.copy2(project / 'docs' / 'executor-plugins.md', build / 'docs' / 'executor-plugins.md')
            # Catch accidental inclusion of per-device files by the sdist manifest.
            for name in ('config.json', 'tasks.db', 'cookies.json', 'debug.log'):
                (build / name).write_text('PERSONAL_DATA_SENTINEL', encoding='utf-8')
            wheels = root / 'wheels'; wheels.mkdir()
            env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
            env.pop('PYTHONPATH', None)

            def run(args, cwd, child_env=env, timeout=90):
                result = subprocess.run([sys.executable, *args], cwd=cwd, env=child_env,
                                        capture_output=True, encoding='utf-8', errors='replace', timeout=timeout)
                self.assertEqual(result.returncode, 0, result.stdout[-5000:] + result.stderr[-5000:])
                return result.stdout

            builder = 'import sys; from setuptools.build_meta import build_wheel,build_sdist; build_wheel(sys.argv[1]); build_sdist(sys.argv[1])'
            run(['-c', builder, str(wheels)], build)
            run(['-c', builder, str(wheels)], build / 'examples' / 'echo-plugin')
            for archive in wheels.glob('*.whl'):
                with zipfile.ZipFile(archive) as package:
                    for member in package.namelist():
                        self.assertNotIn(Path(member).name, {'config.json', 'tasks.db', 'cookies.json', 'debug.log'})
                        self.assertNotIn(b'PERSONAL_DATA_SENTINEL', package.read(member))
            for archive in wheels.glob('*.tar.gz'):
                with tarfile.open(archive) as package:
                    for member in package.getmembers():
                        if member.isfile():
                            self.assertNotIn(Path(member.name).name, {'config.json', 'tasks.db', 'cookies.json', 'debug.log'})
                            self.assertNotIn(b'PERSONAL_DATA_SENTINEL', package.extractfile(member).read())
            installed = root / 'relocated packages'
            run(['-m', 'pip', 'install', '--no-deps', '--no-index', '--no-compile', '--target', str(installed),
                 *[str(p) for p in wheels.glob('*.whl')]], root)
            # Installed code, plugin metadata and module wrapper must work with no sources.
            shutil.rmtree(build)
            isolated = dict(env, PYTHONPATH=str(installed), HUB_SMOKE_PACKAGES=str(installed))
            output = run(['-c', SMOKE, str(root / 'runtime'), str(installed)], root, isolated)
            self.assertEqual(json.loads(output), {'devices': 2, 'completed_tasks': 12, 'installed_package': True})
            self.assertFalse(list(installed.rglob('tasks.db')))
            self.assertFalse(list(installed.rglob('request-*.json')))


if __name__ == '__main__':
    unittest.main()
