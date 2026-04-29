import React, { useState } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

export default function OrderDetailPage() {
  const { oid } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const prep = useQuery({
    queryKey: ['pa', 'order', oid, 'amazon-prep'],
    queryFn: () => pa.orderAmazonPrep(oid),
  });
  const release = useQuery({
    queryKey: ['pa', 'release-address'],
    queryFn: pa.releaseAddress,
    retry: false,
  });

  const [shippingMethod, setShippingMethod] = useState('forwarder');
  const [amazonOrderId, setAmazonOrderId] = useState('');

  const submit = useMutation({
    mutationFn: () => pa.setAmazonOrder(oid, {
      amazon_order_id: amazonOrderId.trim(),
      shipping_method: shippingMethod,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pa', 'order', oid] });
      qc.invalidateQueries({ queryKey: ['pa', 'orders'] });
      navigate('/orders');
    },
  });

  if (prep.isLoading) return <div className="text-sm text-ink-400">로딩 중...</div>;
  if (prep.error) return <div className="text-sm text-red-600">조회 실패: {prep.error.message}</div>;

  const { order, product, amazon_url: amazonUrl, customer, match_status: matchStatus, option } = prep.data || {};

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="mb-1 text-xs text-ink-400">
            <Link to="/orders" className="hover:text-ink-700">← 주문·CS</Link>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">
            주문 #{order?.id} <span className="text-ink-400 font-normal">· {order?.channel_order_id}</span>
          </h1>
          <div className="mt-1 flex items-center gap-2 text-sm text-ink-500">
            <StatusBadge variant={order?.channel === 'coupang' ? 'warn' : 'neutral'}>
              {order?.channel}
            </StatusBadge>
            <span>현재 단계: {order?.current_step}</span>
            {order?.amazon_order_id && (
              <span className="text-ink-700">· Amazon #{order.amazon_order_id}</span>
            )}
          </div>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <CustomerCard customer={customer} order={order} />
        <AmazonProductCard product={product} amazonUrl={amazonUrl} matchStatus={matchStatus} order={order} option={option} />
      </div>

      <ShipToForwarderCard
        customer={customer}
        release={release.data}
        releaseError={release.error?.message}
        onRefresh={() => pa.refreshReleaseAddress().then(() => release.refetch())}
      />

      <ShipToDirectCard customer={customer} />

      <AmazonOrderForm
        order={order}
        shippingMethod={shippingMethod}
        setShippingMethod={setShippingMethod}
        amazonOrderId={amazonOrderId}
        setAmazonOrderId={setAmazonOrderId}
        onSubmit={() => submit.mutate()}
        pending={submit.isPending}
        alreadyDone={!!order?.amazon_order_id}
        error={submit.error?.message}
      />
    </div>
  );
}

// ──────────────────────────────────────────
function CopyButton({ text, label = '복사' }) {
  const [copied, setCopied] = useState(false);
  const handle = async () => {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // noop
    }
  };
  return (
    <Button variant="ghost" size="sm" onClick={handle} disabled={!text}>
      {copied ? '복사됨 ✓' : label}
    </Button>
  );
}

