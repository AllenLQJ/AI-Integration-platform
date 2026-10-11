"""Muse 人工等待：显式协议优先，保守识别中途求助消息，绝不操作验证。"""
import re
import time
from pathlib import Path
from ..models import HubError, redact

USER_WAIT_SECONDS = 900


def find_intervention(history, baseline, task_id):
    rows = [row for row in history if (row.get('seq') or 0) > baseline
            and row.get('role') in {'assistant', 'message.assistant'}
            and row.get('raw', {}).get('payload', {}).get('display_text_ready', True)
            and row.get('text')]
    if not rows:
        return None
    row = max(rows, key=lambda row: row['seq'])
    text = row['text']
    tag = re.search(r'\[HUB_WAIT:' + re.escape(task_id) + r':(human_verification|login|approval)\]', text)
    evidence = 'explicit_message_protocol'
    if tag:
        reason = tag[1]
    else:
        # 仅识别当前阻碍 + 请求用户操作的组合；一般介绍验证机制不算阻塞。
        blocking = re.search(r'卡住|受阻|阻碍|无法继续|没法确认|必须先|需要你|等待你|等你处理|blocked by|cannot continue', text, re.I)
        handover = re.search(r'接管|手动|请你|需要你|等你处理|等待你|take over|manually|need you', text, re.I)
        if not blocking or not handover:
            return None
        reason = next((key for key, pattern in (
            ('human_verification', r'人机验证|验证您是人类|验证你是人类|human.verification|captcha'),
            ('login', r'登录|log.?in|sign.?in'), ('approval', r'审批|批准|approval'))
            if re.search(pattern, text, re.I)), None)
        if reason is None:
            return None
        evidence = 'assistant_message_inference'
    return dict(intervention_id=f"{task_id}:{row['seq']}", reason=reason,
                message=redact(text[:2000]), evidence=evidence, seq=row['seq'])


class HumanWait:
    def __init__(self, task, session_id, event):
        self.task, self.session_id, self.event = task, session_id, event
        self.current = None
        self.started = None
        self.used = 0
        self.seen = set()
        self.reply_after = 0

    def observe(self, history, baseline):
        item = find_intervention(history, baseline, self.task['id'])
        if item and item['intervention_id'] not in self.seen and self.current is None:
            self.seen.add(item['intervention_id'])
            self.reply_after = item['seq']
            self.started = time.time()
            self.remaining = max(0, self.task['deadline'] - self.started)
            self.task['deadline'] = self.started + max(0, USER_WAIT_SECONDS - self.used)
            self.current = dict(item, session_id=self.session_id,
                wait_deadline=self.task['deadline'], execution_deadline=self.task['deadline'],
                action='请用户在 Muse 原会话接管浏览器处理；确认完成后调用 resume_task，不要重复提交')
            self.event('waiting_for_user', self.current)
        return self.current

    def resume(self):
        if not self.current:
            return
        self.used += time.time() - self.started
        self.task['deadline'] = time.time() + self.remaining
        self.event('user_wait_resumed', dict(intervention_id=self.current['intervention_id'],
            session_id=self.session_id, execution_deadline=self.task['deadline']))
        self.current = None

    def consume_confirmation(self):
        if not self.current or not self.task.get('log_dir'):
            return False
        path = Path(self.task['log_dir']) / 'muse-resume.json'
        if not path.exists():
            return False
        import json
        confirmation = json.loads(path.read_text(encoding='utf-8'))
        if confirmation.get('intervention_id') != self.current['intervention_id']:
            path.unlink()
            return False
        if time.time() >= self.current['wait_deadline']:
            raise HubError('TIMED_OUT', '人工等待期限到达')
        # 发送前消耗指令；任何发送不确定都终止等待，不重试或重发。
        path.unlink()
        self.resume()
        return True
