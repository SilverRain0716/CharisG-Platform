import React, { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

const TABS = [
  { id: 'mappings', label: '키워드 매핑' },
  { id: 'reviews', label: '검토 큐' },
];

export default function CategoryMappingPage() {
  const [tab, setTab] = useState('mappings');
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-ink-900">카테고리 매핑</h1>
        <div className="flex gap-2">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition ${
                tab === t.id
                  ? 'bg-primary-600 text-white'
                  : 'bg-white border border-ink-200 text-ink-600 hover:bg-ink-50'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      {tab === 'mappings' ? <MappingsTab /> : <ReviewsTab />}
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━ MAPPINGS TAB ━━━━━━━━━━━━━━━━━━
function MappingsTab() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState({ keyword: '', source: '' });
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState(null);

  const params = {};
  if (filter.keyword) params.keyword = filter.keyword;
  if (filter.source) params.source = filter.source;
  const { data, isLoading } = useQuery({
    queryKey: ['pa', 'category-map', filter],
    queryFn: () => pa.categoryMappings(params),
  });

  const delMut = useMutation({
    mutationFn: (id) => pa.deleteCategoryMapping(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['pa', 'category-map'] }),
  });

  const handleEdit = (row) => {
    setEditing(row);
    setShowForm(true);
  };
  const handleDelete = (row) => {
    if (!confirm(`"${row.keyword}" 매핑을 삭제할까요?`)) return;
    delMut.mutate(row.id);
  };

  return (
    <Card>
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <input
            type="text"
            placeholder="키워드 검색"
            className="border rounded px-3 py-2 text-sm"
            value={filter.keyword}
            onChange={(e) => setFilter({ ...filter, keyword: e.target.value })}
          />
          <select
            className="border rounded px-3 py-2 text-sm"
            value={filter.source}
            onChange={(e) => setFilter({ ...filter, source: e.target.value })}
          >
            <option value="">전체 출처</option>
            <option value="manual">manual</option>
            <option value="ai">ai</option>
            <option value="verified">verified</option>
          </select>
          <div className="ml-auto">
            <Button
              variant="primary"
              onClick={() => {
                setEditing(null);
                setShowForm(true);
              }}
            >
              + 새 매핑
            </Button>
          </div>
        </div>

        <div className="text-sm text-ink-500">
          총 {data?.total ?? 0}건 (표시: {data?.items?.length ?? 0}건)
        </div>

        {isLoading ? (
          <div className="text-ink-400 text-sm">로딩 중...</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-ink-50 text-ink-700">
                <tr>
                  <th className="px-3 py-2 text-left">키워드</th>
                  <th className="px-3 py-2 text-left">네이버</th>
                  <th className="px-3 py-2 text-left">쿠팡</th>
                  <th className="px-3 py-2 text-center">출처</th>
                  <th className="px-3 py-2 text-center">score</th>
                  <th className="px-3 py-2 text-center">갱신</th>
                  <th className="px-3 py-2 text-right">action</th>
                </tr>
              </thead>
              <tbody>
                {(data?.items || []).map((row) => (
                  <tr key={row.id} className="border-t border-ink-100">
                    <td className="px-3 py-2 font-mono text-ink-800">{row.keyword}</td>
                    <td className="px-3 py-2 text-xs">
                      {row.naver_category_id ? (
                        <>
                          <span className="font-mono text-ink-700">{row.naver_category_id}</span>
                          <div className="text-ink-400">{row.naver_category_path}</div>
                        </>
                      ) : (
                        <span className="text-ink-300">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {row.coupang_category_code ? (
                        <>
                          <span className="font-mono text-ink-700">{row.coupang_category_code}</span>
                          <div className="text-ink-400">{row.coupang_category_path}</div>
                        </>
                      ) : (
                        <span className="text-ink-300">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-center">
                      <StatusBadge
                        variant={
                          row.source === 'verified' ? 'success'
                            : row.source === 'ai' ? 'info'
                              : 'neutral'
                        }
                      >
                        {row.source}
                      </StatusBadge>
                    </td>
                    <td className="px-3 py-2 text-center text-xs text-ink-500">
                      {row.ai_naver_score && `N:${row.ai_naver_score}`}
                      {row.ai_coupang_score && ` C:${row.ai_coupang_score}`}
                    </td>
                    <td className="px-3 py-2 text-center text-xs text-ink-400">
                      {row.updated_at?.split(' ')[0] || ''}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        className="text-primary-600 text-xs mr-2"
                        onClick={() => handleEdit(row)}
                      >
                        편집
                      </button>
                      <button
                        className="text-rose-600 text-xs"
                        onClick={() => handleDelete(row)}
                      >
                        삭제
                      </button>
                    </td>
                  </tr>
                ))}
                {(data?.items || []).length === 0 && (
                  <tr>
                    <td colSpan={7} className="text-center py-8 text-ink-400 text-sm">
                      매핑이 없습니다. AI 매핑이 score≥50 으로 통과하면 자동 등록되거나, 직접 추가하세요.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {showForm && (
        <MappingFormModal
          editing={editing}
          onClose={() => setShowForm(false)}
          onSaved={() => {
            setShowForm(false);
            qc.invalidateQueries({ queryKey: ['pa', 'category-map'] });
          }}
        />
      )}
    </Card>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━ REVIEWS TAB ━━━━━━━━━━━━━━━━━━━
function ReviewsTab() {
  const qc = useQueryClient();
  const [status, setStatus] = useState('pending');
  const { data, isLoading } = useQuery({
    queryKey: ['pa', 'category-review', status],
    queryFn: () => pa.categoryReviews({ status }),
  });

  return (
    <Card>
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <select
            className="border rounded px-3 py-2 text-sm"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="pending">대기 중</option>
            <option value="approved">확정</option>
            <option value="rejected">거부</option>
            <option value="all">전체</option>
          </select>
          <div className="text-sm text-ink-500">총 {data?.total ?? 0}건</div>
        </div>

        {isLoading ? (
          <div className="text-ink-400 text-sm">로딩 중...</div>
        ) : (
          <div className="space-y-3">
            {(data?.items || []).map((row) => (
              <ReviewItem
                key={row.id}
                row={row}
                onChanged={() =>
                  qc.invalidateQueries({ queryKey: ['pa', 'category-review'] })
                }
              />
            ))}
            {(data?.items || []).length === 0 && (
              <div className="text-center py-8 text-ink-400 text-sm">검토 대기 항목이 없습니다.</div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

function ReviewItem({ row, onChanged }) {
  const [editingNaver, setEditingNaver] = useState(false);
  const [editingCoupang, setEditingCoupang] = useState(false);
  const [naverPick, setNaverPick] = useState({
    id: row.ai_naver_id, path: row.ai_naver_path,
  });
  const [coupangPick, setCoupangPick] = useState({
    code: row.ai_coupang_code, path: row.ai_coupang_path,
  });

  const approveMut = useMutation({
    mutationFn: (body) => pa.approveCategoryReview(row.id, body),
    onSuccess: onChanged,
  });
  const rejectMut = useMutation({
    mutationFn: (body) => pa.rejectCategoryReview(row.id, body),
    onSuccess: onChanged,
  });

  if (row.status !== 'pending') {
    return (
      <div className="border rounded-lg p-3 bg-ink-50/30">
        <div className="flex items-center justify-between">
          <div>
            <span className="font-mono text-xs text-ink-500 mr-2">#{row.id}</span>
            <span className="text-sm text-ink-700">{row.product_name}</span>
          </div>
          <StatusBadge variant={row.status === 'approved' ? 'success' : 'neutral'}>
            {row.status}
          </StatusBadge>
        </div>
        <div className="mt-2 text-xs text-ink-500">
          {row.status === 'approved' && (
            <>
              네이버: <span className="font-mono">{row.approved_naver_id}</span> ·
              쿠팡: <span className="font-mono">{row.approved_coupang_code}</span>
            </>
          )}
          {row.notes && <span className="ml-2">— {row.notes}</span>}
        </div>
      </div>
    );
  }

  return (
    <div className="border rounded-lg p-4">
      <div className="flex items-start justify-between mb-3">
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold text-ink-800">{row.product_name}</div>
          {row.product_name_en && (
            <div className="text-xs text-ink-400 mt-0.5">{row.product_name_en}</div>
          )}
          <div className="text-xs text-ink-500 mt-1">
            {row.keyword && <>키워드: <span className="font-mono">{row.keyword}</span> · </>}
            product_id: {row.product_id || '—'}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        {/* 네이버 */}
        <div className="border rounded p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold text-ink-700">네이버</span>
            <span className={`text-xs px-2 py-0.5 rounded ${
              row.ai_naver_score >= 70 ? 'bg-emerald-100 text-emerald-700'
                : row.ai_naver_score >= 50 ? 'bg-amber-100 text-amber-700'
                  : 'bg-rose-100 text-rose-700'
            }`}>
              score {row.ai_naver_score ?? 0}
            </span>
          </div>
          {!editingNaver ? (
            <div>
              <div className="font-mono text-sm">{naverPick.id || '—'}</div>
              <div className="text-xs text-ink-500">{naverPick.path}</div>
              {row.ai_naver_reason && (
                <div className="text-xs text-ink-400 mt-1 italic">{row.ai_naver_reason}</div>
              )}
              <button
                className="text-xs text-primary-600 mt-2"
                onClick={() => setEditingNaver(true)}
              >
                수정
              </button>
            </div>
          ) : (
            <CategorySearchInput
              channel="naver"
              onPick={(c) => {
                setNaverPick({ id: c.id, path: c.path });
                setEditingNaver(false);
              }}
              onCancel={() => setEditingNaver(false)}
            />
          )}
        </div>

        {/* 쿠팡 */}
        <div className="border rounded p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold text-ink-700">쿠팡</span>
            <span className={`text-xs px-2 py-0.5 rounded ${
              row.ai_coupang_score >= 70 ? 'bg-emerald-100 text-emerald-700'
                : row.ai_coupang_score >= 50 ? 'bg-amber-100 text-amber-700'
                  : 'bg-rose-100 text-rose-700'
            }`}>
              score {row.ai_coupang_score ?? 0}
            </span>
          </div>
          {!editingCoupang ? (
            <div>
              <div className="font-mono text-sm">{coupangPick.code || '—'}</div>
              <div className="text-xs text-ink-500">{coupangPick.path}</div>
              {row.ai_coupang_reason && (
                <div className="text-xs text-ink-400 mt-1 italic">{row.ai_coupang_reason}</div>
              )}
              <button
                className="text-xs text-primary-600 mt-2"
                onClick={() => setEditingCoupang(true)}
              >
                수정
              </button>
            </div>
          ) : (
            <CategorySearchInput
              channel="coupang"
              onPick={(c) => {
                setCoupangPick({ code: c.code, path: c.path });
                setEditingCoupang(false);
              }}
              onCancel={() => setEditingCoupang(false)}
            />
          )}
        </div>
      </div>

      <div className="flex justify-end gap-2 mt-3">
        <Button
          variant="outline"
          onClick={() => {
            if (!confirm('거부하시겠습니까?')) return;
            rejectMut.mutate({});
          }}
          disabled={rejectMut.isPending}
        >
          거부
        </Button>
        <Button
          variant="primary"
          onClick={() =>
            approveMut.mutate({
              naver_id: naverPick.id,
              naver_path: naverPick.path,
              coupang_code: coupangPick.code,
              coupang_path: coupangPick.path,
              save_to_dict: true,
            })
          }
          disabled={approveMut.isPending}
        >
          {approveMut.isPending ? '저장 중...' : '확정 (캐시 저장)'}
        </Button>
      </div>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━ MODALS / SEARCH ━━━━━━━━━━━━━
function MappingFormModal({ editing, onClose, onSaved }) {
  const [form, setForm] = useState({
    keyword: editing?.keyword || '',
    naver_category_id: editing?.naver_category_id || '',
    naver_category_path: editing?.naver_category_path || '',
    coupang_category_code: editing?.coupang_category_code || null,
    coupang_category_path: editing?.coupang_category_path || '',
    notes: editing?.notes || '',
  });
  const [naverSearch, setNaverSearch] = useState(false);
  const [coupangSearch, setCoupangSearch] = useState(false);

  const saveMut = useMutation({
    mutationFn: (body) =>
      editing ? pa.updateCategoryMapping(editing.id, body) : pa.createCategoryMapping(body),
    onSuccess: onSaved,
  });

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
        <div className="p-4 border-b flex items-center justify-between">
          <h2 className="text-lg font-semibold">{editing ? '매핑 수정' : '새 매핑'}</h2>
          <button onClick={onClose} className="text-ink-400 hover:text-ink-700">✕</button>
        </div>
        <div className="p-4 space-y-4">
          <div>
            <label className="block text-xs font-semibold text-ink-700 mb-1">키워드 (lowercase)</label>
            <input
              type="text"
              className="w-full border rounded px-3 py-2 text-sm font-mono"
              value={form.keyword}
              onChange={(e) => setForm({ ...form, keyword: e.target.value })}
              placeholder="예: collagen peptides powder"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-ink-700 mb-1">네이버 카테고리</label>
            <div className="flex items-center gap-2">
              <input
                type="text"
                className="border rounded px-2 py-1.5 text-sm font-mono w-32"
                placeholder="ID"
                value={form.naver_category_id || ''}
                onChange={(e) => setForm({ ...form, naver_category_id: e.target.value })}
              />
              <input
                type="text"
                className="border rounded px-2 py-1.5 text-sm flex-1"
                placeholder="path"
                value={form.naver_category_path || ''}
                onChange={(e) => setForm({ ...form, naver_category_path: e.target.value })}
              />
              <button
                className="text-xs text-primary-600 px-2"
                onClick={() => setNaverSearch(!naverSearch)}
              >
                {naverSearch ? '닫기' : '검색'}
              </button>
            </div>
            {naverSearch && (
              <CategorySearchInput
                channel="naver"
                onPick={(c) => {
                  setForm({ ...form, naver_category_id: c.id, naver_category_path: c.path });
                  setNaverSearch(false);
                }}
                onCancel={() => setNaverSearch(false)}
              />
            )}
          </div>

          <div>
            <label className="block text-xs font-semibold text-ink-700 mb-1">쿠팡 카테고리</label>
            <div className="flex items-center gap-2">
              <input
                type="number"
                className="border rounded px-2 py-1.5 text-sm font-mono w-32"
                placeholder="code"
                value={form.coupang_category_code || ''}
                onChange={(e) => setForm({ ...form, coupang_category_code: e.target.value ? parseInt(e.target.value) : null })}
              />
              <input
                type="text"
                className="border rounded px-2 py-1.5 text-sm flex-1"
                placeholder="path"
                value={form.coupang_category_path || ''}
                onChange={(e) => setForm({ ...form, coupang_category_path: e.target.value })}
              />
              <button
                className="text-xs text-primary-600 px-2"
                onClick={() => setCoupangSearch(!coupangSearch)}
              >
                {coupangSearch ? '닫기' : '검색'}
              </button>
            </div>
            {coupangSearch && (
              <CategorySearchInput
                channel="coupang"
                onPick={(c) => {
                  setForm({ ...form, coupang_category_code: c.code, coupang_category_path: c.path });
                  setCoupangSearch(false);
                }}
                onCancel={() => setCoupangSearch(false)}
              />
            )}
          </div>

          <div>
            <label className="block text-xs font-semibold text-ink-700 mb-1">노트 (선택)</label>
            <textarea
              className="w-full border rounded px-3 py-2 text-sm"
              rows={2}
              value={form.notes || ''}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
            />
          </div>
        </div>
        <div className="p-4 border-t flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>취소</Button>
          <Button
            variant="primary"
            onClick={() => saveMut.mutate(form)}
            disabled={!form.keyword || saveMut.isPending}
          >
            {saveMut.isPending ? '저장 중...' : '저장'}
          </Button>
        </div>
      </div>
    </div>
  );
}

function CategorySearchInput({ channel, onPick, onCancel }) {
  const [q, setQ] = useState('');
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!q || q.length < 2) {
      setResults([]);
      return;
    }
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const fn = channel === 'naver' ? pa.searchNaverCategory : pa.searchCoupangCategory;
        const res = await fn(q, 15);
        setResults(res?.items || []);
      } catch (e) {
        setResults([]);
      } finally {
        setLoading(false);
      }
    }, 300);
    return () => clearTimeout(t);
  }, [q, channel]);

  return (
    <div className="mt-2 border rounded p-2 bg-ink-50/40">
      <div className="flex gap-2 mb-2">
        <input
          type="text"
          className="border rounded px-2 py-1 text-sm flex-1"
          placeholder={`${channel === 'naver' ? '네이버' : '쿠팡'} 카테고리 검색 (예: 화면보호, 콜라겐)`}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          autoFocus
        />
        <button className="text-xs text-ink-500 px-2" onClick={onCancel}>
          취소
        </button>
      </div>
      {loading && <div className="text-xs text-ink-400">검색 중...</div>}
      <div className="max-h-48 overflow-y-auto space-y-1">
        {results.map((c) => (
          <button
            key={channel === 'naver' ? c.id : c.code}
            className="w-full text-left px-2 py-1 rounded text-xs hover:bg-white border border-transparent hover:border-ink-200"
            onClick={() => onPick(c)}
          >
            <span className="font-mono text-ink-700">
              {channel === 'naver' ? c.id : c.code}
            </span>
            <span className="text-ink-500 ml-2">{c.path}</span>
          </button>
        ))}
        {!loading && q.length >= 2 && results.length === 0 && (
          <div className="text-xs text-ink-400">결과 없음</div>
        )}
      </div>
    </div>
  );
}