function Field({ label, value, mono = false, en }) {
  return (
    <div className="grid grid-cols-[110px_1fr] items-start gap-2 py-1 text-sm">
      <div className="text-ink-500">{label}</div>
      <div className={mono ? 'font-mono text-ink-900' : 'text-ink-900'}>
        {value || <span className="text-ink-300">—</span>}
        {en && value && (
          <div className="mt-0.5 text-xs text-ink-500">{en}</div>
        )}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────
function CustomerCard({ customer, order }) {
  const qc = useQueryClient();
  const [translating, setTranslating] = useState(false);
  const [translateError, setTranslateError] = useState(null);

  if (!customer) return null;
  const copyAll = [
    customer.name_ko && `수령인: ${customer.name_ko}${customer.name_en ? ' (' + customer.name_en + ')' : ''}`,
    customer.phone_safe && `안심번호: ${customer.phone_safe}`,
    customer.phone_real && `실번호: ${customer.phone_real}`,
    customer.customs_code && `통관부호: ${customer.customs_code}`,
    customer.address_ko && `주소: ${customer.address_ko}`,
    customer.address_en && `(EN) ${customer.address_en}`,
    customer.shipping_message && `배송메모: ${customer.shipping_message}`,
  ].filter(Boolean).join('\n');

  const retranslate = async () => {
    if (!order?.id) return;
    setTranslateError(null);
    setTranslating(true);
    try {
      await pa.translateOrder(order.id);
      qc.invalidateQueries({ queryKey: ['pa', 'order', String(order.id), 'amazon-prep'] });
    } catch (e) {
      setTranslateError(e.message || '변환 실패');
    } finally {
      setTranslating(false);
    }
  };

  return (
    <Card title="고객 정보">
      <Field label="수령인 (KO)" value={customer.name_ko} en={customer.name_en || undefined} />
      <Field label="안심번호" value={customer.phone_safe} mono />
      <Field label="실번호" value={customer.phone_real} mono />
      <Field label="통관부호" value={customer.customs_code} mono />
      <Field label="주소 (KO)" value={customer.address_ko} en={customer.address_en || undefined} />
      <Field label="배송 메모" value={customer.shipping_message} />
      <Field label="번역 상태" value={translationLabel(customer.translation_status)} />
      <div className="mt-3 flex flex-wrap gap-2">
        <CopyButton text={copyAll} label="고객 정보 전체 복사" />
        <Button
          size="sm"
          variant="secondary"
          onClick={retranslate}
          disabled={translating}
        >
          {translating ? '변환 중...' : '영문 재변환'}
        </Button>
      </div>
      {translateError && <div className="mt-2 text-xs text-red-600">{translateError}</div>}
    </Card>
  );
}

function translationLabel(s) {
  if (s === 'done') return '완료 ✓';
  if (s === 'error') return '실패 ✗ — "영문 재변환" 버튼 사용';
  return '대기 중';
}

// ──────────────────────────────────────────
function AmazonProductCard({ product, amazonUrl, matchStatus, order, option }) {
  if (matchStatus === 'missing_product') {
    return (
      <Card title="아마존 상품">
        <div className="text-sm text-red-600">
          ⚠ 쿠팡 sellerProductId가 listings_pa에 매핑되지 않음 —
          product_id가 NULL입니다. Phase C에서 수동 매칭 UI 추가 예정.
        </div>
      </Card>
    );
  }
  if (!product) {
    return (
      <Card title="아마존 상품">
        <div className="text-sm text-ink-500">상품 정보 없음</div>
      </Card>
    );
  }

  const asin = product.asin;
  const priceUsd = product.cost_usd;
  const qty = order?.quantity || 1;
  const totalUsd = priceUsd ? (priceUsd * qty).toFixed(2) : null;
  const copyAll = [
    asin && `ASIN: ${asin}`,
    product.title_en && `Name: ${product.title_en}`,
    priceUsd && `Unit Price: $${priceUsd}`,
    qty && `Qty: ${qty}`,
    totalUsd && `Expected Total: $${totalUsd}`,
    amazonUrl && `URL: ${amazonUrl}`,
  ].filter(Boolean).join('\n');

  return (
    <Card title="아마존 상품">
      {option?.is_variation && (
        <div className="mb-2 rounded-md bg-pa-50 border border-pa-200 px-3 py-2 text-xs text-pa-700">
          🎁 옵션 상품 — 선택된 옵션: <strong>{option.option_label || '—'}</strong>
          {option.group_master_asin && (
            <span className="ml-2 text-ink-500">
              (그룹: <code className="font-mono">{option.group_master_asin}</code>)
            </span>
          )}
        </div>
      )}
      <Field label="ASIN" value={asin} mono />
      <Field label="영문명" value={product.title_en} />
      <Field label="한글명" value={product.title_ko} />
      <Field label="브랜드" value={product.brand} />
      <Field label="단가 (USD)" value={priceUsd ? `$${priceUsd}` : ''} mono />
      <Field label="수량" value={qty} />
      <Field label="예상 합계" value={totalUsd ? `$${totalUsd}` : ''} mono />
      <Field label="무게 (g)" value={product.weight_g} mono />
      <div className="mt-3 flex flex-wrap gap-2">
        {amazonUrl && (
          <a
            href={amazonUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex h-8 items-center rounded-md bg-brand-pa-500 px-3 text-xs font-medium text-white hover:bg-brand-pa-600"
          >
            아마존에서 열기 ↗
          </a>
        )}
        <CopyButton text={asin} label="ASIN 복사" />
        <CopyButton text={copyAll} label="상품 정보 복사" />
      </div>
      {matchStatus === 'missing_asin' && (
        <div className="mt-3 text-xs text-yellow-700">⚠ ASIN 누락 — products 레코드에 asin 채워주세요.</div>
      )}
    </Card>
  );
}

// ──────────────────────────────────────────
function ShipToForwarderCard({ customer, release, releaseError, onRefresh }) {
  // 백엔드에서 oversea/국내 분기해 full_line을 조립해 내려주므로 그대로 사용.
  const shipTo = release?.full_line || '';

  const forwarderHandoff = [
    customer?.name_ko && `수령인: ${customer.name_ko}${customer.name_en ? ' (' + customer.name_en + ')' : ''}`,
    customer?.customs_code && `통관부호: ${customer.customs_code}`,
    customer?.phone_real && `실휴대폰: ${customer.phone_real}`,
    customer?.address_ko && `한국 주소: ${customer.address_ko}`,
    customer?.address_en && `(EN) ${customer.address_en}`,
  ].filter(Boolean).join('\n');

  return (
    <Card title="① 배송 옵션: 배대지 경유 (Forwarder)">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div>
          <div className="text-xs font-medium text-ink-500 mb-2">Ship to (아마존 결제 화면 입력)</div>
          {releaseError ? (
            <div className="text-sm text-red-600">
              ⚠ 배대지 주소 조회 실패: {releaseError}
              <div className="mt-2">
                <Button size="sm" variant="secondary" onClick={onRefresh}>재시도</Button>
              </div>
            </div>
          ) : !release ? (
            <div className="text-sm text-ink-400">네이버 RELEASE 주소록 불러오는 중...</div>
          ) : (
            <>
              <pre className="whitespace-pre-wrap rounded-md bg-ink-50 p-3 text-xs font-mono text-ink-900">{shipTo}</pre>
              <div className="mt-2 flex gap-2">
                <CopyButton text={shipTo} label="배대지 주소 복사" />
                <Button size="sm" variant="ghost" onClick={onRefresh}>주소록 새로고침</Button>
              </div>
              {!release.oversea && (
                <div className="mt-2 text-xs text-yellow-700">
                  ⚠ 네이버 RELEASE 엔트리가 해외주소 플래그가 아닙니다 — 배대지 맞는지 확인
                </div>
              )}
            </>
          )}
        </div>

        <div>
          <div className="text-xs font-medium text-ink-500 mb-2">
            포워더 전달용 (배대지 도착 후 포워더에게 전달)
          </div>
          <pre className="whitespace-pre-wrap rounded-md bg-ink-50 p-3 text-xs text-ink-900">{forwarderHandoff || '—'}</pre>
          <div className="mt-2">
            <CopyButton text={forwarderHandoff} label="포워더 정보 복사" />
          </div>
        </div>
      </div>
    </Card>
  );
}

// ──────────────────────────────────────────
function ShipToDirectCard({ customer }) {
  const addrEn = customer?.address_en
    ? [
        customer.name_en || customer.name_ko,
        customer.address_en,
        'Republic of Korea',
        customer.phone_real ? `Phone: +82-${normalizeKrPhone(customer.phone_real)}` : null,
      ].filter(Boolean).join('\n')
    : '';

  return (
    <Card title="② 배송 옵션: 아마존 직배송 (Direct to KR)">
      {customer?.translation_status !== 'done' ? (
        <div className="text-sm text-ink-500">
          ⏳ 영문 주소 변환 {customer?.translation_status === 'error' ? '실패 — 위 "영문 재변환" 버튼을 눌러주세요.' : '진행 중...'}
        </div>
      ) : (
        <>
          <pre className="whitespace-pre-wrap rounded-md bg-ink-50 p-3 text-xs font-mono text-ink-900">{addrEn}</pre>
          <div className="mt-2 flex gap-2">
            <CopyButton text={addrEn} label="직배송 주소 복사" />
            {customer.customs_code && (
              <CopyButton text={customer.customs_code} label="통관부호 복사" />
            )}
          </div>
        </>
      )}
      <div className="mt-3 text-xs text-yellow-700">
        ⚠ Amazon 상품 페이지에서 <strong>International Shipping to South Korea</strong> 지원 여부 먼저 확인.
      </div>
    </Card>
  );
}

function normalizeKrPhone(phone) {
  // 01091075191 → 10-9107-5191
  const digits = String(phone).replace(/\D/g, '');
  if (digits.length === 11 && digits.startsWith('0')) {
    return digits.slice(1, 3) + '-' + digits.slice(3, 7) + '-' + digits.slice(7);
  }
  return digits;
}

// ──────────────────────────────────────────
function AmazonOrderForm({
  order, shippingMethod, setShippingMethod,
  amazonOrderId, setAmazonOrderId,
  onSubmit, pending, alreadyDone, error,
}) {
  return (
    <Card title="아마존 발주 완료 처리">
      {alreadyDone ? (
        <div className="text-sm text-ink-600">
          이미 아마존 주문번호 <strong>{order.amazon_order_id}</strong>로 등록됨
          (배송방식: {order.shipping_method || '—'}).
        </div>
      ) : (
        <div className="space-y-4">
          <div>
            <div className="text-xs font-medium text-ink-500 mb-1">배송 방식</div>
            <label className="mr-4 text-sm">
              <input
                type="radio"
                value="forwarder"
                checked={shippingMethod === 'forwarder'}
                onChange={(e) => setShippingMethod(e.target.value)}
                className="mr-1.5"
              />
              배대지 경유
            </label>
            <label className="text-sm">
              <input
                type="radio"
                value="direct"
                checked={shippingMethod === 'direct'}
                onChange={(e) => setShippingMethod(e.target.value)}
                className="mr-1.5"
              />
              직배송 (to KR)
            </label>
          </div>

          <div>
            <div className="text-xs font-medium text-ink-500 mb-1">
              아마존 주문번호 (amazon.com 결제 완료 후)
            </div>
            <input
              type="text"
              value={amazonOrderId}
              onChange={(e) => setAmazonOrderId(e.target.value)}
              placeholder="111-1234567-1234567"
              className="w-full max-w-sm rounded-md border border-ink-200 px-3 py-2 text-sm font-mono"
            />
          </div>

          {error && <div className="text-sm text-red-600">{error}</div>}

          <Button
            variant="pa"
            size="md"
            onClick={onSubmit}
            disabled={pending || !amazonOrderId.trim()}
          >
            {pending ? '저장 중...' : '발주 완료 처리 (→ amazon_purchase 단계)'}
          </Button>
        </div>
      )}
    </Card>
  );
}
