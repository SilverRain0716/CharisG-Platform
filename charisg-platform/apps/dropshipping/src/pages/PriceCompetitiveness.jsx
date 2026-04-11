import React from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  ScatterChart, Scatter, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ResponsiveContainer,
} from 'recharts';
import { Card } from '@charisg/ui';
import { ds } from '../api/ds.js';

export default function PriceCompetitiveness() {
  const { data, isLoading } = useQuery({
    queryKey: ['ds', 'products', 'price'],
    queryFn: () => ds.products({ limit: 500 }),
  });

  const points = (data?.items || [])
    .filter((p) => p.amazon_price_p75 && p.calculated_price)
    .map((p) => ({
      x: p.amazon_price_p75,
      y: p.source_price,
      name: p.product_name,
      group: p.matrix_group,
    }));

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">가격 경쟁력</h1>
        <p className="mt-1 text-sm text-ink-500">Amazon p75 (X) × CJ 매입가 (Y) — 대각선 아래 = 경쟁력 있음.</p>
      </header>

      <Card>
        {isLoading && <div className="p-6 text-sm text-ink-400">로딩 중...</div>}
        {!isLoading && (
          <div className="h-[480px]">
            <ResponsiveContainer>
              <ScatterChart margin={{ top: 20, right: 30, bottom: 10, left: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis type="number" dataKey="x" name="Amazon p75" unit="$" />
                <YAxis type="number" dataKey="y" name="CJ 매입" unit="$" />
                <ReferenceLine
                  segment={[{ x: 0, y: 0 }, { x: 70, y: 70 }]}
                  stroke="#94a3b8"
                  strokeDasharray="4 4"
                  label="break-even"
                />
                <Tooltip
                  cursor={{ strokeDasharray: '3 3' }}
                  content={({ active, payload }) => {
                    if (!active || !payload?.length) return null;
                    const p = payload[0].payload;
                    return (
                      <div className="rounded-md bg-white p-2 text-xs shadow-card ring-1 ring-ink-200">
                        <div className="font-semibold">{p.name}</div>
                        <div>p75 ${p.x?.toFixed(2)} · CJ ${p.y?.toFixed(2)}</div>
                        <div className="text-ink-500">{p.group}</div>
                      </div>
                    );
                  }}
                />
                <Scatter data={points} fill="#0d9488" />
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>
    </div>
  );
}
