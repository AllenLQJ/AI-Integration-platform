import json
import os
from contextlib import contextmanager
import select
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch
from agent_hub.daemon import make_server
from agent_hub.models import HubError
from agent_hub.service import Service


def chrome_path():
    candidates = [os.environ.get('AGENT_HUB_CHROME'),
                  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                  '/Applications/Chromium.app/Contents/MacOS/Chromium',
                  shutil.which('google-chrome'), shutil.which('chromium'), shutil.which('chromium-browser')]
    return next((c for c in candidates if c and Path(c).exists()), None)


class Chrome:
    """Headless Chrome driven over --remote-debugging-pipe (fd 3 in, fd 4 out, NUL-delimited JSON)."""
    def __init__(self, executable, profile):
        import fcntl
        to_chrome, from_chrome = os.pipe(), os.pipe()
        def child():
            read = fcntl.fcntl(to_chrome[0], fcntl.F_DUPFD, 10)
            write = fcntl.fcntl(from_chrome[1], fcntl.F_DUPFD, 10)
            os.dup2(read, 3); os.dup2(write, 4)
        self.process = subprocess.Popen(
            [executable, '--headless=new', '--remote-debugging-pipe', '--user-data-dir=' + profile, '--no-first-run',
             '--no-default-browser-check', '--disable-gpu', '--disable-extensions', 'about:blank'],
            preexec_fn=child, close_fds=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(to_chrome[0]); os.close(from_chrome[1])
        self.output, self.input = to_chrome[1], from_chrome[0]
        self.buffer, self.ident = b'', 0
        # A cold Chrome on a busy CI runner can take longer than one command's budget to answer.
        # Wait for it once here, generously, so the tests' own 20s budgets measure the page.
        try:
            self.send('Browser.getVersion', timeout=90)
        except BaseException:
            self.close()
            raise

    def send(self, method, params=None, session=None, timeout=20):
        self.ident += 1
        message = dict(id=self.ident, method=method, params=params or {})
        if session:
            message['sessionId'] = session
        data = json.dumps(message).encode() + b'\0'
        while data:
            data = data[os.write(self.output, data):]
        deadline = time.monotonic() + timeout
        while True:
            while b'\0' not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Chrome DevTools 无响应：' + method)
                if select.select([self.input], [], [], remaining)[0]:
                    chunk = os.read(self.input, 1 << 20)
                    if not chunk:
                        raise RuntimeError('Chrome 已退出')
                    self.buffer += chunk
            raw, self.buffer = self.buffer.split(b'\0', 1)
            reply = json.loads(raw)
            if reply.get('id') == self.ident:
                if 'error' in reply:
                    raise RuntimeError(method + ': ' + json.dumps(reply['error'], ensure_ascii=False))
                return reply.get('result', {})

    def evaluate(self, session, expression):
        result = self.send('Runtime.evaluate', dict(expression=expression, returnByValue=True), session)
        if 'exceptionDetails' in result:
            raise RuntimeError(json.dumps(result['exceptionDetails'], ensure_ascii=False))
        return result['result'].get('value')

    def close(self):
        try:
            self.send('Browser.close', timeout=5)
        except (OSError, RuntimeError, TimeoutError):
            pass  # Chrome 可能在回复前退出；下面以进程退出为准。
        try:
            self.process.wait(15)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait(10)
        os.close(self.output); os.close(self.input)


DOM = '''(() => {
  const text = selector => { const node = document.querySelector(selector); return node ? node.textContent : null; };
  const parse = value => { try { return JSON.parse(value); } catch (error) { return 'INVALID'; } };
  const sections = {}, panels = {};
  for (const section of document.querySelectorAll('[data-task]')) {
    const box = section.querySelector('.events');
    sections[section.dataset.task] = {
      state: section.querySelector('.exec-state').textContent,
      children: Array.from(section.children).map(node => node.tagName + '.' + node.className),
      events: Array.from(box.children).map(node => node.tagName === 'PRE' ? Number(node.dataset.seq) : node.tagName),
      bodies: Array.from(box.children).map(node => parse(node.textContent)),
    };
  }
  for (const panel of document.querySelectorAll('[data-verification]')) {
    const artifacts = panel.querySelector('.artifacts').textContent;
    panels[panel.dataset.verification] = {
      state: panel.querySelector('.verification-state').textContent,
      verdict: parse(panel.querySelector('.verdict').textContent),
      artifacts: parse(artifacts), artifactsText: artifacts,
      live: panel.querySelectorAll('.live-verdict').length,
    };
  }
  const connection = document.querySelector('#connection');
  return {work: text('#work-status'), current: text('#current-attempt'), currentState: text('#current-state'),
          verification: text('#verification-status'), connection: connection.dataset.state,
          snapshot: connection.dataset.snapshot, events: connection.dataset.events,
          snapshotAt: Number(connection.dataset.snapshotAt || 0), now: Date.now(),
          connectionText: connection.textContent, cookie: document.cookie, sections, panels};
})()'''


class Gate:
    capabilities = ['read_only', 'edit_worktree']
    remote_cancel = True
    def __init__(self):
        self.release = threading.Semaphore(0)
    def diagnose(self):
        return {'available': True}
    def run(self, task, cancel, event):
        while not self.release.acquire(timeout=.05):
            if cancel.is_set():
                raise HubError('CANCELLED', '任务已取消')
        (Path(task['workspace']) / 'output.txt').write_text(task['prompt'])
        return {'answer': 'done', 'session_id': 'gate-session'}


@unittest.skipIf(os.name == 'nt', '依赖 fcntl 与 --remote-debugging-pipe 文件描述符')
@unittest.skipUnless(chrome_path(), '未找到 Chrome；设置 AGENT_HUB_CHROME 指向可执行文件')
class WorkbenchBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.gate = Gate()
        self.service = Service(self.root / 'data', {'gate': self.gate})
        self.service.max_auto_rework = 0
        self.token = 'browser-private-token'
        self.server = make_server(self.service, self.token)
        # ThreadingHTTPServer 默认不跟踪请求线程；改为跟踪，server_close() 才会等待仍在读写 data 的长轮询结束。
        self.server.daemon_threads = False
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True); self.worker.start()
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.chrome = Chrome(chrome_path(), str(self.root / 'profile'))
    def tearDown(self):
        try:
            self.chrome.close()
        finally:
            self.gate.release.release(20)
            self.server.shutdown(); self.worker.join()
            # 长轮询只在 service.stopping 置位后提前返回，因此先关闭服务，再等待请求线程，最后删除目录。
            self.service.close()
            self.server.server_close()
            self.tmp.cleanup()

    def wait_until(self, check, message, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(.1)
        self.fail(message)

    def settled(self, ident):
        result = self.service.call('get_result', {'task_id': ident})
        return result['ready'] and result['verification']['state'] not in {'awaiting_verification', 'verifying'}

    def dom(self, session):
        try:
            return self.chrome.evaluate(session, DOM)
        except RuntimeError:
            return None

    def wait_dom(self, session, predicate, message, timeout=30):
        last = []
        def check():
            last[:] = [self.dom(session)]
            return last[0] if last[0] and predicate(last[0]) else None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return last[0]
            time.sleep(.2)
        self.fail(message + '\n' + json.dumps(last[0] if last else None, ensure_ascii=False, indent=2)[:4000])

    def matches_service(self, ident):
        def predicate(dom):
            item = self.service.call('get_work_item', {'task_id': ident})
            current = next(a for a in item['attempts'] if a['task_id'] == item['current_task_id'])
            if (dom['work'], dom['current'], dom['currentState'], dom['verification']) != (
                    item['work_status'], current['task_id'], current['state'], current['verification']['state']):
                return False
            for attempt in item['attempts']:
                section, panel = dom['sections'].get(attempt['task_id']), dom['panels'].get(attempt['task_id'])
                if not section or section['state'] != attempt['state'] or panel['state'] != attempt['verification']['state']:
                    return False
                if panel['verdict'] != attempt['verification']:
                    return False
                if panel['artifacts'] != attempt['artifacts']:
                    return False
            return True
        return predicate

    def assert_layout(self, dom):
        self.assertEqual(dom['cookie'], '')
        for ident, section in dom['sections'].items():
            self.assertEqual(section['children'], ['H3.', 'P.', 'P.note', 'DIV.events'], ident)
            self.assertTrue(all(isinstance(seq, int) for seq in section['events']), section['events'])
            self.assertEqual(section['events'], sorted(set(section['events'])), ident)
            self.assertEqual([body['seq'] for body in section['bodies']], section['events'], ident)
        for ident, panel in dom['panels'].items():
            self.assertEqual(panel['live'], 0)
            self.assertIsInstance(panel['artifacts'], list, panel['artifactsText'])
            self.assertEqual(json.loads(panel['artifactsText']), panel['artifacts'])
            ids = [a['artifact_id'] for a in panel['artifacts']]
            self.assertEqual(len(ids), len(set(ids)), ident)
            self.assertEqual(panel['verdict']['state'], panel['state'])

    def open_page(self, ident):
        request = urllib.request.Request(self.origin + '/work/' + ident + '/session', method='POST',
                                         headers={'Authorization': 'Bearer ' + self.token})
        with urllib.request.urlopen(request) as response:
            name, cookie = response.headers['Set-Cookie'].split(';')[0].split('=', 1)
            self.assertEqual(name, 'hub_work_' + self.origin.rsplit(':', 1)[1])
        target = self.chrome.send('Target.createTarget', {'url': 'about:blank'})['targetId']
        session = self.chrome.send('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
        self.chrome.send('Network.enable', session=session)
        def grant():
            self.assertTrue(self.chrome.send('Network.setCookie', dict(name=name, value=cookie, url=self.origin + '/work/',
                                                                       path='/work', httpOnly=True, sameSite='Strict'), session)['success'])
        grant()
        self.chrome.send('Page.navigate', {'url': self.origin + '/work/' + ident}, session)
        return session, grant

    def finished_task(self, request_id):
        from hub_fixtures import with_work_item
        criteria = [{'id': 'f', 'kind': 'file_exists', 'path': 'output.txt'}]
        ident = self.service.call('submit_task', with_work_item(self.service, dict(request_id=request_id, agent='gate', prompt=request_id, cwd=str(self.workspace),
                                                       mode='read_only', acceptance={'criteria': criteria})))['id']
        self.gate.release.release()
        self.wait_until(lambda: self.settled(ident), '任务验收未结束')
        return ident

    def last_seq(self, ident):
        return self.service.store.events(ident, 0, 200)['events'][-1]['seq']

    @contextmanager
    def failing(self, *tools):
        original = self.service.call
        def call(tool, arguments):
            if tool in tools:
                raise RuntimeError('simulated outage: ' + tool)
            return original(tool, arguments)
        # The outage is deliberate; socketserver would print a traceback for every request it breaks.
        with patch.object(self.service, 'call', side_effect=call), \
                patch('agent_hub.daemon.HubHTTPServer.handle_error', lambda *args: None):
            yield

    def test_snapshot_outage_stays_visible_while_events_flow(self):
        ident = self.finished_task('snapshot-outage')
        session, _ = self.open_page(ident)
        self.wait_dom(session, lambda d: d['connection'] == 'live' and self.matches_service(ident)(d), '页面未进入实时同步')
        with self.failing('get_work_item'):
            dom = self.wait_dom(session, lambda d: d['snapshot'] == 'down', '仅快照故障时未提示', timeout=30)
            self.assertEqual((dom['connection'], dom['events']), ('down', 'ok'))
            self.assertIn('状态快照通道中断', dom['connectionText'])
            message = '快照故障期间\n事件仍到达'
            self.service.store.event(ident, 'message', {'message': message})
            seq = self.last_seq(ident)
            dom = self.wait_dom(session, lambda d: seq in d['sections'][ident]['events'], '快照故障期间事件通道未继续工作')
            self.assertEqual(dom['sections'][ident]['bodies'][-1]['data'], {'message': message})
            self.assertEqual((dom['connection'], dom['snapshot'], dom['events']), ('down', 'down', 'ok'))
            time.sleep(3)
            dom = self.dom(session)
            self.assertEqual((dom['connection'], dom['snapshot']), ('down', 'down'))
            self.assertIn('状态快照通道中断', dom['connectionText'])
        dom = self.wait_dom(session, lambda d: d['connection'] == 'live' and self.matches_service(ident)(d), '快照恢复后未恢复同步', timeout=45)
        self.assertEqual((dom['snapshot'], dom['events']), ('ok', 'ok'))
        self.assert_layout(dom)

    def test_events_outage_stays_visible_while_snapshots_succeed(self):
        ident = self.finished_task('events-outage')
        session, _ = self.open_page(ident)
        self.wait_dom(session, lambda d: d['connection'] == 'live' and self.matches_service(ident)(d), '页面未进入实时同步')
        with self.failing('get_events'):
            dom = self.wait_dom(session, lambda d: d['events'] == 'down', '仅事件故障时未提示', timeout=30)
            self.assertEqual((dom['connection'], dom['snapshot']), ('down', 'ok'))
            self.assertIn('事件通道中断', dom['connectionText'])
            since = dom['now']
            dom = self.wait_dom(session, lambda d: d['snapshotAt'] > since, '事件故障期间快照未继续成功', timeout=30)
            self.assertEqual((dom['connection'], dom['snapshot'], dom['events']), ('down', 'ok', 'down'))
            self.assertIn('事件通道中断', dom['connectionText'])
            self.assertTrue(self.matches_service(ident)(dom))
            message = '事件故障期间写入\n恢复后补齐'
            self.service.store.event(ident, 'message', {'message': message})
            seq = self.last_seq(ident)
        dom = self.wait_dom(session, lambda d: d['connection'] == 'live' and seq in d['sections'][ident]['events']
                            and self.matches_service(ident)(d), '事件恢复后未补齐或未恢复同步', timeout=45)
        self.assertEqual(dom['sections'][ident]['bodies'][-1]['data'], {'message': message})
        self.assert_layout(dom)

    def test_live_state_verification_new_attempt_and_connection_failures(self):
        from hub_fixtures import with_work_item
        criteria = [{'id': 'f', 'kind': 'file_exists', 'path': 'output.txt'}]
        ident = self.service.call('submit_task', with_work_item(self.service, dict(request_id='browser', agent='gate', prompt='first', cwd=str(self.workspace),
                                                       mode='read_only', acceptance={'criteria': criteria})))['id']
        self.wait_until(lambda: self.service.store.get(ident)['state'] == 'running', '任务未进入 running')
        session, grant = self.open_page(ident)

        dom = self.wait_dom(session, lambda d: d['connection'] == 'live' and self.matches_service(ident)(d), '运行中页面未与真实状态一致')
        self.assertEqual((dom['work'], dom['currentState'], dom['verification']), ('running', 'running', 'not_requested'))
        self.assert_layout(dom)

        self.gate.release.release()
        self.wait_until(lambda: self.settled(ident), '首轮验收未结束')
        dom = self.wait_dom(session, self.matches_service(ident), '首轮完成后顶部、执行状态或验收结论未更新')
        self.assertEqual((dom['work'], dom['currentState'], dom['verification']), ('completed', 'succeeded', 'passed'))
        self.assertEqual(dom['sections'][ident]['state'], 'succeeded')
        self.assertEqual(dom['panels'][ident]['verdict']['state'], 'passed')
        self.assertTrue(dom['panels'][ident]['artifacts'])
        self.assert_layout(dom)
        message = '第一行\n第二行 \\n 字面反斜杠'
        self.service.store.event(ident, 'message', {'message': message})
        seq = self.last_seq(ident)
        dom = self.wait_dom(session, lambda d: seq in d['sections'][ident]['events'], '含换行的事件未显示')
        self.assertEqual(dom['sections'][ident]['bodies'][-1], self.service.store.events(ident, seq - 1, 1)['events'][0])
        self.assertEqual(dom['sections'][ident]['bodies'][-1]['data']['message'], message)
        self.assert_layout(dom)

        child = self.service.call('continue_task', dict(request_id='browser-next', task_id=ident, prompt='second'))['id']
        self.wait_until(lambda: self.service.store.get(child)['state'] == 'running', '续接尝试未进入 running')
        dom = self.wait_dom(session, lambda d: d['current'] == child and self.matches_service(ident)(d), '新尝试未出现或顶部仍显示旧尝试')
        self.assertEqual((dom['work'], dom['currentState']), ('running', 'running'))
        self.assertEqual(dom['panels'][ident]['verdict']['state'], 'passed')
        self.assert_layout(dom)

        self.gate.release.release()
        self.wait_until(lambda: self.settled(child), '续接验收未结束')
        dom = self.wait_dom(session, lambda d: d['currentState'] == 'succeeded' and self.matches_service(ident)(d), '续接完成后状态未更新')
        self.assertEqual(dom['current'], child)
        self.assert_layout(dom)

        with self.failing('get_events', 'get_work_item'):
            dom = self.wait_dom(session, lambda d: d['snapshot'] == 'down' and d['events'] == 'down', '服务异常时未提示连接中断')
            self.assertEqual(dom['connection'], 'down')
            self.assertIn('连接中断', dom['connectionText'])
        dom = self.wait_dom(session, lambda d: d['connection'] == 'live' and self.matches_service(ident)(d), '服务恢复后未自动恢复同步', timeout=45)

        self.chrome.send('Network.deleteCookies', dict(name='hub_work_' + self.origin.rsplit(':', 1)[1], url=self.origin + '/work/'), session)
        dom = self.wait_dom(session, lambda d: d['connection'] == 'unauthorized', 'Cookie 失效后未提示未授权', timeout=40)
        self.assertIn('未授权', dom['connectionText']); self.assertIn('/session', dom['connectionText'])
        grant()
        dom = self.wait_dom(session, lambda d: d['connection'] == 'live' and self.matches_service(ident)(d), '重新授权后未恢复同步', timeout=20)
        self.assert_layout(dom)


if __name__ == '__main__':
    unittest.main()
