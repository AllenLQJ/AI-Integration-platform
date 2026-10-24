import unittest

from agent_hub.adapters.muse import Muse


class MuseOwnedVisibleMessageTests(unittest.TestCase):
    def test_owned_visible_dedup_ignores_thought_and_foreign_roles(self):
        seen = {}
        events = [
            {'seq': 1, 'role': 'assistant', 'text': 'before baseline',
             'raw': {'payload': {'display_text_ready': True, 'status': 'completed'}}},
            {'seq': 5, 'role': 'user', 'text': 'user',
             'raw': {'payload': {'display_text_ready': True, 'status': 'completed'}}},
            {'seq': 6, 'role': 'assistant', 'text': 'thinking',
             'raw': {'payload': {'display_text_ready': True, 'status': 'completed', 'kind': 'thought'}}},
            {'seq': 7, 'role': 'assistant', 'text': 'progress',
             'raw': {'payload': {'display_text_ready': True, 'status': 'completed', 'revision': 1}}},
            {'seq': 7, 'role': 'assistant', 'text': 'progress v2',
             'raw': {'payload': {'display_text_ready': True, 'status': 'completed', 'revision': 2}}},
            {'seq': 8, 'role': 'assistant', 'text': 'hidden',
             'raw': {'payload': {'display_text_ready': False, 'status': 'completed'}}},
        ]
        got = list(Muse.owned_visible_messages(events, baseline=3, seen=seen))
        self.assertEqual([item[0]['text'] for item in got], ['progress', 'progress v2'])
        self.assertEqual(seen[7], 2)
        # Replaying same revision is ignored.
        again = list(Muse.owned_visible_messages(events, baseline=3, seen=seen))
        self.assertEqual(again, [])


if __name__ == '__main__':
    unittest.main()
