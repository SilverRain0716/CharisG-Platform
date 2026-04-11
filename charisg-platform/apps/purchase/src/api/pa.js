import { apiFetch } from '@charisg/auth';

export const pa = {
  summary:        () => apiFetch('/api/pa/summary'),
  dashboard:      () => apiFetch('/api/pa/dashboard'),

  // Discovery
  runDatalab:     () => apiFetch('/api/pa/datalab/run', { method: 'POST' }),
  trends:         (cat = '50000000', days = 30) => apiFetch(`/api/pa/datalab/trends?category_param=${cat}&days=${days}`),
  keywords:       (params = {}) => {
    const q = new URLSearchParams(params).toString();
    return apiFetch(`/api/pa/keywords${q ? '?' + q : ''}`);
  },
  clusters:       () => apiFetch('/api/pa/keywords/clusters'),
  runCluster:     (keywords) => apiFetch('/api/pa/keywords/cluster', { method: 'POST', body: { keywords } }),
  searchadVolumes: (keywords) => apiFetch('/api/pa/searchad/volumes', { method: 'POST', body: { keywords } }),

  // Sourcing
  sourcing:       (params = {}) => {
    const q = new URLSearchParams(params).toString();
    return apiFetch(`/api/pa/sourcing${q ? '?' + q : ''}`);
  },
  decision:       (id, decision, reason) => apiFetch(`/api/pa/sourcing/${id}/decision`, { method: 'PATCH', body: { decision, reason } }),
  bulkDecision:   (ids, decision, reason) => apiFetch('/api/pa/sourcing/bulk-decision', { method: 'POST', body: { ids, decision, reason } }),

  // Margin / Customs
  marginCalc:     (req) => apiFetch('/api/pa/margin/calculate', { method: 'POST', body: req }),
  customsQuick:   (req) => apiFetch('/api/pa/customs/quick', { method: 'POST', body: req }),
  customsTariff:  (req) => apiFetch('/api/pa/customs/tariff', { method: 'POST', body: req }),

  // Products
  products:       (params = {}) => {
    const q = new URLSearchParams(params).toString();
    return apiFetch(`/api/pa/products${q ? '?' + q : ''}`);
  },
  product:        (id) => apiFetch(`/api/pa/products/${id}`),
  setProductStatus: (id, status) => apiFetch(`/api/pa/products/${id}/status`, { method: 'PATCH', body: { status } }),

  // Detail page
  generateDetail: (pid) => apiFetch(`/api/pa/detail-page/${pid}/generate`, { method: 'POST' }),

  // Upload
  uploadSmartstore: (pid) => apiFetch(`/api/pa/smartstore/upload/${pid}`, { method: 'POST' }),
  uploadCoupang:    (pid) => apiFetch(`/api/pa/coupang/upload/${pid}`, { method: 'POST' }),

  // Orders
  ordersKanban:   () => apiFetch('/api/pa/orders/kanban'),
  orders:         (params = {}) => {
    const q = new URLSearchParams(params).toString();
    return apiFetch(`/api/pa/orders${q ? '?' + q : ''}`);
  },
  order:          (id) => apiFetch(`/api/pa/orders/${id}`),
  advance:        (id, step, note) => apiFetch(`/api/pa/orders/${id}/advance`, { method: 'PATCH', body: { step, note } }),
  tracking:       (id) => apiFetch(`/api/pa/tracking/${id}`),

  // CS
  cs:             (status) => apiFetch(`/api/pa/cs${status ? '?status=' + status : ''}`),
  createCs:       (req) => apiFetch('/api/pa/cs', { method: 'POST', body: req }),
  resolveCs:      (id, final_response) => apiFetch(`/api/pa/cs/${id}/resolve`, { method: 'PATCH', body: { final_response } }),

  // Returns
  returns:        () => apiFetch('/api/pa/returns'),

  // Monitor
  stockAlerts:    () => apiFetch('/api/pa/monitor/stock'),
  marginAlerts:   () => apiFetch('/api/pa/monitor/margin'),

  // Settings
  settings:       () => apiFetch('/api/pa/settings'),
  putSetting:     (key, value) => apiFetch('/api/pa/settings', { method: 'PUT', body: { key, value } }),
};
