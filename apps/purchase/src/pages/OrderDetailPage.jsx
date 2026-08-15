import React, { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

const ORDER_STEPS = [
  ['order_received',  '주문 접수'],
  ['amazon_purchase', '아마존 구매'],
  ['invoice_registered', '송장등록(배송지시)'],
  ['in_transit',      '배송중'],
  ['completed',       '완료'],
];
const STEP_INDEX = Object.fromEntries(ORDER_STEPS.map(([k], i) => [k, i]));
const labelOf = (k) => ORDER_STEPS.find(([key]) => key === k)?.[1] || k;
const nextStep = (cur) => {
  const i = STEP_INDEX[cur];
  if (i == null || i >= ORDER_STEPS.length - 1) return null;
  return ORDER_STEPS[i + 1][0];
};

// 한국 휴대폰 하이픈 포맷: 01047385608 → 010-4738-5608 (알 수 없는 형식은 원본 유지)
const formatPhoneKR = (p) => {
  if (!p) return p;
  const d = String(p).replace(/\D/g, '');
  if (d.length === 11) return `${d.slice(0, 3)}-${d.slice(3, 7)}-${d.slice(7)}`;
  if (d.length === 10) {
    if (d.startsWith('02')) return `${d.slice(0, 2)}-${d.slice(2, 6)}-${d.slice(6)}`;
    return `${d.slice(0, 3)}-${d.slice(3, 6)}-${d.slice(6)}`;
  }
  return p;
};

// ──────────────────────────────────────────
// 문자 발송 — 자동 발송 없음. 여기서 직접 써서 보낸다.
//
// 안심번호(050)는 LMS가 막혀 있어 EUC-KR 90byte 단문만 가능하다.
// 실번호는 통관 목적으로 받은 번호라 사유를 고른 경우에만 열린다.

const SMS_MAX_BYTES = 90;

const REAL_REASONS = [
  { value: 'customs_error', label: '개인통관부호 오류' },
  { value: 'address_unclear', label: '주소 불명확' },
  { value: 'safe_number_failed', label: '안심번호 발송 실패' },
];

// 전부 EUC-KR 90byte 이내 (안심번호 단문 한도). 문안 수정 시 byte 재확인 필요.
const SMS_TEMPLATES = [
  { label: '품절', text: '주문하신 상품이 현지 품절되어 부득이 취소 처리됩니다. 불편을 드려 죄송합니다.' },
  { label: '배송지연', text: '주문하신 상품 배송이 지연되고 있습니다. 확인 후 다시 안내드리겠습니다.' },
  { label: '통관부호', text: '개인통관고유부호 오류로 통관이 불가합니다. 정확한 번호를 알려주세요.' },
  { label: '주소확인', text: '배송지 주소가 불명확하여 확인이 필요합니다. 정확한 주소를 알려주세요.' },
  { label: '배송불가', text: '주문하신 상품이 해외배송 제한 품목으로 부득이 취소 처리됩니다. 죄송합니다.' },
  { label: '가격오류', text: '상품 가격이 잘못 기재되어 부득이 취소 처리됩니다. 불편을 드려 죄송합니다.' },
  { label: '옵션확인', text: '주문하신 상품의 옵션 확인이 필요합니다. 연락 부탁드립니다.' },
  { label: '반품안내', text: '반품 접수되었습니다. 회수 후 순차적으로 환불 처리해 드리겠습니다.' },
];

/** EUC-KR 기준 byte 길이 — 한글 2byte, ASCII 1byte. 서버 검증과 같은 기준. */
function euckrBytes(text) {
  let n = 0;
  for (const ch of String(text || '')) {
    n += ch.charCodeAt(0) > 127 ? 2 : 1;
  }
  return n;
}

function SmsCard({ oid, customer }) {
  const qc = useQueryClient();
  const [numberType, setNumberType] = useState('safe');
  const [reason, setReason] = useState('customs_error');
  const [text, setText] = useState('');

  const history = useQuery({
    queryKey: ['pa', 'order-sms', oid],
    queryFn: () => pa.smsHistory(oid),
    enabled: !Number.isNaN(oid),
  });
  const balance = useQuery({
    queryKey: ['pa', 'sms-balance'],
    queryFn: () => pa.smsBalance(),
    retry: false,
    staleTime: 60 * 1000,
  });

  const send = useMutation({
    mutationFn: () => pa.sendSms(oid, {
      number_type: numberType,
      text: text.trim(),
      reason: numberType === 'real' ? reason : null,
    }),
    onSuccess: () => {
      setText('');
      qc.invalidateQueries({ queryKey: ['pa', 'order-sms', oid] });
      qc.invalidateQueries({ queryKey: ['pa', 'sms-balance'] });
    },
  });

  const target = numberType === 'safe' ? customer?.phone_safe : customer?.phone_real;
  const bytes = euckrBytes(text);
  const overLimit = numberType === 'safe' && bytes > SMS_MAX_BYTES;
  const blocked = numberType === 'safe' && String(target || '').replace(/\D/g, '').startsWith('0508');
  const canSend = !!target && !!text.trim() && !overLimit && !blocked && !send.isPending;
  const totalCash = balance.data ? (balance.data.balance || 0) + (balance.data.point || 0) : null;

  return (
    <Card title="문자 보내기">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-[11px]">
        {balance.isLoading && <span className="text-ink-400">잔액 확인 중…</span>}
        {balance.isError && <span className="text-signal-err">잔액 조회 실패: {String(balance.error?.message || balance.error)}</span>}
        {balance.data && (
          <>
            <span className={totalCash <= 0 ? 'font-medium text-signal-err' : 'text-ink-500'}>
              잔액 {Math.floor(balance.data.balance).toLocaleString()}원
              {balance.data.point > 0 && ` + 포인트 ${Math.floor(balance.data.point).toLocaleString()}P`}
            </span>
            {totalCash <= 0 && <span className="text-signal-err">— 충전 전까지 발송 실패</span>}
            <span className="text-ink-400">발신 {balance.data.sender || '미설정'}</span>
          </>
        )}
      </div>

      <div className="mb-3 space-y-1.5 text-xs">
        <label className="flex flex-wrap items-center gap-2">
          <input type="radio" checked={numberType === 'safe'} onChange={() => setNumberType('safe')} />
          <span>안심번호</span>
          <span className="font-mono text-ink-700">{customer?.phone_safe || '—'}</span>
          <span className="text-[10px] text-ink-400">단문 90byte 제한</span>
        </label>
        <label className="flex flex-wrap items-center gap-2">
          <input
            type="radio"
            checked={numberType === 'real'}
            onChange={() => setNumberType('real')}
            disabled={!customer?.phone_real}
          />
          <span>실번호</span>
          <span className="font-mono text-ink-700">{customer?.phone_real || '—'}</span>
          {numberType === 'real' && (
            <select
              className="rounded border border-ink-200 px-1.5 py-0.5 text-[11px]"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            >
              {REAL_REASONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          )}
        </label>
        {numberType === 'real' && (
          <p className="text-[10px] text-signal-warn">
            실번호는 통관 목적으로 수집한 번호입니다. 안심번호로 해결되지 않는 건에만 사용하세요.
          </p>
        )}
      </div>

      {/* 템플릿은 본문을 채워넣기만 한다. '직접입력'으로 언제든 빈 상태로 되돌린다. */}
      <div className="mb-2 flex flex-wrap items-center gap-1">
        <Button
          size="sm"
          variant={text ? 'ghost' : 'secondary'}
          onClick={() => setText('')}
          disabled={!text}
          title="본문을 비우고 직접 작성"
        >
          직접입력
        </Button>
        <span className="mx-0.5 text-ink-200">|</span>
        {SMS_TEMPLATES.map((t) => (
          <Button key={t.label} size="sm" variant="ghost" onClick={() => setText(t.text)}>{t.label}</Button>
        ))}
      </div>

      <textarea
        className="w-full rounded border border-ink-200 p-2 text-xs"
        rows={3}
        value={text}
        placeholder="보낼 내용을 입력하세요"
        onChange={(e) => setText(e.target.value)}
      />

      <div className="mt-2 flex items-center justify-between gap-2">
        <span className={`text-[11px] ${overLimit ? 'font-medium text-signal-err' : 'text-ink-400'}`}>
          {bytes}{numberType === 'safe' ? ` / ${SMS_MAX_BYTES} byte` : ' byte'}
          {overLimit && ` — ${bytes - SMS_MAX_BYTES}byte 초과, 안심번호는 장문 불가`}
          {numberType === 'real' && bytes > SMS_MAX_BYTES && ' (LMS로 발송)'}
        </span>
        <Button onClick={() => send.mutate()} disabled={!canSend}>
          {send.isPending ? '발송 중…' : '발송'}
        </Button>
      </div>

      {blocked && <p className="mt-2 text-[11px] text-signal-err">0508 안심번호는 문자 수신이 불가합니다. 실번호를 사용하세요.</p>}
      {send.isError && <p className="mt-2 text-[11px] text-signal-err">발송 실패: {String(send.error?.message || send.error)}</p>}

      {history.data?.length > 0 && (
        <div className="mt-3 border-t border-ink-100 pt-2">
          <div className="mb-1.5 text-[10px] font-mono uppercase tracking-wider text-ink-500">발송 이력</div>
          <ul className="space-y-1.5">
            {history.data.map((h) => (
              <li key={h.id} className="text-[11px]">
                <div className="flex flex-wrap items-center gap-1.5 text-ink-400">
                  <span>{h.sent_at}</span>
                  <span className="font-mono">{h.to_number}</span>
                  <span>{h.number_type === 'safe' ? '안심' : '실번호'}</span>
                  <span>{h.msg_type}</span>
                  <StatusBadge variant={h.status === 'sent' ? 'ok' : 'err'}>
                    {h.status === 'sent' ? '발송' : '실패'}
                  </StatusBadge>
                </div>
                <div className="text-ink-700">{h.text}</div>
                {h.error_msg && <div className="text-signal-err">{h.error_msg}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

export default function OrderDetailPage() {
  const { id } = useParams();
  const oid = Number(id);
  const navigate = useNavigate();
  const qc = useQueryClient();

  const prep = useQuery({
    queryKey: ['pa', 'order-prep', oid],
    queryFn: () => pa.orderAmazonPrep(oid),
    enabled: !Number.isNaN(oid),
  });
  const release = useQuery({
    queryKey: ['pa', 'release-address'],
    queryFn: () => pa.releaseAddress(),
    staleTime: 60 * 60 * 1000,
  });

  const advance = useMutation({
    mutationFn: ({ step, note }) => pa.advance(oid, step, note || ''),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pa', 'order-prep', oid] });
      qc.invalidateQueries({ queryKey: ['pa', 'orders'] });
    },
  });

  if (prep.isLoading) return <div className="text-sm text-ink-400">로딩 중…</div>;
  if (prep.isError) {
    return (
      <div className="space-y-4">
        <BackBtn onClick={() => navigate('/orders')} />
        <div className="text-sm text-signal-err">불러오기 실패: {String(prep.error?.message || prep.error)}</div>
      </div>
    );
  }
  if (!prep.data) return null;

  const { order, product, amazon_url, customer, match_status } = prep.data;
  const sm = order.shipping_method;
  const nxt = nextStep(order.current_step);

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-2">
        <BackBtn onClick={() => navigate('/orders')} />
        <h1 className="min-w-0 flex-1 text-right text-base font-semibold text-ink-900 [word-break:keep-all] sm:text-xl">
          주문 #{order.id} <span className="text-ink-500">·</span> {order.channel}{' '}
          <span className="font-mono text-xs text-ink-500 sm:text-sm">({order.channel_order_id || '—'})</span>
        </h1>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* 좌측: 주문 + 상품 + 고객(한글) */}
        <div className="space-y-4">
          <Card title="주문 정보">
            <Row k="채널" v={`${order.channel} (${order.channel_order_id || '—'})`} />
            <Row k="주문 일시" v={order.placed_at || '—'} mono />
            {order.ordered_at && <Row k="결제 시각" v={order.ordered_at} mono />}
            <Row k="판매가" v={order.sale_price_krw != null ? `₩${Number(order.sale_price_krw).toLocaleString()}` : '—'} />
            <Row k="수량" v={order.quantity ?? 1} />
            <Row k="현재 단계" v={<StatusBadge variant="info">{labelOf(order.current_step)}</StatusBadge>} />
            <Row k="배송 방식" v={sm ? <StatusBadge variant={sm === 'direct' ? 'ok' : 'info'}>{sm}</StatusBadge> : <Missing>미정</Missing>} />
            <Row k="Amazon 주문" v={order.amazon_order_id || <Missing>발주 전</Missing>} mono />
            {order.external_sku && <Row k="외부 SKU" v={order.external_sku} mono />}
            {nxt && (
              <div className="mt-3 flex items-center justify-end gap-2">
                <span className="text-[11px] text-ink-500">→ {labelOf(nxt)}</span>
                <Button onClick={() => advance.mutate({ step: nxt, note: '상세 페이지' })} disabled={advance.isPending}>
                  {advance.isPending ? '진행 중…' : '다음 단계로'}
                </Button>
              </div>
            )}
          </Card>

          <Card title="상품">
            {product ? (
              <>
                <Row k="ASIN" v={
                  <a href={amazon_url} target="_blank" rel="noreferrer" className="font-mono text-signal-info hover:underline">
                    {product.asin} ↗
                  </a>
                } />
                <Row k="상품명(한)" v={product.title_ko || '—'} />
                <Row k="상품명(영)" v={product.title_en || '—'} />
                <Row k="브랜드" v={product.brand || '—'} />
                <Row k="원가/중량" v={`$${product.cost_usd ?? '—'} · ${product.weight_g ?? '—'}g`} />
                <Row k="목표 마진" v={product.margin_pct != null ? `${product.margin_pct}%` : '—'} />
              </>
            ) : (
              <div className="text-xs text-signal-err">
                ⚠ 매칭된 상품 없음 ({match_status === 'missing_product' ? '쿠팡 sellerProductId 미등록' : 'ASIN 누락'})
              </div>
            )}
          </Card>

          <Card title="고객 (한글)">
            <Row k="이름" v={customer.name_ko || <Missing />} />
            <Row k="안심번호" v={customer.phone_safe || '—'} mono />
            <Row k="실휴대폰" v={customer.phone_real || <Missing />} mono />
            <Row k="주소" v={customer.address_ko || <Missing />} />
            <Row k="배송 메시지" v={customer.shipping_message || '—'} />
          </Card>

          <SmsCard oid={oid} customer={customer} />
        </div>

        {/* 우측: 통관(영문) + 양식 분기 + 워크플로우 */}
        <div className="space-y-4">
          <Card title="통관 정보 (필수)">
            <Row k="고객명(영)" v={customer.name_en || <Missing />} />
            <Row k="주소(영)" v={customer.address_en || <Missing />} />
            <Row k="개인통관" v={customer.customs_code || <Missing />} mono />
            <Row k="번역 상태" v={
              customer.translation_status === 'done'
                ? <StatusBadge variant="ok">done</StatusBadge>
                : <StatusBadge variant="warn">{customer.translation_status || 'pending'}</StatusBadge>
            } />
          </Card>

          {/* 양식 분기 */}
          {(sm === 'direct' || !sm) && (
            <Card title="📦 양식 ① 아마존 직배송 (direct)">
              <p className="mb-2 text-[11px] text-ink-600">
                Amazon Ship-To 에 <b>고객 영문 정보 그대로</b> 입력합니다. 통관부호(PCCC)는 별도 필드.
              </p>
              <CopyableForm
                lines={[
                  `Name: ${customer.name_en || '(미수집)'}`,
                  `Phone: ${formatPhoneKR(customer.phone_real || customer.phone_safe) || '(미수집)'}`,
                  `Address: ${customer.address_en || '(미수집)'}`,
                  `PCCC: ${customer.customs_code || '(미수집)'}`,
                ]}
              />
            </Card>
          )}

          {(sm === 'forwarder' || !sm) && (
            <Card title="🚛 양식 ② 배대지 경유 (forwarder)">
              <p className="mb-2 text-[11px] text-ink-600">
                Amazon Ship-To 에 <b>배대지 미국 주소</b> 를 입력. 도착 후 한국 고객 한글 정보로 국내 발송.
              </p>
              {release.isLoading && <div className="text-xs text-ink-400">배대지 주소 로딩 중…</div>}
              {release.isError && <div className="text-xs text-signal-err">배대지 주소 조회 실패: {String(release.error?.message || release.error)}</div>}
              {release.data && (
                <CopyableForm
                  lines={[
                    `Name: ${release.data.name || ''}`,
                    `Phone: ${release.data.phone1 || ''}`,
                    `Address: ${release.data.raw_address || (release.data.detail_address + ', ' + release.data.base_address)}`,
                    `ZIP: ${release.data.postal_code || ''}`,
                  ]}
                />
              )}
              <div className="mt-3 rounded-md bg-ink-50 p-2 ring-1 ring-ink-100">
                <div className="mb-1 text-[10px] font-mono uppercase tracking-wider text-ink-500">국내 발송 (배대지 → 한국)</div>
                <Row k="고객" v={customer.name_ko || <Missing />} />
                <Row k="전화" v={customer.phone_real || customer.phone_safe || <Missing />} mono />
                <Row k="주소" v={customer.address_ko || <Missing />} />
              </div>
            </Card>
          )}

          {/* 워크플로우 6단계 */}
          <Card title="진행 단계">
            <ol className="space-y-1.5">
              {ORDER_STEPS.map(([key, lbl], i) => {
                const idx = STEP_INDEX[order.current_step] ?? -1;
                const done = i < idx;
                const current = i === idx;
                return (
                  <li
                    key={key}
                    className={
                      'flex items-center gap-3 rounded-md px-3 py-1.5 ring-1 ' +
                      (done
                        ? 'bg-soft-ok ring-signal-ok/30'
                        : current
                          ? 'bg-soft-warn ring-signal-warn/30'
                          : 'bg-ink-50 ring-ink-100')
                    }
                  >
                    <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-surface text-[10px] font-bold ring-1 ring-ink-200">
                      {done ? '✓' : i + 1}
                    </span>
                    <div className="flex-1 text-xs font-semibold text-ink-900">{lbl}</div>
                  </li>
                );
              })}
            </ol>
          </Card>
        </div>
      </div>
    </div>
  );
}

function BackBtn({ onClick }) {
  return (
    <button onClick={onClick} className="text-sm text-ink-500 hover:text-ink-900">
      ← 주문·CS 로
    </button>
  );
}

function Row({ k, v, mono }) {
  // 모바일(< sm): 라벨 위, 값 아래로 적층 — 좁은 화면에서 한 글자씩 깨지는 현상 방지
  // 데스크탑(sm+): 라벨 96px 좌측 고정 + 값 우측
  // [break-words] 는 CJK 어절 break 단위가 없어 한국어에 무력 — keep-all 로 단어 단위 줄바꿈
  return (
    <div className="flex flex-col gap-0.5 py-1 text-xs sm:flex-row sm:gap-2 sm:py-0.5">
      <span className="font-mono text-ink-500 sm:w-24 sm:shrink-0">{k}</span>
      <span className={'min-w-0 flex-1 text-ink-900 [word-break:keep-all] [overflow-wrap:anywhere]' + (mono ? ' font-mono' : '')}>{v}</span>
    </div>
  );
}

function Missing({ children }) {
  return <span className="font-semibold text-signal-err">⚠ {children || '미수집'}</span>;
}

function CopyableForm({ lines }) {
  const text = lines.join('\n');
  const [copied, setCopied] = React.useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // fallback
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };
  return (
    <div className="space-y-2">
      <pre className="whitespace-pre-wrap rounded-md bg-ink-900 p-2.5 text-[11px] leading-relaxed text-ink-50 font-mono">{text}</pre>
      <div className="flex justify-end">
        <Button onClick={onCopy}>{copied ? '✓ 복사됨' : '복사'}</Button>
      </div>
    </div>
  );
}
