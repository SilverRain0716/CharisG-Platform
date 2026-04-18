import { apiFetch } from '@charisg/auth';

function qs(params) {
  const q = new URLSearchParams(params).toString();
  return q ? '?' + q : '';
}

/**
 * DS API 클라이언트 — 모든 호출에 market 파라미터 자동 전달.
 * market은 createDsApi(market) 또는 개별 호출에서 설정.
 */
export function createDsApi(market = 'US') {
  const m = (extra = {}) => ({ market, ...extra });

  return {
    // Summary & Dashboard
    summary:        () => apiFetch(`/api/ds/summary${qs(m())}`),
    dashboard:      () => apiFetch(`/api/ds/dashboard${qs(m())}`),

    // Scoring Pipeline
    scoringMatrix:  () => apiFetch(`/api/ds/scoring/matrix${qs(m())}`),
    scoringDist:    () => apiFetch(`/api/ds/scoring/distribution${qs(m())}`),
    filterFails:    () => apiFetch(`/api/ds/scoring/filter-fails${qs(m())}`),
    scoringReport:  () => apiFetch(`/api/ds/scoring/report${qs(m())}`),
    runScoring:     (opts = {}) => apiFetch(`/api/ds/scoring/run${qs({...m(), ...opts})}`, { method: 'POST' }),
    scoringProgress: () => apiFetch(`/api/ds/scoring/progress${qs(m())}`),

    // Products
    products:       (params = {}) => apiFetch(`/api/ds/products${qs({...m(), ...params})}`),
    product:        (id) => apiFetch(`/api/ds/products/${id}${qs(m())}`),
    setStatus:      (id, status) => apiFetch(`/api/ds/products/${id}/status${qs(m())}`, { method: 'PATCH', body: { status } }),
    bulkStatus:     (ids, status) => apiFetch(`/api/ds/products/bulk-status${qs(m())}`, { method: 'POST', body: { ids, status } }),
    kanban:         () => apiFetch(`/api/ds/products/kanban${qs(m())}`),

    // Listings
    listings:       () => apiFetch(`/api/ds/listings${qs(m())}`),

    // CJ & Crawler
    cjStats:        () => apiFetch('/api/ds/cj/stats'),
    crawlerStatus:  () => apiFetch('/api/ds/crawler/status'),
    runCrawler:     (req) => apiFetch('/api/ds/crawler/run', { method: 'POST', body: req }),

    // Monitor
    health:         () => apiFetch(`/api/ds/monitor/health${qs(m())}`),
    postHealth:     (data) => apiFetch(`/api/ds/monitor/health${qs(m())}`, { method: 'POST', body: data }),

    // Settings
    filters:        () => apiFetch('/api/ds/settings/filters'),
    brands:         () => apiFetch('/api/ds/settings/brands'),
    feeCategories:  () => apiFetch(`/api/ds/fees/categories${qs(m())}`),

    // ASIN Pipeline
    asinSummary:     () => apiFetch(`/api/ds/asin-pipeline/summary${qs(m())}`),
    matchSingle:     (id) => apiFetch(`/api/ds/asin-pipeline/match/single/${id}${qs(m())}`, { method: 'POST' }),
    matchBatch:      (opts = {}) => apiFetch(`/api/ds/asin-pipeline/match/batch${qs({...m(), ...opts})}`, { method: 'POST' }),
    matchProgress:   () => apiFetch(`/api/ds/asin-pipeline/match/progress${qs(m())}`),
    matchCandidates: (id) => apiFetch(`/api/ds/asin-pipeline/match/candidates/${id}${qs(m())}`),
    matchSelect:     (id, asin) => apiFetch(`/api/ds/asin-pipeline/match/select/${id}/${asin}`, { method: 'POST' }),
    offerValidate:   (id) => apiFetch(`/api/ds/asin-pipeline/offer/validate/${id}${qs(m())}`, { method: 'POST' }),
    offerRegister:   (id, dryRun = true) => apiFetch(`/api/ds/asin-pipeline/offer/register/${id}${qs({...m(), dry_run: dryRun})}`, { method: 'POST' }),
    offerBatch:      (opts = {}) => apiFetch(`/api/ds/asin-pipeline/offer/batch${qs({...m(), ...opts})}`, { method: 'POST' }),
    offerProgress:   () => apiFetch(`/api/ds/asin-pipeline/offer/progress${qs(m())}`),
  };
}

// 기본 US 인스턴스 (하위 호환)
export const ds = createDsApi('US');
