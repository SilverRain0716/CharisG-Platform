import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

export default function SettingsPage() {
  const { data, isLoading } = useQuery({ queryKey: ['pa', 'settings'], queryFn: pa.settings });

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
    </div>
  );
}
