import datetime
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_hub.dashboard import Dashboard
from agent_hub.dashboard_chains import OVERVIEW_ATTENTION_LIMIT, DashboardChains
from agent_hub.store import Store
from agent_hub.verification import Verification


def noon(days_ago):
    """Local noon N days back, so day buckets never straddle midnight while the test runs."""
    day = datetime.date.today() - datetime.timedelta(days=days_ago)
    return datetime.datetime.combine(day, datetime.time(12)).timestamp()


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'tasks.db')
        self.verification = Verification(self.store, None)
        self.service = SimpleNamespace(store=self.store, verification=self.verification, adapters={})
        self.chains = DashboardChains(Dashboard(self.service))

    def task(self, agent, state, days_ago=0, seconds=60, prompt='任务', **fields):
        ident = self.store.create('r%d' % len(self.store.list()), dict(
            agent=agent, prompt=prompt, cwd=r'C:\Users\private-user\project', mode='read_only', **fields))['id']
        if state != 'queued':
            if state in {'running', 'waiting_for_user', 'cancelling'}:
                self.store.update(ident, state=state)
            else:
                self.store.finish(ident, state, {})
        created = noon(days_ago)
        with self.store.connection() as db:
            db.execute('UPDATE tasks SET created=?, updated=? WHERE id=?', (created, created + seconds, ident))
        return ident

    def test_empty_hub(self):
        result = self.chains.overview()
        self.assertEqual(result['totals'], dict(chains=0, executions=0, active=0, waiting_for_user=0))
        self.assertEqual((result['agents'], result['attention'], result['attention_total']), ([], [], 0))
        self.assertEqual(len(result['daily']), 14)
        self.assertTrue(all(day['succeeded'] == day['failed'] == day['other'] == day['active'] == 0
                            for day in result['daily']))

    def test_counts_executions_by_state_agent_and_day(self):
        self.task('codex', 'succeeded', days_ago=0, seconds=60)
        self.task('codex', 'failed', days_ago=1, seconds=120)
        self.task('claude', 'timed_out', days_ago=3, seconds=300)
        self.task('claude', 'running', days_ago=0)
        self.task('claude', 'cancelled', days_ago=20, seconds=100)
        result = self.chains.overview()
        self.assertEqual(result['totals'], dict(chains=5, executions=5, active=1, waiting_for_user=0))
        self.assertEqual(result['states'], dict(cancelled=1, failed=1, running=1, succeeded=1, timed_out=1))
        claude, codex = result['agents']
        self.assertEqual((claude['agent'], codex['agent']), ('claude', 'codex'))
        self.assertEqual((claude['executions'], claude['succeeded'], claude['failed'], claude['active']), (3, 0, 1, 1))
        # Medians cover finished executions only: claude has 300 and 100, codex 60 and 120.
        self.assertEqual((claude['median_seconds'], codex['median_seconds']), (200.0, 90.0))
        daily = {day['date']: day for day in result['daily']}
        today = datetime.date.today()
        self.assertEqual(result['daily'][0]['date'], (today - datetime.timedelta(days=13)).isoformat())
        self.assertEqual(result['daily'][-1]['date'], today.isoformat())
        self.assertEqual(daily[today.isoformat()], dict(date=today.isoformat(), succeeded=1, failed=0, other=0, active=1))
        self.assertEqual(daily[(today - datetime.timedelta(days=1)).isoformat()]['failed'], 1)
        self.assertEqual(daily[(today - datetime.timedelta(days=3)).isoformat()]['failed'], 1)
        # The 20-day-old execution is outside the window.
        self.assertEqual(sum(day[key] for day in result['daily'] for key in ('succeeded', 'failed', 'other', 'active')), 4)

    def test_attention_lists_chains_that_need_a_person_newest_first(self):
        self.task('codex', 'succeeded', days_ago=0)
        review = self.task('codex', 'succeeded', days_ago=0, seconds=30,
                           acceptance={'criteria': [{'id': 'r', 'kind': 'content_review', 'description': '核对'}]})
        with self.store.connection() as db:
            db.execute("INSERT INTO verifications VALUES (?, 'needs_review', '{}')", (review,))
        failed = [self.task('codex', 'failed', days_ago=2, seconds=index) for index in range(OVERVIEW_ATTENTION_LIMIT + 1)]
        waiting = self.task('muse', 'waiting_for_user', days_ago=0, seconds=5)
        result = self.chains.overview()
        self.assertEqual(result['attention_total'], len(failed) + 2)
        self.assertEqual(len(result['attention']), OVERVIEW_ATTENTION_LIMIT)
        updated = [item['updated'] for item in result['attention']]
        self.assertEqual(updated, sorted(updated, reverse=True))
        listed = [item['task_id'] for item in result['attention']]
        self.assertEqual(listed[:2], [review, waiting])
        self.assertEqual(result['attention'][0]['work_status'], 'needs_review')
        self.assertEqual(result['work_status']['needs_review'], 1)
        self.assertEqual(result['totals']['waiting_for_user'], 1)

    def test_only_titles_and_counts_leave_the_hub(self):
        prompt = '整理报告 ' + 'x' * 300 + '\n第二行包含 C:\\Users\\private-user\\secret.txt'
        self.task('codex', 'failed', prompt=prompt)
        text = json.dumps(self.chains.overview(), ensure_ascii=False)
        self.assertNotIn('private-user', text)
        self.assertNotIn('第二行', text)
        self.assertLessEqual(len(self.chains.overview()['attention'][0]['title']), 120)


if __name__ == '__main__':
    unittest.main()
