import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, StatusBadge } from '@charisg/ui';
import { ds } from '../api/ds.js';

export default function CrawlerManagement() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ['ds', 'crawler'], queryFn: ds.crawlerStatus });

  const run = useMutation({
    mutationFn: (crawler) => ds.runCrawler({ crawler }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ds', 'crawler'] }),
  });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">크롤러</h1>
        <p className="mt-1 text-sm text-ink-500">
          CJ / Amazon Keyword / Google Trends. EC2에서 GitHub Actions 또는 cron 으로 실행.
        </p>
      </header>

      {isLoading && <div className="text-sm text-ink-400">로딩 중...</div>}

      {data && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          {data.crawlers.map((c) => (
            <Card key={c.id} title={c.label}>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between"><span className="text-ink-500">수집 건수</span><span className="font-medium">{c.count?.toLocaleString() || '—'}</span></div>
                <div className="flex justify-between"><span className="text-ink-500">상태</span><StatusBadge variant="info">{c.status}</StatusBadge></div>
                <div className="flex justify-between"><span className="text-ink-500">마지막 실행</span><span>{c.last_run || '—'}</span></div>
              </div>
              <div className="mt-4">
                <Button variant="ds" size="sm" onClick={() => run.mutate(c.id)} disabled={run.isPending}>
                  실행
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
