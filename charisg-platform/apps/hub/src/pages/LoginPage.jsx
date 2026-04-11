import React, { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useAuth } from '@charisg/auth';
import { Button, Input, Card } from '@charisg/ui';

export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = params.get('next') || '/';

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
    <main className="flex min-h-screen items-center justify-center bg-gradient-to-br from-ink-50 to-ink-100 px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 inline-block h-12 w-12 rounded-xl bg-gradient-to-br from-brand-ds-500 to-brand-pa-500" />
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">Charis G Platform</h1>
          <p className="mt-1 text-sm text-ink-500">Dropshipping · Purchase Agent</p>
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
              <div className="rounded-md bg-red-50 px-3 py-2 text-xs text-signal-err ring-1 ring-red-100">
                {error}
              </div>
            )}
            <Button type="submit" className="w-full" disabled={busy}>
              {busy ? '로그인 중...' : '로그인'}
            </Button>
          </form>
        </Card>
      </div>
    </main>
  );
}
