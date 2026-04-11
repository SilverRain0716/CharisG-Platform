/**
 * 인증 실패 시 Shell 로그인으로 리다이렉트.
 * 단일 도메인이므로 /login 으로 이동만 하면 된다.
 */
export function getShellOrigin() {
  return window.location.origin;
}

export function redirectToLogin() {
  const next = window.location.pathname + window.location.search;
  const url = `${getShellOrigin()}/login?next=${encodeURIComponent(next)}`;
  window.location.href = url;
}
