import React, { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useAuth } from '@charisg/auth';
import { Button, Input, Card } from '@charisg/ui';

export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // 어디서 튕겨 왔는지 모르면 콘솔로 보낸다 — 루트로 보내면 다시 여기로 돌아온다.
  const next = params.get('next') || '/purchase/';

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await login(username, password);
      window.location.href = next;
    } catch (err) {
      setError(err?.message || '로그인 실패');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 inline-block h-11 w-11 rounded-lg bg-gradient-to-br from-channel-smartstore to-channel-coupang" />
          <h1 className="text-xl font-semibold tracking-tight text-ink-900">Charis G</h1>
          <p className="mt-1 text-[12.5px] text-ink-500">구매대행 운영 콘솔</p>
        </div>

        <Card title="로그인">
          <form onSubmit={onSubmit} className="space-y-4">
            <Input
              label="아이디"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              required
            />
            <Input
              label="비밀번호"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            {error && (
              <div className="rounded-md bg-soft-err px-3 py-2 text-xs text-signal-err ring-1 ring-signal-err/30">
                {error}
              </div>
            )}
            <Button type="submit" variant="primary" className="w-full" disabled={busy}>
              {busy ? '로그인 중...' : '로그인'}
            </Button>
          </form>
        </Card>
      </div>
    </main>
  );
}
