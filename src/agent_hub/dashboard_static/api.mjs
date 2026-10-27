// The browser only receives a scoped HttpOnly dashboard cookie.
export class APIError extends Error {
  constructor(message, status = 0) { super(message); this.name = 'APIError'; this.status = status; }
}

// Responses served from the revalidated cache (HTTP 304); callers may skip diffing them.
const revalidated = new WeakSet();
export const isUnchanged = value => value != null && typeof value === 'object' && revalidated.has(value);

export function createAPI(fetcher = globalThis.fetch.bind(globalThis)) {
  // Memory only: never persist conversation payloads or credentials to storage.
  const responses = new Map();
  let responseBytes = 0;
  function forget(path) {
    const old = responses.get(path);
    if (old) responseBytes -= old.bytes;
    responses.delete(path);
  }
  function clear() { responses.clear(); responseBytes = 0; }
  async function request(path, { signal, method = 'GET', body } = {}) {
    if (method !== 'GET') clear();
    const cached = method === 'GET' ? responses.get(path) : null;
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timeout = setTimeout(abort, 15000);
    try {
      const response = await fetcher(`/dashboard/${path}`, {
        method, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: { Accept: 'application/json', ...(body ? {'Content-Type': 'application/json'} : {}),
          ...(cached ? {'If-None-Match': cached.etag} : {}) },
        signal: controller.signal, ...(body ? {body: JSON.stringify(body)} : {}),
      });
      if (response.status === 304 && cached) { const value = JSON.parse(cached.text); revalidated.add(value); return value; }
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) clear();
        throw new APIError(response.status === 401 || response.status === 403
          ? '会话已失效，请在本机重新打开看板。' : `请求失败（${response.status}），请稍后重试。`, response.status);
      }
      try {
        const data = await response.json();
        forget(path);
        const etag = response.headers?.get?.('ETag');
        if (method === 'GET' && etag) {
          const text = JSON.stringify(data), bytes = text.length * 2;
          if (bytes <= 2 * 1024 * 1024) {
            responses.set(path, {etag,text,bytes}); responseBytes += bytes;
            while (responses.size > 32 || responseBytes > 8 * 1024 * 1024) forget(responses.keys().next().value);
          }
        }
        return data;
      }
      catch { throw new APIError('服务器响应无法解析。'); }
    } catch (error) {
      if (error.name === 'AbortError' && !signal?.aborted) throw new APIError('连接超时，请检查 Hub 是否运行。');
      if (error instanceof APIError || error.name === 'AbortError') throw error;
      throw new APIError('无法连接 Hub，请检查服务并重试。');
    } finally {
      clearTimeout(timeout); signal?.removeEventListener('abort', abort);
    }
  }
  function valid(value, condition) { if (!condition) throw new APIError('服务器响应格式不正确。'); return value; }
  return {
    session: ticket => request('session', {method: 'POST', body: {ticket}}),
    async tasks(filters, signal) {
      const params = new URLSearchParams(Object.entries(filters).filter(([,v])=>v !== '' && v != null));
      const data = await request(`api/v1/tasks?${params}`, {signal});
      return valid(data, Array.isArray(data?.items) && (data.next_offset === null || Number.isInteger(data.next_offset)));
    },
    async chains(filters, signal) {
      const params = new URLSearchParams(Object.entries(filters).filter(([,v])=>v !== '' && v != null));
      const data = await request(`api/v1/chains?${params}`, {signal});
      return valid(data, Array.isArray(data?.items) && (data.next_offset === null || Number.isInteger(data.next_offset)));
    },
    async chain(id, signal) {
      const data = await request(`api/v1/chains/${encodeURIComponent(id)}`, {signal});
      return valid(data, typeof data?.chain?.id === 'string' && ['nodes','edges','messages','warnings'].every(key=>Array.isArray(data[key])) && typeof data.truncated === 'boolean');
    },
    async agents(signal) {
      const data = await request('api/v1/agents', {signal}); return valid(data, Array.isArray(data?.items));
    },
    async detail(id, signal) {
      const data = await request(`api/v1/tasks/${encodeURIComponent(id)}`, {signal});
      return valid(data, data?.task?.id === id && ['messages','relations','criteria','artifacts'].every(key=>Array.isArray(data[key])));
    },
    async events(id, after, signal) {
      const data = await request(`api/v1/tasks/${encodeURIComponent(id)}/events?after_seq=${after}`, {signal});
      return valid(data, Array.isArray(data?.events) && Number.isInteger(data.next_seq));
    },
    async eventHistory(id, before = '9223372036854775807', signal) {
      const data = await request(`api/v1/tasks/${encodeURIComponent(id)}/events?before_seq=${before}`, {signal});
      return valid(data, Array.isArray(data?.events) && Number.isInteger(data.next_seq) && typeof data.has_more === 'boolean');
    },
  };
}

export async function consumeTicket(api, location = window.location, history = window.history) {
  const fragment = location.hash;
  // Remove sensitive fragment before any asynchronous work, including invalid tickets.
  if (fragment) history.replaceState(null, '', location.pathname + location.search);
  const ticket = new URLSearchParams(fragment.replace(/^#/, '')).get('ticket');
  if (ticket) await api.session(ticket);
}
