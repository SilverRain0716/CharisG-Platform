import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, Button, EmptyState, ChannelMark, StatusBadge } from '@charisg/ui';

import { useChannel } from '../channel.jsx';
import CoupangPage from './CoupangPage.jsx';
import SmartStorePage from './SmartStorePage.jsx';
import ChannelListingsPage from './ChannelListingsPage.jsx';

/**
 * 등록 상품 — 채널·계정에 따라 실제 화면을 고르는 배차대.
 *
 * 채널별 화면은 이미 각각 만들어져 있다(쿠팡 구/신, 스마트스토어). 상단에서
 * 고른 컨텍스트에 맞는 것을 여기서 연결한다. 사이드바 메뉴를 채널×계정만큼
 * 늘리지 않고 하나로 유지하기 위한 층이다.
 *
 * 아직 화면이 없는 채널(11번가)은 빈 화면 대신 지금 상태와 다음 할 일을 말한다.
 */
export default function ChannelProductsPage() {
  const navigate = useNavigate();
  const { channel, account, channelMeta, accountMeta, withScope } = useChannel();

  if (channel === 'all') {
    return (
      <Card>
        <EmptyState
          title="채널을 먼저 고르세요"
          description="등록 상품은 채널·계정 단위로 봅니다. 상단 탭에서 채널을 선택하면 그 계정의 리스팅만 표시됩니다."
          action={<Button variant="primary" onClick={() => navigate(withScope('/', { channel: 'coupang' }))}>쿠팡으로</Button>}
        />
      </Card>
    );
  }

  if (channel === 'coupang') {
    // 두 계정 모두 같은 목록 화면을 쓴다. 계정은 CoupangPage 가 컨텍스트에서 직접 읽는다.
    // (신계정 '업로드' 작업 화면은 목록이 아니라 별도 메뉴 — /channel-upload)
    return <CoupangPage />;
  }

  if (channel === 'smartstore') {
    // 계정은 SmartStorePage 가 컨텍스트에서 직접 읽는다 — prop 으로 넘기지 않는다.
    return <SmartStorePage />;
  }

  // ★11번가·옥션은 공용 목록 화면을 쓴다(2026-08-15).
  //   채널마다 화면을 따로 만들면 네 벌이 되고, 오늘 겪은 어긋남도 네 배가 된다.
  //   목록이 보여줄 것(상품명·상태·가격·옵션)은 채널이 달라도 같다.
  if (accountMeta?.usable) {
    return <ChannelListingsPage />;
  }

  // 연동 자체가 아직인 채널(G마켓)
  return (
    <Card>
      <EmptyState
        icon={<ChannelMark channel={channel} mark={channelMeta.mark} size="md" muted={!accountMeta?.usable} />}
        title={`${channelMeta.label} 등록 화면은 아직 없습니다`}
        description={
          accountMeta?.usable
            ? `연동은 준비돼 있습니다(${accountMeta.status}). 등록 슬롯 ${accountMeta.limit_products?.toLocaleString() || '—'}개, 일 한도 ${accountMeta.limit_daily?.toLocaleString() || '—'}건. 화면이 붙기 전까지는 스크립트로 등록합니다.`
            : '이 계정은 아직 연동되지 않았습니다.'
        }
        action={
          <div className="flex items-center gap-2">
            {accountMeta?.status && <StatusBadge variant={accountMeta.usable ? 'info' : 'mute'}>{accountMeta.status}</StatusBadge>}
            <Button onClick={() => navigate(withScope('/settings'))}>연동 설정</Button>
          </div>
        }
      />
    </Card>
  );
}
