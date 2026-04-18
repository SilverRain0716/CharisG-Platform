export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

/**
 * apiFetch — credentials: 'include' 가 강제된 fetch 래퍼.
 * 401 응답은 ApiError 로 throw. 페이지 전환은 호출자(App.jsx useAuth)가 담당.
 */
export async function apiFetch(path, opts = {}) {
  const { method = 'GET', body, headers = {}, signal, raw = false } = opts;

  const init = {
    method,
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      ...headers,
    },
    signal,
  };

  if (body !== undefined) {
    if (body instanceof FormData) {
      init.body = body;
    } else {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
  }

  const res = await fetch(path, init);

  if (res.status === 401) {
    throw new ApiError('Unauthorized', 401, null);
  }

  if (!res.ok) {
    let errBody = null;
    try { errBody = await res.json(); } catch {}
    throw new ApiError(
      errBody?.detail || res.statusText || `HTTP ${res.status}`,
      res.status,
      errBody,
    );
  }

  if (raw) return res;

  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    return res.json();
  }
  return res.text();
}
