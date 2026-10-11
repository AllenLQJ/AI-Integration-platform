"""既有固定 Bot 会话的真实取消、期限和正常服务关闭验收；使用已有账号额度。"""
import argparse
import json
import os
import time
import uuid
from pathlib import Path
from agent_hub.adapters.grokbot import GrokBot
from agent_hub.client import Client
from agent_hub.config import private_dir
from agent_hub.store import Store


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--https-proxy', default=os.environ.get('HTTPS_PROXY'))
    parser.add_argument('--bot-name', required=True, help='已有专用验收 Bot 名称，不创建新 Bot')
    parser.add_argument('--session-id', required=True, help='同一已有 Bot 的原会话 ID')
    parser.add_argument('--case', choices=('all','cancel','timeout','shutdown'), default='all')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if root.exists():
        parser.error('须使用新的私有目录')
    private_dir(root)
    nonce = uuid.uuid4().hex[:10]
    env = {'HTTPS_PROXY':args.https_proxy} if args.https_proxy else {}
    report = {'cases':{}, 'nonce':nonce, 'bot_name':args.bot_name, 'session_id':args.session_id}
    existing = GrokBot(args.bot_name, env=env, session_id=args.session_id)
    preflight = existing.request('GET','/v0/grokbot/sessions/'+args.session_id+'/entries?afterUpdatedSeq=-1',None,10)
    if preflight['turn'] != dict(idle=True,inFlight=False,queued=0) or existing.state_file.exists():
        parser.error('已有会话未停止或有待核对任务；不投递新任务')
    for case in (('cancel','timeout','shutdown') if args.case=='all' else (args.case,)):
        data = root/case
        private_dir(data)
        name = args.bot_name
        save(data/'config.json', {'agents':{'grokbot':{'bot_name':name,'env':env,'session_id':args.session_id}}})
        client, bot = Client(data), GrokBot(name, env=env, session_id=args.session_id)
        numbers = ','.join(str(10000001+i*7919) for i in range(80))
        task = client.call('submit_task', dict(request_id=uuid.uuid4().hex,agent='grokbot',
            prompt='这是一次取消功能验收。不要使用工具、浏览器或操作本机文件。请在内部逐项心算下列每个整数的平方并求总和，认真核对全部80项，只回复最终总和，不输出过程：'+numbers,
            cwd=str(root),mode='delegate',timeout_seconds=10 if case=='timeout' else 90))
        started = time.monotonic()
        record = dict(task_id=task['id'], bot_name=name, expected_session=args.session_id, same_session=False)
        report['cases'][case] = record
        try:
            while time.monotonic()-started < 25:
                current = client.call('get_task',{'task_id':task['id']})
                if current.get('delivery')=='accepted' and current.get('session_id'):
                    sid = current['session_id']
                    record['submit_session'] = sid
                    if sid != args.session_id:
                        raise RuntimeError('会话与指定原会话不同，禁止取消另一个会话')
                    page = bot.request('GET','/v0/grokbot/sessions/'+sid+'/entries?afterUpdatedSeq=-1',None,3)
                    record['before_cancel_turn'] = page['turn']
                    if page['turn']['inFlight']:
                        break
                time.sleep(.2)
            else:
                raise RuntimeError('任务未进入已确认的远端忙碌回合')
            if case=='cancel':
                record['cancel_response'] = client.call('cancel_task',{'task_id':task['id']})
            elif case=='shutdown':
                record['shutdown_response'] = client.stop()
            end = time.monotonic()+40
            while time.monotonic()<end:
                if case=='shutdown':
                    current = Store(data/'tasks.db').get(task['id'])
                    if current['state'] in {'cancelled','timed_out','failed','interrupted'}:
                        result = json.loads(Path(current['result']['path']).read_text())
                        result.update(state=current['state'],ready=True)
                        break
                else:
                    result = client.call('get_result',{'task_id':task['id']})
                    if result['ready']:
                        break
                time.sleep(.2)
            else:
                raise RuntimeError('未回收终态')
            record['result'] = result
            record['cancel_target_session'] = result.get('cancellation_evidence',{}).get('session_id')
            record['same_session'] = record.get('submit_session')==record['cancel_target_session']==args.session_id
            record['elapsed_seconds'] = round(time.monotonic()-started,3)
            record['passed'] = (result['state']==('timed_out' if case=='timeout' else 'cancelled')
                and result.get('remote_cancelled') is True
                and result.get('interrupt_acknowledged') is True
                and not bot.state_file.exists() and record['same_session'])
            save(root/'report.json',report)
            print(case,json.dumps({'passed':record['passed'],'state':result['state'],
                'remote_cancelled':result.get('remote_cancelled'),'interrupt_acknowledged':result.get('interrupt_acknowledged'),
                'evidence':result.get('cancellation_evidence'),'warning':result.get('warning')},ensure_ascii=False),flush=True)
            if not result.get('remote_cancelled'):
                raise RuntimeError('远端停止未确认，保留全部证据并停止后续投递')
            if case=='cancel':
                next_task = client.call('submit_task',dict(request_id=uuid.uuid4().hex,agent='grokbot',
                    prompt='不用工具，只回复 NEXT-'+nonce,cwd=str(root),mode='delegate',timeout_seconds=60))
                record['old_task_cancel_response'] = client.call('cancel_task',{'task_id':task['id']})
                for _ in range(300):
                    next_result = client.call('get_result',{'task_id':next_task['id']})
                    if next_result['ready']:
                        break
                    time.sleep(.2)
                record['next_task'] = dict(task_id=next_task['id'], result=next_result)
                record['next_task_verified'] = next_result['state']=='succeeded' and 'NEXT-'+nonce in next_result.get('answer','')
                save(root/'report.json',report)
                if not record['next_task_verified']:
                    raise RuntimeError('取消后新任务未通过')
        finally:
            if (data/'connection.json').exists():
                client.stop()
    report['passed'] = all(r['passed'] for r in report['cases'].values())
    save(root/'report.json',report)


if __name__=='__main__':
    main()
