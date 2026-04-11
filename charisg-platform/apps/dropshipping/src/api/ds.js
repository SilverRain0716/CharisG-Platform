import { apiFetch } from '@charisg/auth';

export const ds = {
  summary:        () => apiFetch('/api/ds/summary'),
  dashboard:      () => apiFetch('/api/ds/dashboard'),
  scoringMatrix:  () => apiFetch('/api/ds/scoring/matrix'),
  scoringDist:    () => apiFetch('/api/ds/scoring/distribution'),
  filterFails:    () => apiFetch('/api/ds/scoring/filter-fails'),
  scoringReport:  () => apiFetch('/api/ds/scoring/report'),
  runScoring:     () => apiFetch('/api/ds/scoring/run', { method: 'POST' }),
  products:       (params = {}) => {
    const q = new URLSearchParams(params).toString();
    return apiFetch(`/api/ds/products${q ? '?' + q : ''}`);
  },
  product:        (id) => apiFetch(`/api/ds/products/${id}`),
  setStatus:      (id, status) => apiFetch(`/api/ds/products/${id}/status`, { method: 'PATCH', body: { status } }),
  bulkStatus:     (ids, status) => apiFetch('/api/ds/products/bulk-status', { method: 'POST', body: { ids, status } }),
  kanban:         () => apiFetch('/api/ds/products/kanban'),
  crawlerStatus:  () => apiFetch('/api/ds/crawler/status'),
  runCrawler:     (req) => apiFetch('/api/ds/crawler/run', { method: 'POST', body: req }),
  health:         () => apiFetch('/api/ds/monitor/health'),
  postHealth:     (data) => apiFetch('/api/ds/monitor/health', { method: 'POST', body: data }),
  filters:        () => apiFetch('/api/ds/settings/filters'),
  brands:         () => apiFetch('/api/ds/settings/brands'),
  feeCategories:  () => apiFetch('/api/ds/fees/categories'),
};
