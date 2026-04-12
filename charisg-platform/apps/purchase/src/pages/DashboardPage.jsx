import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, KPICard, FunnelChart, Button } from '@charisg/ui';
import { pa } from '../api/pa.js';

const PROMPT_URL = '/purchase/prompts/amazon_kr_sourcing_v3.1.md';

function PromptCard() {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(null);

  const handleCopy = async () => {
    setError(null);
    try {
      const res = await fetch(PROMPT_URL);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (e) {
      setError(e.message || '알 수 없는 오류');
      setTimeout(() => setError(null), 3000);
    }
  };

  return (
    <Card title="Amazon 소싱 프롬프트 (v3.1)">
      <p className="text-sm text-ink-600 mb-4">
        Claude.ai 웹 프로젝트 시스템 프롬프트에 붙여넣으면 디스커버리 키워드를 ASIN 10컬럼 구글시트로 변환합니다.
      </p>
      <div className="flex items-center gap-2">
        <a
          href={PROMPT_URL}
          download="amazon_kr_sourcing_v3.1.md"
          className="inline-flex items-center rounded-md bg-brand-pa-500 px-4 py-2 text-sm font-medium text-white hover:bg-brand-pa-600"
        >
          다운로드
        </a>
        <Button variant="ghost" size="sm" onClick={handleCopy}>
          {copied ? '복사됨 ✓' : '클립보드 복사'}
        </Button>
      </div>
      {error && (
        <div className="mt-3 text-sm text-red-600">
          복사 실패: {error} (HTTPS 환경에서만 동작합니다)
        </div>
      )}
    </Card>
  );
}

export default function DashboardPage() {
  const { data, isLoading } = useQuery({ queryKey: ['pa', 'dashboard'], queryFn: pa.dashboard });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">대시보드</h1>
        <p className="mt-1 text-sm text-ink-500">미국 아마존 → 한국 구매대행 파이프라인 조감.</p>
      </header>

      <PromptCard />

      {isLoading && <div className="text-sm text-ink-400">로딩 중...</div>}

      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <KPICard label="활성 상품"   value={data.kpis?.active_products?.toLocaleString() || 0} accent="pa" />
            <KPICard label="평균 마진"   value={`${data.kpis?.avg_margin || 0}%`} accent="pa" />
            <KPICard label="GO 대기"     value={data.todos?.go_pending || 0} accent="pa" />
            <KPICard label="업로드 대기" value={data.todos?.upload_pending || 0} accent="pa" />
            <KPICard label="미처리 CS"   value={data.todos?.cs_open || 0} accent="pa" />
          </div>

          <Card title="파이프라인 퍼널">
            <FunnelChart
              stages={(data.funnel || []).map((s) => ({ ...s, color: 'bg-brand-pa-500' }))}
            />
          </Card>
        </>
      )}
    </div>
  );
}
