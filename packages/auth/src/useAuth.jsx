import React, { createContext, useContext, useEffect, useState } from 'react';
import { apiFetch } from './apiFetch.js';

const AuthContext = createContext({
  user: null,
  loading: true,
  login: async () => {},
  logout: async () => {},
});

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      if (import.meta.env.VITE_BYPASS_AUTH === '1') {
        if (!cancelled) {
          setUser({ id: 0, username: 'dev', role: 'admin' });
          setLoading(false);
        }
        return;
      }
      try {
        const me = await apiFetch('/api/hub/auth/me');
        if (!cancelled) setUser(me);
      } catch {
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  async function login(username, password) {
    const data = await apiFetch('/api/hub/auth/login', {
      method: 'POST',
      body: { username, password },
    });
    setUser(data.user);
    return data;
  }

  async function logout() {
    let revoked = false;
    try {
      await apiFetch('/api/hub/auth/logout', { method: 'POST' });
      // 검증: cookie 가 실제로 무효화됐는지 확인. 401 떨어져야 정상.
      try {
        await apiFetch('/api/hub/auth/me');
        // 200 이면 logout 실패 (cookie 잔존 + server 가 인증 통과시킴)
        alert('로그아웃 실패. 모바일 Safari 의 캐시 문제일 수 있습니다.\n설정 → Safari → 고급 → 웹사이트 데이터 → wongbigo.com 삭제 후 다시 시도해주세요.');
      } catch {
        revoked = true;
      }
    } catch {
      revoked = true;  // logout 요청 자체가 실패해도 클라는 정리
    }
    setUser(null);
    try { localStorage.clear(); sessionStorage.clear(); } catch {}
    if (revoked) {
      // hard reload (timestamp query) — 새 빌드 강제 + useAuth 재초기화
      window.location.href = '/login?_t=' + Date.now();
    }
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
