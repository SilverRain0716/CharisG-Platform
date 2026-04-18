import { apiFetch } from '@charisg/auth';

export const pa = {
  summary:        () => apiFetch('/api/pa/summary'),
  dashboard:      () => apiFetch('/api/pa/dashboard'),

  // Discovery — 5단계 풀 파이프라인 (카테고리 추적 기반)
  discoveryCategories: () => apiFetch('/api/pa/discovery/categories'),
  syncCategories:      () => apiFetch('/api/pa/discovery/categories/sync', { method: 'POST' }),
  toggleCategory:      (cid, tracked) => apiFetch(`/api/pa/discovery/categories/${cid}`, { method: 'PATCH', body: { tracked } }),
  discoveryRun:        () => apiFetch('/api/pa/discovery/run', { method: 'POST' }),
  discoveryStatus:     () => apiFetch('/api/pa/discovery/status'),

  trends:         (cat = '50000000', days = 30) => apiFetch(`/api/pa/datalab/trends?category_param=${cat}&days=${days}`),
  keywords:       (params = {}) => {
    const q = new URLSearchParams(params).toString();
    return apiFetch(`/api/pa/keywords${q ? '?' + q : ''}`);
  },
  clusters:       () => apiFetch('/api/pa/keywords/clusters'),
  runCluster:     (keywords) => apiFetch('/api/pa/keywords/cluster', { method: 'POST', body: { keywords } }),
  searchadVolumes: (keywords) => apiFetch('/api/pa/searchad/volumes', { method: 'POST', body: { keywords } }),

  // Sourcing
  sourcing:             (params = {}) => {
    const q = new URLSearchParams(params).toString();
    return apiFetch(`/api/pa/sourcing${q ? '?' + q : ''}`);
  },
  importSheet:          (sheet_url) => apiFetch('/api/pa/sourcing/import-sheet', { method: 'POST', body: { sheet_url } }),
  bulkDeleteCandidates: (ids) => apiFetch('/api/pa/sourcing/bulk-delete', { method: 'POST', body: { ids } }),
  promoteAllSourcing:   () => apiFetch('/api/pa/sourcing/promote-all', { method: 'POST', body: {} }),

  // Customs
  customsQuick:   (req) => apiFetch('/api/pa/customs/quick', { method: 'POST', body: req }),
  customsTariff:  (req) => apiFetch('/api/pa/customs/tariff', { method: 'POST', body: req }),

  // Products
  products:       (params = {}) => {
    const q = new URLSearchParams(params).toString();
    return apiFetch(`/api/pa/products${q ? '?' + q : ''}`);
  },
  product:        (id) => apiFetch(`/api/pa/products/${id}`),
  setProductStatus: (id, status) => apiFetch(`/api/pa/products/${id}/status`, { method: 'PATCH', body: { status } }),
  bulkDeleteProducts: (body) => apiFetch('/api/pa/products/bulk-delete', { method: 'POST', body }),

  // Detail page
  generateDetail: (pid) => apiFetch(`/api/pa/detail-page/${pid}/generate`, { method: 'POST' }),
  startBatchJob: (body) => apiFetch('/api/pa/detail-page/batch', { method: 'POST', body }),
  getBatchJobStatus: (jobId) => apiFetch(`/api/pa/detail-page/batch/${jobId}`),
  getCurrentBatchJob: () => apiFetch('/api/pa/detail-page/batch'),
  getDetailPage: (pid) => apiFetch(`/api/pa/detail-page/${pid}`),

  // Channel listing
  sendToChannel:    (pid, channels) => apiFetch(`/api/pa/products/${pid}/send-to-channel`, { method: 'POST', body: { channels } }),
  bulkSendToChannel: () => apiFetch('/api/pa/products/bulk-send-to-channel', { method: 'POST', body: {} }),
  smartstoreListings: () => apiFetch('/api/pa/smartstore/listings'),
  coupangListings:    () => apiFetch('/api/pa/coupang/listings'),

  // Upload
  uploadSmartstore: (pid) => apiFetch(`/api/pa/smartstore/upload/${pid}`, { method: 'POST' }),
  uploadCoupang:    (pid) => apiFetch(`/api/pa/coupang/upload/${pid}`, { method: 'POST' }),
  uploadAllSmartstore:       () => apiFetch('/api/pa/smartstore/upload-all', { method: 'POST' }),
  uploadAllCoupang:          () => apiFetch('/api/pa/coupang/upload-all', { method: 'POST' }),
  smartstoreUploadJob:       () => apiFetch('/api/pa/smartstore/upload-job'),
  smartstoreUploadStatus:    (jobId) => apiFetch(`/api/pa/smartstore/upload-all/${jobId}`),
  coupangUploadJob:          () => apiFetch('/api/pa/coupang/upload-job'),
  coupangUploadStatus:       (jobId) => apiFetch(`/api/pa/coupang/upload-all/${jobId}`),

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
  pricingSettings: () => apiFetch('/api/pa/settings/pricing'),
  updatePricing:  (body) => apiFetch('/api/pa/settings/pricing', { method: 'PUT', body }),

  // Product price override
  updateProductPrice: (pid, sale_price_krw) =>
    apiFetch(`/api/pa/products/${pid}/price`, { method: 'PATCH', body: { sale_price_krw } }),

  // SmartStore attributes
  attrPending:       () => apiFetch('/api/pa/smartstore/attributes/pending'),
  attrGet:           (pid) => apiFetch(`/api/pa/smartstore/attributes/${pid}`),
  attrInfer:         (pid) => apiFetch(`/api/pa/smartstore/attributes/${pid}/infer`, { method: 'POST' }),
  attrSave:          (pid, attributes) => apiFetch(`/api/pa/smartstore/attributes/${pid}`, { method: 'PUT', body: { attributes } }),
  attrBatchInfer:    (body) => apiFetch('/api/pa/smartstore/attributes/batch-infer', { method: 'POST', body }),
  attrBatchAll:      () => apiFetch('/api/pa/smartstore/attributes/batch-all', { method: 'POST' }),
  attrBatchAllStatus: () => apiFetch('/api/pa/smartstore/attributes/batch-all/status'),
};
