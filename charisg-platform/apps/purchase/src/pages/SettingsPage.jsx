import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

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

          <Card title="설정값">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
              {Object.entries(data.settings || {}).map(([k, v]) => (
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
