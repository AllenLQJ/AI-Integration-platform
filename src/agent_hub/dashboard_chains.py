"""Read-only task-chain projections built from explicit stored relationships.

A request scans only graph metadata once. Conversation/result reads are limited
by the displayed-node cap; no adapters, scheduler or controller writes are used.
"""
import heapq
from collections import defaultdict, deque

from .dashboard import _integer, _string, _text
from .models import HubError, TERMINAL


MAX_NODES = 200
MAX_MESSAGES = 500
MAX_TEXT = 1000000


class DashboardChains:
    def __init__(self, dashboard):
        self.dashboard = dashboard
        self.store = dashboard.store

    def _snapshot(self, q='', state='', agent=''):
        # Keep large prompts/results/artifacts out of the all-task graph scan.
        pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        with self.store.connection() as db:
            rows = db.execute("""SELECT id,state,created,updated,
                json_extract(data,'$.agent') AS agent,
                json_extract(data,'$.parent_id') AS parent_id,
                json_extract(data,'$.review_of.task_id') AS review_id,
                CASE WHEN json_extract(data,'$.resume_session') THEN 1
                     WHEN length(json_extract(data,'$.resume_session')) > 0 THEN 1 ELSE 0 END AS resumed,
                ((? = '' OR id LIKE ? ESCAPE '\\' OR json_extract(data,'$.prompt') LIKE ? ESCAPE '\\')
                 AND (? = '' OR state = ?)
                 AND (? = '' OR json_extract(data,'$.agent') = ?)) AS matches
                FROM tasks""", (q, pattern, pattern, state, state, agent, agent)).fetchall()
        tasks = {row['id']: dict(row) for row in rows}
        neighbors = defaultdict(set)
        edges = []
        dangling = set()
        reviews = set()
        parent_children = defaultdict(list)
        for ident, task in tasks.items():
            if task['review_id']:
                reviews.add(ident)
            for field, kind in [('parent_id', 'continuation' if task['resumed'] else 'switch_executor'),
                                ('review_id', 'review')]:
                source = task[field]
                if not source:
                    continue
                if source not in tasks:
                    dangling.add(ident)
                    continue
                edges.append(dict(source=source, target=ident, kind=kind))
                neighbors[source].add(ident)
                neighbors[ident].add(source)
                if field == 'parent_id':
                    parent_children[source].append(ident)
        # Continuing a reviewer remains review work and must not replace the
        # latest implementation's acceptance/execution state.
        queue = deque(reviews)
        while queue:
            for child in parent_children[queue.popleft()]:
                if child not in reviews:
                    reviews.add(child)
                    queue.append(child)
        components, membership = [], {}
        for ident in tasks:
            if ident in membership:
                continue
            index = len(components)
            group, pending = [], [ident]
            membership[ident] = index
            while pending:
                current = pending.pop()
                group.append(current)
                for neighbor in neighbors[current]:
                    if neighbor not in membership:
                        membership[neighbor] = index
                        pending.append(neighbor)
            components.append(dict(members=group, edges=[]))
        for edge in edges:
            components[membership[edge['source']]]['edges'].append(edge)
        order = lambda ident: (tasks[ident]['created'], ident)
        for component in components:
            members = component['members']
            component['edges'].sort(key=lambda edge: (order(edge['source']), order(edge['target']), edge['kind']))
            incoming = {ident: 0 for ident in members}
            outgoing = defaultdict(list)
            for edge in component['edges']:
                incoming[edge['target']] += 1
                outgoing[edge['source']].append(edge['target'])
            roots = [ident for ident in members if incoming[ident] == 0]
            component['root'] = min(roots or members, key=order)
            queue = deque(roots)
            visited = 0
            while queue:
                current = queue.popleft()
                visited += 1
                for child in outgoing[current]:
                    incoming[child] -= 1
                    if incoming[child] == 0:
                        queue.append(child)
            component['warnings'] = (['cycles'] if visited < len(members) else [])
            if any(ident in dangling for ident in members):
                component['warnings'].append('dangling_links')
            implementation = [ident for ident in members if ident not in reviews]
            component['latest'] = max(implementation or members, key=order)
            component['updated'] = max(tasks[ident]['updated'] for ident in members)
        return tasks, components, membership

    def _summary(self, tasks, component):
        root, latest = component['root'], component['latest']
        root_summary = self.dashboard._summary(self.store.get(root))
        latest_summary = root_summary if latest == root else self.dashboard._summary(self.store.get(latest))
        return dict(latest_summary, id=root, title=root_summary['title'], created=root_summary['created'],
                    updated=component['updated'], root_task_id=root, latest_task_id=latest,
                    task_count=len(component['members']),
                    agents=sorted({_text(tasks[ident]['agent']) for ident in component['members']}),
                    active_count=sum(tasks[ident]['state'] not in TERMINAL for ident in component['members']))

    def chains(self, limit=30, offset=0, q='', state='', agent=''):
        _integer(limit, 'limit', 1, 100)
        _integer(offset, 'offset', 0, 9223372036854775807)
        for name, value, maximum in [('q', q, 500), ('state', state, 64), ('agent', agent, 200)]:
            _string(value, name, maximum)
        tasks, components, _ = self._snapshot(q, state, agent)
        matching = [component for component in components
                    if any(tasks[ident]['matches'] for ident in component['members'])]
        matching.sort(key=lambda component: (-component['updated'], component['root']))
        page = matching[offset:offset + limit]
        return dict(items=[self._summary(tasks, component) for component in page],
                    next_offset=offset + limit if offset + limit < len(matching) else None)

    @staticmethod
    def _message_order(message):
        timestamp = message.get('timestamp')
        return (timestamp is None, timestamp or 0, message['task_id'], message['id'])

    def detail(self, member_id):
        _string(member_id, 'member_id', 200, required=True)
        tasks, components, membership = self._snapshot()
        if member_id not in membership:
            raise HubError('TASK_NOT_FOUND', 'Task not found')
        component = components[membership[member_id]]
        chain = self._summary(tasks, component)
        warnings = list(component['warnings'])
        # Root and requested execution remain navigable, then favor recent work.
        priority = [component['root'], member_id, component['latest']]
        priority.extend(sorted(component['members'], key=lambda ident: (tasks[ident]['updated'], ident), reverse=True))
        selected = list(dict.fromkeys(priority))[:MAX_NODES]
        selected_set = set(selected)
        if len(component['members']) > len(selected):
            warnings.append('nodes_truncated')
        nodes, messages, text_size = [], [], 0
        omitted = False
        for ident in selected:
            detail = self.dashboard.detail(ident)
            nodes.append(detail['task'])
            for message in detail['messages']:
                message = dict(message, task_id=ident, agent=detail['task']['agent'])
                if message['completeness'] == 'truncated':
                    omitted = True
                heapq.heappush(messages, (self._message_order(message), message))
                text_size += len(message['text'])
                while len(messages) > MAX_MESSAGES or text_size > MAX_TEXT:
                    _, removed = heapq.heappop(messages)
                    text_size -= len(removed['text'])
                    omitted = True
        if omitted:
            warnings.append('messages_truncated')
        nodes.sort(key=lambda node: (node['created'], node['id']))
        return dict(chain=chain, nodes=nodes,
                    edges=[edge for edge in component['edges']
                           if edge['source'] in selected_set and edge['target'] in selected_set],
                    messages=[message for _, message in sorted(messages)],
                    truncated='nodes_truncated' in warnings or omitted, warnings=warnings)
