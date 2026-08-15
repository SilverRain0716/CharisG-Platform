import React from 'react';
import { MetricStrip, Metric } from './MetricStrip.jsx';

/**
 * KPICard — 구버전 호환 래퍼.
 *
 * 새 화면은 MetricStrip + Metric 을 쓴다(한 줄에 여러 지표, 헤어라인 구분).
 * 아직 KPICard 를 쓰는 화면이 남아 있어 같은 모양으로 흘려보낸다.
 * 새로 쓰지 말 것.
 */
export function KPICard({ label, value, delta, trend, hint }) {
  const signed = delta == null ? null : trend === 'down' ? -Math.abs(delta) : Math.abs(delta);
  return (
    <MetricStrip cols={1}>
      <Metric label={label} value={value} delta={signed} hint={hint} />
    </MetricStrip>
  );
}
