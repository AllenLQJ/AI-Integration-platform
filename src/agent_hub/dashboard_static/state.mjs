export class RequestGate {
  constructor() { this.token = null; }
  invalidate() { this.token?.controller.abort(); this.token = null; }
  next() {
    this.invalidate();
    const controller = new AbortController();
    this.token = {controller, signal: controller.signal};
    return this.token;
  }
  current(token) { return Boolean(token && this.token === token && !token.signal.aborted); }
}

export function mergeEvents(previous, incoming) {
  const events = new Map(previous.map(event=>[event.seq,event]));
  for (const event of incoming) if (Number.isInteger(event.seq)) events.set(event.seq,event);
  return [...events.values()].sort((a,b)=>a.seq-b.seq);
}

export class DashboardState {
  constructor() {
    this.listGate = new RequestGate(); this.detailGate = new RequestGate();
    this.filters = {q:'',state:'',agent:''}; this.offset = 0; this.nextOffset = null;
    this.items = []; this.selected = null; this.detail = null; this.events = []; this.cursor = 0;
  }
  changeFilters(filters) {
    this.listGate.invalidate(); this.filters = {...this.filters,...filters};
    this.items = []; this.offset = 0; this.nextOffset = null;
  }
  select(id) {
    const token = this.detailGate.next(); this.selected = id;
    this.detail = null; this.events = []; this.cursor = 0;
    return token;
  }
  commitEvents(token, data) {
    if (!this.detailGate.current(token)) return false;
    this.events = mergeEvents(this.events, data.events);
    this.cursor = Math.max(this.cursor, data.next_seq);
    return true;
  }
}
