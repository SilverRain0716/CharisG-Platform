import React from 'react';
import { Card } from '@charisg/ui';

export default function Performance() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">매출·성과</h1>
        <p className="mt-1 text-sm text-ink-500">Phase 1+ 활성화 — 현재 Phase 0 (오가닉 판매 전).</p>
      </header>

      <Card>
        <div className="text-center">
          <div className="text-sm text-ink-500">목표 $500</div>
          <div className="mt-2 h-3 w-full overflow-hidden rounded-full bg-ink-100">
            <div className="h-full w-0 bg-brand-ds-500" />
          </div>
          <div className="mt-2 text-xs text-ink-400">$0 / $500</div>
        </div>
      </Card>

      <Card title="일/주/월 매출">
        <div className="flex h-48 items-center justify-center text-sm text-ink-400">
          Phase 1 도달 시 활성화
        </div>
      </Card>
    </div>
  );
}
