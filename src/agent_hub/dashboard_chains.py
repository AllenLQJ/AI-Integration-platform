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
MAX_TIMELINE = 500


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
            # work_item tables are created by WorkItems; older pure-Store fixtures may lack them.
            try:
                member_rows = db.execute(
                    'SELECT work_item_id, task_id, relation, related_task_id FROM work_item_members').fetchall()
                item_rows = db.execute('SELECT id, title, created, updated FROM work_items').fetchall()
            except Exception:
                member_rows, item_rows = [], []
        tasks = {row['id']: dict(row) for row in rows}
        work_items = {row['id']: dict(row) for row in item_rows}
        task_work = {row['task_id']: dict(row) for row in member_rows if row['task_id'] in tasks}
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
        # Explicit work_item membership unifies develop/review/rework without guessing.
        relation_kind = {
            'review': 'review', 'rework': 'rework', 'continuation': 'continuation',
            'switch': 'switch_executor', 'development': 'development', 'linked': 'linked',
            'root': 'linked',
        }
        for task_id, member in task_work.items():
            related = member.get('related_task_id')
            kind = relation_kind.get(member.get('relation'), 'linked')
            if related and related in tasks:
                edges.append(dict(source=related, target=task_id, kind=kind))
                neighbors[related].add(task_id)
                neighbors[task_id].add(related)
                if kind == 'review':
                    reviews.add(task_id)
            # Same work_item members always share a component even without related_task_id.
            for other_id, other in task_work.items():
                if other['work_item_id'] == member['work_item_id'] and other_id != task_id:
                    neighbors[task_id].add(other_id)
                    neighbors[other_id].add(task_id)
        # Continuing a reviewer remains review work and must not replace the
        # latest implementation's acceptance/execution state.
        # Explicit work_item relation=rework is always implementation (even if
        # parent_id points at a review attempt) and must never stay in reviews.
        queue = deque(reviews)
        while queue:
            for child in parent_children[queue.popleft()]:
                member = task_work.get(child) or {}
                if member.get('relation') == 'rework':
                    continue
                if child not in reviews:
                    reviews.add(child)
                    queue.append(child)
        for task_id, member in task_work.items():
            if member.get('relation') == 'review':
                reviews.add(task_id)
            elif member.get('relation') == 'rework':
                reviews.discard(task_id)
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
        # Deduplicate edges within a component.
        for component in components:
            seen = set()
            unique = []
            for edge in component['edges']:
                key = (edge['source'], edge['target'], edge['kind'])
                if key in seen:
                    continue
                seen.add(key)
                unique.append(edge)
            component['edges'] = unique
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
            # Prefer stable work_item container when all/any members share one.
            item_ids = {task_work[ident]['work_item_id'] for ident in members if ident in task_work}
            if len(item_ids) == 1:
                wid = next(iter(item_ids))
                meta = work_items.get(wid) or {}
                component['work_item_id'] = wid
                component['work_item_title'] = meta.get('title') or '工作项'
                component['updated'] = max(component['updated'], meta.get('updated') or 0)
            else:
                component['work_item_id'] = None
                component['work_item_title'] = None
        return tasks, components, membership

    def _summary(self, tasks, component):
        root, latest = component['root'], component['latest']
        root_summary = self.dashboard._summary(self.store.get(root))
        latest_summary = root_summary if latest == root else self.dashboard._summary(self.store.get(latest))
        # Title / stats / latest main execution from implementation — not overridden by review state.
        chain_id = component.get('work_item_id') or root
        title = component.get('work_item_title') or root_summary['title']
        return dict(latest_summary, id=chain_id, title=title, created=root_summary['created'],
                    updated=component['updated'], root_task_id=root, latest_task_id=latest,
                    work_item_id=component.get('work_item_id'),
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

    @staticmethod
    def _timeline_order(entry):
        timestamp = entry.get('timestamp')
        return (timestamp is None, timestamp or 0, entry['task_id'], entry['id'])

    @staticmethod
    def _timeline_chars(entry):
        import json
        return len(json.dumps({
            'title': entry.get('title', ''),
            'preview': entry.get('preview', ''),
            'text': entry.get('text', ''),
            'operation': entry.get('operation') or {},
        }, ensure_ascii=False))

    def detail(self, member_id):
        _string(member_id, 'member_id', 200, required=True)
        tasks, components, membership = self._snapshot()
        if member_id not in membership:
            # Allow resolving by work_item_id (chain list id when aggregated).
            match = next((c for c in components if c.get('work_item_id') == member_id), None)
            if not match:
                raise HubError('TASK_NOT_FOUND', 'Task not found')
            component = match
            anchor = component['latest']
        else:
            component = components[membership[member_id]]
            anchor = member_id
        chain = self._summary(tasks, component)
        warnings = list(component['warnings'])
        # Root and requested execution remain navigable, then favor recent work.
        priority = [component['root'], anchor, component['latest']]
        priority.extend(sorted(component['members'], key=lambda ident: (tasks[ident]['updated'], ident), reverse=True))
        selected = list(dict.fromkeys(priority))[:MAX_NODES]
        selected_set = set(selected)
        if len(component['members']) > len(selected):
            warnings.append('nodes_truncated')
        nodes, messages, text_size = [], [], 0
        timeline, timeline_size = [], 0
        coverages = []
        omitted = False
        timeline_omitted = False
        for ident in selected:
            detail = self.dashboard.detail(ident)
            nodes.append(detail['task'])
            coverages.append(detail.get('capture_coverage') or {})
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
            for entry in detail.get('timeline') or []:
                entry = dict(entry, task_id=ident, agent=entry.get('agent') or detail['task']['agent'])
                heapq.heappush(timeline, (self._timeline_order(entry), entry))
                timeline_size += self._timeline_chars(entry)
                while len(timeline) > MAX_TIMELINE or timeline_size > MAX_TEXT:
                    _, removed = heapq.heappop(timeline)
                    timeline_size -= self._timeline_chars(removed)
                    timeline_omitted = True
        if omitted:
            warnings.append('messages_truncated')
        if timeline_omitted:
            warnings.append('timeline_truncated')
        nodes.sort(key=lambda node: (node['created'], node['id']))
        return dict(chain=chain, nodes=nodes,
                    edges=[edge for edge in component['edges']
                           if edge['source'] in selected_set and edge['target'] in selected_set],
                    messages=[message for _, message in sorted(messages)],
                    timeline=[entry for _, entry in sorted(timeline)],
                    capture_coverage=coverages,
                    truncated='nodes_truncated' in warnings or omitted or timeline_omitted,
                    warnings=warnings)
