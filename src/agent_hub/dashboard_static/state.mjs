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

export const POLL_ACTIVE = 2500, POLL_IDLE = 15000;
const TERMINAL = new Set(['succeeded','failed','timed_out','cancelled','interrupted','delivery_unknown']);
const VERIFYING = new Set(['pending','awaiting_verification','verifying']);
const settled = task => !VERIFYING.has(task?.verification_state);
/** A chain is idle once no member runs and neither the chain nor the inspected execution awaits verification. */
export function chainIdle(chain, detail) {
  const summary = chain?.chain;
  if (!summary || summary.active_count || !settled(summary)) return false;
  const task = detail?.task;
  return !task || (TERMINAL.has(task.state) && settled(task));
}

const MAX_EVENTS = 1000, MAX_EVENT_BYTES = 2 * 1024 * 1024;
const eventEncoder = new TextEncoder();
// Retained events keep their identity across polls, so each is measured once.
const eventSizes = new WeakMap();
function eventSize(event) {
  let size = eventSizes.get(event);
  if (size === undefined) { size = eventEncoder.encode(JSON.stringify(event)).length + 1; eventSizes.set(event, size); }
  return size;
}
function eventWindow(previous, incoming) {
  const events = new Map(previous.map(event=>[event.seq,event]));
  for (const event of incoming) if (Number.isInteger(event.seq)) events.set(event.seq,event);
  const ordered = [...events.values()].sort((a,b)=>b.seq-a.seq), retained = [];
  let bytes = 2, trimmed = false;
  for (let event of ordered) {
    let size = eventSize(event);
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
