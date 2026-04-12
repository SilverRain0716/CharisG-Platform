import React, { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

const PRICING_FIELDS = [
  { key: 'margin_target_rate', label: '목표 마진율', unit: '%', multiplier: 100, step: 0.5, desc: '판매가 역산 시 적용되는 목표 마진' },
  { key: 'smartstore_fee_rate', label: '스마트스토어 수수료', unit: '%', multiplier: 100, step: 0.01, desc: '네이버 스마트스토어 판매 수수료' },
  { key: 'coupang_fee_rate', label: '쿠팡 수수료', unit: '%', multiplier: 100, step: 0.01, desc: '쿠팡 판매 수수료' },
  { key: 'amazon_shipping_default_usd', label: '아마존 기본 배송비', unit: 'USD', multiplier: 1, step: 0.5, desc: '아마존→배대지 기본 배송비' },
  { key: 'cj_shipping_default_usd_per_kg', label: 'CJ 배송비 (kg당)', unit: 'USD', multiplier: 1, step: 0.1, desc: '배대지→국내 kg당 배송비' },
  { key: 'image_retention_days', label: '이미지 보관일', unit: '일', multiplier: 1, step: 1, desc: '업로드 전 이미지 캐시 보관 기간' },
];

function PricingForm() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ['pa', 'pricing'], queryFn: pa.pricingSettings });
  const [form, setForm] = useState({});
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (data) {
      const init = {};
      PRICING_FIELDS.forEach(({ key, multiplier }) => {
        const v = data[key];
        init[key] = v != null ? +(v * multiplier).toFixed(4) : '';
      });
      setForm(init);
      setDirty(false);
    }
  }, [data]);

  const saveMut = useMutation({
    mutationFn: (body) => pa.updatePricing(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pa', 'pricing'] });
      qc.invalidateQueries({ queryKey: ['pa', 'settings'] });
      setDirty(false);
    },
  });

  const handleChange = (key, value) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    setDirty(true);
  };

  const handleSave = () => {
    const body = {};
    PRICING_FIELDS.forEach(({ key, multiplier }) => {
      const v = form[key];
      if (v !== '' && v != null) {
        body[key] = multiplier === 1 ? Number(v) : Number(v) / multiplier;
      }
    });
    saveMut.mutate(body);
  };

  if (isLoading) return <div className="text-sm text-ink-400">로딩 중...</div>;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {PRICING_FIELDS.map(({ key, label, unit, step, desc }) => (
          <div key={key} className="rounded-lg border border-ink-200 bg-white p-4">
            <label className="block text-sm font-semibold text-ink-700 mb-1">{label}</label>
            <p className="text-xs text-ink-400 mb-2">{desc}</p>
            <div className="flex items-center gap-2">
              <input
                type="number"
                step={step}
                value={form[key] ?? ''}
                onChange={(e) => handleChange(key, e.target.value)}
                className="flex-1 rounded-md border border-ink-300 px-3 py-1.5 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none"
              />
              <span className="text-sm text-ink-500 font-medium w-10">{unit}</span>
            </div>
          </div>
        ))}
      </div>
      <div className="flex items-center gap-3">
        <Button variant="pa" disabled={!dirty || saveMut.isPending} onClick={handleSave}>
          {saveMut.isPending ? '저장 중…' : '마진 설정 저장'}
        </Button>
        {saveMut.isSuccess && <span className="text-sm text-green-600">저장 완료</span>}
        {data?.exchange_rate_usd_krw && (
          <span className="ml-auto text-xs text-ink-400">
            현재 환율: ₩{Number(data.exchange_rate_usd_krw).toLocaleString()} / USD
            {data.exchange_rate_updated_at && ` (${data.exchange_rate_updated_at})`}
          </span>
        )}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ['pa', 'settings'], queryFn: pa.settings });
  const catsQ = useQuery({ queryKey: ['pa', 'discoveryCategories'], queryFn: pa.discoveryCategories });

  const syncMut = useMutation({
    mutationFn: pa.syncCategories,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['pa', 'discoveryCategories'] }),
  });
  const toggleMut = useMutation({
    mutationFn: ({ cid, tracked }) => pa.toggleCategory(cid, tracked),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['pa', 'discoveryCategories'] }),
  });

  const topLevelCats = (catsQ.data || []).filter((c) => c.level === 1);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">설정</h1>
        <p className="mt-1 text-sm text-ink-500">마진 파라미터, 크롤 스케줄, 알림 채널, API 연동 상태.</p>
      </header>

      <Card title="마진 / 수수료 설정">
        <PricingForm />
      </Card>

      {isLoading && <div className="text-sm text-ink-400">로딩 중...</div>}

      {data && (
        <>
          <Card title="API 연동 상태">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
              {Object.entries(data.integrations || {}).map(([k, v]) => (
                <div key={k} className="flex items-center justify-between rounded-md border border-ink-200 bg-white px-3 py-2">
                  <span className="text-sm text-ink-700">{k}</span>
                  <StatusBadge variant={v ? 'ok' : 'err'}>{v ? '연결됨' : '미설정'}</StatusBadge>
                </div>
              ))}
            </div>
          </Card>

          <Card title="기타 설정값">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
              {Object.entries(data.settings || {})
                .filter(([k]) => !PRICING_FIELDS.some((f) => f.key === k))
                .map(([k, v]) => (
                <div key={k} className="rounded-md border border-ink-200 bg-ink-50 px-3 py-2">
                  <div className="text-xs text-ink-500">{k}</div>
                  <div className="text-sm font-semibold text-ink-900">{String(v)}</div>
                </div>
              ))}
            </div>
          </Card>
        </>
      )}

      <Card
        title="디스커버리 카테고리 추적"
        action={
          <Button size="sm" variant="secondary" onClick={() => syncMut.mutate()} disabled={syncMut.isPending}>
            {syncMut.isPending ? '동기화 중…' : '카테고리 트리 동기화'}
          </Button>
        }
      >
        <p className="mb-3 text-xs text-ink-500">
          선택한 카테고리에서 네이버 데이터랩 TOP 100 키워드를 수집합니다. 풀 파이프라인은 디스커버리 페이지에서 실행.
        </p>
        {catsQ.isLoading && <div className="text-sm text-ink-400">로딩 중...</div>}
        {!catsQ.isLoading && topLevelCats.length === 0 && (
          <div className="rounded-md border border-dashed border-ink-200 bg-ink-50 px-4 py-6 text-center text-sm text-ink-500">
            카테고리 없음. 먼저 "카테고리 트리 동기화" 버튼을 눌러 주세요.
          </div>
        )}
        {topLevelCats.length > 0 && (
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
            {topLevelCats.map((c) => (
              <label
                key={c.cid}
                className="flex cursor-pointer items-center gap-2 rounded-md border border-ink-200 bg-white px-3 py-2 hover:bg-ink-50"
              >
                <input
                  type="checkbox"
                  checked={!!c.tracked}
                  onChange={(e) => toggleMut.mutate({ cid: c.cid, tracked: e.target.checked })}
                  disabled={toggleMut.isPending}
                />
                <span className="text-sm text-ink-900">{c.name}</span>
                <span className="ml-auto text-[11px] text-ink-400">cid={c.cid}</span>
              </label>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
