"""Shared helpers so public submit_task always carries an explicit work_item_id."""


def ensure_work_item(service, request_id='wi-default', title='工作项', acceptance=None):
    args = dict(request_id=request_id, title=title)
    if acceptance is not None:
        args['acceptance'] = acceptance
    return service.call('create_work_item', args)['work_item_id']


def with_work_item(service, args, request_prefix='wi'):
    args = dict(args)
    if not args.get('work_item_id'):
        rid = args.get('request_id', 'task')
        args['work_item_id'] = ensure_work_item(service, request_id=f'{request_prefix}:{rid}')
    return args
