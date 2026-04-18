import { apiFetch } from '@charisg/auth';

function qs(params) {
  const q = new URLSearchParams(params).toString();
  return q ? '?' + q : '';
}

export const ds = {
  summary:        () => apiFetch('/api/ds/summary'),
  dashboard:      () => apiFetch('/api/ds/dashboard'),
  scoringMatrix:  () => apiFetch('/api/ds/scoring/matrix'),
  scoringDist:    () => apiFetch('/api/ds/scoring/distribution'),
  filterFails:    () => apiFetch('/api/ds/scoring/filter-fails'),
  scoringReport:  () => apiFetch('/api/ds/scoring/report'),
  runScoring:     (opts = {}) => apiFetch(`/api/ds/scoring/run${qs(opts)}`, { method: 'POST' }),
  scoringProgress: () => apiFetch('/api/ds/scoring/progress'),
  products:       (params = {}) => apiFetch(`/api/ds/products${qs(params)}`),
  product:        (id) => apiFetch(`/api/ds/products/${id}`),
  setStatus:      (id, status) => apiFetch(`/api/ds/products/${id}/status`, { method: 'PATCH', body: { status } }),
  bulkStatus:     (ids, status) => apiFetch('/api/ds/products/bulk-status', { method: 'POST', body: { ids, status } }),
  kanban:         () => apiFetch('/api/ds/products/kanban'),
  listings:       () => apiFetch('/api/ds/listings'),
  cjStats:        () => apiFetch('/api/ds/cj/stats'),
  crawlerStatus:  () => apiFetch('/api/ds/crawler/status'),
  runCrawler:     (req) => apiFetch('/api/ds/crawler/run', { method: 'POST', body: req }),
  health:         () => apiFetch('/api/ds/monitor/health'),
  postHealth:     (data) => apiFetch('/api/ds/monitor/health', { method: 'POST', body: data }),
  filters:        () => apiFetch('/api/ds/settings/filters'),
  brands:         () => apiFetch('/api/ds/settings/brands'),
  feeCategories:  () => apiFetch('/api/ds/fees/categories'),

  // ASIN Pipeline
  asinSummary:     () => apiFetch('/api/ds/asin-pipeline/summary'),
  matchSingle:     (id) => apiFetch(`/api/ds/asin-pipeline/match/single/${id}`, { method: 'POST' }),
  matchBatch:      (opts = {}) => apiFetch(`/api/ds/asin-pipeline/match/batch${qs(opts)}`, { method: 'POST' }),
  matchProgress:   () => apiFetch('/api/ds/asin-pipeline/match/progress'),
  matchCandidates: (id) => apiFetch(`/api/ds/asin-pipeline/match/candidates/${id}`),
  matchSelect:     (id, asin) => apiFetch(`/api/ds/asin-pipeline/match/select/${id}/${asin}`, { method: 'POST' }),
  offerValidate:   (id) => apiFetch(`/api/ds/asin-pipeline/offer/validate/${id}`, { method: 'POST' }),
  offerRegister:   (id, dryRun = true) => apiFetch(`/api/ds/asin-pipeline/offer/register/${id}?dry_run=${dryRun}`, { method: 'POST' }),
  offerBatch:      (opts = {}) => apiFetch(`/api/ds/asin-pipeline/offer/batch${qs(opts)}`, { method: 'POST' }),
  offerProgress:   () => apiFetch('/api/ds/asin-pipeline/offer/progress'),
};
