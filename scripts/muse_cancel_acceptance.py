"""只复用显式提供的 Muse 专用会话；取消后在同会话续接，不创建会话。"""
import argparse
import json
import time
import uuid
from pathlib import Path
from agent_hub.client import Client
from agent_hub.config import private_dir


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--session-id',required=True)
    parser.add_argument('--config',default=str(Path.home()/'.agent-hub/config.json'))
    args=parser.parse_args()
    root=Path(args.root).resolve()
    if root.exists():parser.error('请使用新的私有验收目录')
    private_dir(root)
    item=json.loads(Path(args.config).read_text())['agents']['muse']
    item['session_id']=args.session_id
    config=root/'config.json';config.write_text(json.dumps({'agents':{'muse':item}}));config.chmod(0o600)
    client=Client(root);nonce=uuid.uuid4().hex[:8];report={'session_id':args.session_id}
    def save():
        path=root/'report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2));path.chmod(0o600)
    def result(task_id):
        for _ in range(400):
            value=client.call('get_result',{'task_id':task_id})
            if value['ready']:return value
            time.sleep(.2)
        raise RuntimeError('等待终态超时，保留原任务 ID')
    try:
        task=client.call('submit_task',dict(request_id=uuid.uuid4().hex,agent='muse',cwd=str(root),mode='delegate',timeout_seconds=90,
            prompt='这是取消验收，不用任何工具或外部操作。在内部逐项计算从10000001开始连续200个整数平方并求和，认真检查全部后只回复总和。'+nonce))
        report['task_id']=task['id'];save()
        for _ in range(100):
            current=client.call('get_task',{'task_id':task['id']})
            if current.get('delivery')=='accepted':break
            if current['state'] in {'failed','interrupted','delivery_unknown'}:raise RuntimeError('派发未确认')
            time.sleep(.2)
        else:raise RuntimeError('未取得派发确认')
        if current['session_id']!=args.session_id:raise RuntimeError('会话不一致，禁止取消其他会话')
        time.sleep(1)
        report['cancel_response']=client.call('cancel_task',{'task_id':task['id']})
        report['result']=result(task['id']);save()
        if not report['result'].get('remote_cancelled'):raise RuntimeError('原会话停止未确认，不投递后续任务')
        child=client.call('continue_task',dict(task_id=task['id'],request_id=uuid.uuid4().hex,
            prompt='不要使用工具，只回复 MUSE-NEXT-'+nonce,timeout_seconds=60))
        report['old_task_cancel_response']=client.call('cancel_task',{'task_id':task['id']})
        report['next_result']=result(child['id'])
        report['passed']=(report['result'].get('interrupt_acknowledged') is True
            and report['result'].get('cancellation_evidence',{}).get('session_id')==args.session_id
            and report['next_result']['state']=='succeeded'
            and 'MUSE-NEXT-'+nonce in report['next_result'].get('answer','')
            and report['next_result'].get('session_id')==args.session_id)
        save();print(json.dumps({'passed':report['passed'],'session_id':args.session_id,'task_id':task['id']}))
    finally:
        if (root/'connection.json').exists():client.stop()


if __name__=='__main__':main()
