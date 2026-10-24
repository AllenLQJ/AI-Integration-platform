// The browser only receives a scoped HttpOnly dashboard cookie.
export class APIError extends Error {
  constructor(message, status = 0) { super(message); this.name = 'APIError'; this.status = status; }
}

export function createAPI(fetcher = globalThis.fetch.bind(globalThis)) {
  async function request(path, { signal, method = 'GET', body } = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timeout = setTimeout(abort, 15000);
    try {
      const response = await fetcher(`/dashboard/${path}`, {
        method, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: { Accept: 'application/json', ...(body ? {'Content-Type': 'application/json'} : {}) },
        signal: controller.signal, ...(body ? {body: JSON.stringify(body)} : {}),
      });
      if (!response.ok) throw new APIError(response.status === 401 || response.status === 403
        ? '会话已失效，请在本机重新打开看板。' : `请求失败（${response.status}），请稍后重试。`, response.status);
      try { return await response.json(); }
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
  };
}

export async function consumeTicket(api, location = window.location, history = window.history) {
  const fragment = location.hash;
  // Remove sensitive fragment before any asynchronous work, including invalid tickets.
  if (fragment) history.replaceState(null, '', location.pathname + location.search);
  const ticket = new URLSearchParams(fragment.replace(/^#/, '')).get('ticket');
  if (ticket) await api.session(ticket);
}
