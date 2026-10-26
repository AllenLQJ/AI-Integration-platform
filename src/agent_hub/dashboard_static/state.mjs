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

const MAX_EVENTS = 1000, MAX_EVENT_BYTES = 2 * 1024 * 1024;
const eventEncoder = new TextEncoder();
function eventWindow(previous, incoming) {
  const events = new Map(previous.map(event=>[event.seq,event]));
  for (const event of incoming) if (Number.isInteger(event.seq)) events.set(event.seq,event);
  const ordered = [...events.values()].sort((a,b)=>b.seq-a.seq), retained = [];
  let bytes = 2, trimmed = false;
  for (let event of ordered) {
    let size = eventEncoder.encode(JSON.stringify(event)).length + 1;
    if (size > MAX_EVENT_BYTES - 2) {
      event = {seq:event.seq,kind:event.kind,timestamp:event.timestamp,
        data:{truncated:true,message:'此事件过大，仅显示摘要；完整记录可通过 Hub get_events 读取。'}};
      size = eventEncoder.encode(JSON.stringify(event)).length + 1;
      trimmed = true;
    }
    if (retained.length >= MAX_EVENTS || bytes + size > MAX_EVENT_BYTES) { trimmed = true; break; }
    retained.push(event); bytes += size;
  }
  return {events:retained.reverse(),trimmed};
}
export function mergeEvents(previous, incoming) { return eventWindow(previous,incoming).events; }

export class DashboardState {
  constructor() {
    this.listGate = new RequestGate(); this.detailGate = new RequestGate(); this.chainGate = new RequestGate();
    this.chainId = null; this.chain = null;
    this.filters = {q:'',state:'',agent:''}; this.offset = 0; this.nextOffset = null;
    this.items = []; this.selected = null; this.detail = null; this.events = []; this.cursor = 0;
    this.historyMode = false; this.eventsTruncated = false;
  }
  changeFilters(filters) {
    this.listGate.invalidate(); this.filters = {...this.filters,...filters};
    this.items = []; this.offset = 0; this.nextOffset = null;
  }
  selectChain(id) {
    const token = this.chainGate.next(); this.detailGate.invalidate();
    this.chainId = id; this.chain = null; this.selected = null; this.detail = null; this.events = []; this.cursor = 0;
    this.historyMode = false; this.eventsTruncated = false;
    return token;
  }
  select(id) {
    const token = this.detailGate.next(); this.selected = id;
    this.detail = null; this.events = []; this.cursor = 0;
    this.historyMode = false; this.eventsTruncated = false;
    return token;
  }
  commitEvents(token, data) {
    if (!this.detailGate.current(token)) return false;
    if (this.historyMode) return true;
    const window = eventWindow(this.events,data.events);
    this.events = window.events; this.eventsTruncated ||= window.trimmed;
    this.cursor = Math.max(this.cursor, data.next_seq);
    return true;
  }
  replaceEvents(token, data, historical = false) {
    if (!this.detailGate.current(token)) return false;
    const window = eventWindow([],data.events);
    this.events = window.events; this.eventsTruncated = window.trimmed || Boolean(data.has_more);
    this.historyMode = historical;
    if (!historical) this.cursor = data.next_seq;
    return true;
  }
}
