# Charis Obsidian — Design System

> **용도**: 이 파일은 Claude Code가 항상 참조해서 모든 신 페이지/컴포넌트가 동일 디자인 컨셉을 유지하도록 함.
> **원본**: Google Stitch에서 생성된 "Charis Obsidian" 테마 기반.
> **적용 범위**: DS App 전체 + 향후 PA App, Hub App 통일

---

## 컬러 팔레트

### 배경 (Surfaces)
```
--bg-primary:      #0F0F14      → 메인 배경 (거의 블랙)
--bg-card:         #1A1A24      → 카드/패널 배경
--bg-card-hover:   #22222E      → 카드 호버
--bg-elevated:     #252530      → 모달, 드롭다운
--bg-terminal:     #0A0A10      → 터미널/로그 영역
--bg-sidebar:      #12121A      → 사이드바
```

### 보더 (Borders)
```
--border-subtle:   #2A2A35      → 일반 구분선
--border-hover:    #3A3A48      → 호버 시
--border-active:   #1D9E75      → 선택/활성 상태 (teal)
```

### 텍스트 (Text)
```
--text-primary:    #FFFFFF      → 주요 텍스트, 숫자
--text-secondary:  #8B8B9E      → 부제목, 라벨
--text-muted:      #5A5A6E      → 비활성, 힌트
--text-inverse:    #0F0F14      → 밝은 배경 위 텍스트
```

### 액센트 (Accents)
```
--accent-teal:     #1D9E75      → 프라이머리. CTA, 활성 메뉴, GO 상태, 성공
--accent-blue:     #3B82F6      → 차트, 정보, 링크
--accent-orange:   #F59E0B      → 경고, 대기, 트렌드
--accent-coral:    #EF4444      → 위험, 에러, SKIP/REJECT
--accent-green:    #27AE60      → 성공 배지 (teal과 구분 시)
--accent-purple:   #8B5CF6      → PA 앱 전용 (향후)
```

### 차트 컬러 (순서대로)
```
chart-1: #1D9E75 (teal)
chart-2: #3B82F6 (blue)
chart-3: #F59E0B (orange)
chart-4: #EF4444 (coral)
chart-5: #8B5CF6 (purple)
chart-6: #06B6D4 (cyan)
```

---

## 타이포그래피

```
--font-body:       'Inter', 'Noto Sans KR', sans-serif
--font-mono:       'JetBrains Mono', 'DM Mono', monospace

--text-hero:       2.5rem / 700    → 대형 숫자 (KPI 메인값)
--text-h1:         1.5rem / 600    → 페이지 제목
--text-h2:         1.125rem / 600  → 섹션 제목
--text-body:       0.875rem / 400  → 본문
--text-caption:    0.75rem / 400   → 캡션, 타임스탬프
--text-badge:      0.625rem / 600  → 배지 내부
```

---

## 컴포넌트 스타일

### 카드 (Card)
```css
background: var(--bg-card);
border: 1px solid var(--border-subtle);
border-radius: 12px;
padding: 1.25rem;
/* 그림자 없음. 보더로 구분 */
/* 호버 시 border-color → var(--border-hover) */
```

### KPI 카드
```
- 상단: 라벨 (--text-secondary, 0.75rem)
- 중앙: 숫자 (--text-primary, 2rem, bold)
- 하단: 트렌드 (↑ green / ↓ coral, 0.75rem)
- 좌상단: 컬러 아이콘 (액센트 컬러 원형 배경)
- 우상단: 화살표 링크 아이콘 (→)
```

### 테이블 (DataTable)
```css
/* 행 */
background: transparent;           /* 기본 */
background: #15151E;               /* 짝수 행 (alternating) */
border-bottom: 1px solid #1E1E28; /* 행 구분선 */

/* 호버 */
background: #1E1E2A;

/* 헤더 */
color: var(--text-secondary);
font-size: 0.75rem;
text-transform: uppercase;
letter-spacing: 0.05em;

/* 상태 배지 */
padding: 2px 8px;
border-radius: 4px;
font-size: 0.625rem;
font-weight: 600;
```

### 배지 (StatusBadge)
```
GO:          bg #1D9E7520, text #1D9E75, border #1D9E7540
GO_ORGANIC:  bg #27AE6020, text #27AE60
SKIP:        bg #EF444420, text #EF4444
candidate:   bg #8B8B9E20, text #8B8B9E
listed:      bg #3B82F620, text #3B82F6
active:      bg #1D9E7520, text #1D9E75
paused:      bg #F59E0B20, text #F59E0B
```

### 사이드바 (Sidebar)
```
width: 60px (접힘 상태) / 220px (펼침 상태)
background: var(--bg-sidebar)
border-right: 1px solid var(--border-subtle)

/* 메뉴 아이템 */
padding: 12px;
border-radius: 8px;
color: var(--text-muted);

/* 활성 메뉴 */
background: #1D9E7515;
color: var(--accent-teal);
border-left: 3px solid var(--accent-teal); /* 또는 배경 하이라이트 */
```

### 탑 바 (GlobalTopBar)
```
background: var(--bg-primary);
border-bottom: 1px solid var(--border-subtle);
height: 56px;

/* 탭 */
color: var(--text-muted);          /* 비활성 */
color: var(--text-primary);        /* 활성 */
border-bottom: 2px solid var(--accent-teal); /* 활성 탭 */
```

### 차트 (Recharts)
```
/* 배경 투명 */
/* 그리드 라인: #1E1E28 */
/* 축 텍스트: var(--text-muted) */
/* 데이터 영역: 반투명 fill (opacity 0.15~0.3) + solid stroke */
/* 툴팁: bg var(--bg-elevated), border var(--border-subtle) */
```

### 버튼
```
/* Primary (CTA) */
background: var(--accent-teal);
color: white;
border: none;
border-radius: 8px;
padding: 8px 16px;
font-weight: 600;

/* Ghost */
background: transparent;
color: var(--text-secondary);
border: 1px solid var(--border-subtle);

/* Danger */
background: var(--accent-coral);
color: white;
```

### 터미널 (크롤러 로그)
```css
background: var(--bg-terminal);
border: 1px solid var(--border-subtle);
border-radius: 8px;
font-family: var(--font-mono);
font-size: 0.75rem;
color: #1D9E75;                    /* teal 텍스트 */
padding: 1rem;
overflow-y: auto;
max-height: 400px;

/* 프로그레스 바 */
background: #1A1A24;
fill: var(--accent-teal);
height: 4px;
border-radius: 2px;
```

---

## 레이아웃 규칙

```
페이지 최대 너비: 제한 없음 (전너비)
사이드바: 60px (접힘) / 220px (펼침)
탑 바: 56px 고정
콘텐츠 패딩: 24px
카드 간 간격: 16px (gap)
섹션 간 간격: 24px

그리드: CSS Grid 또는 Flexbox
- KPI 카드: 4컬럼 (1fr 1fr 1fr 1fr)
- 2분할: 2fr 1fr 또는 3fr 1fr
- 모바일: 1컬럼 스택
```

---

## Tailwind 매핑

```javascript
// tailwind.config.js extend
colors: {
  surface: {
    primary: '#0F0F14',
    card: '#1A1A24',
    'card-hover': '#22222E',
    elevated: '#252530',
    terminal: '#0A0A10',
    sidebar: '#12121A',
  },
  border: {
    subtle: '#2A2A35',
    hover: '#3A3A48',
  },
  text: {
    primary: '#FFFFFF',
    secondary: '#8B8B9E',
    muted: '#5A5A6E',
  },
  accent: {
    teal: '#1D9E75',
    blue: '#3B82F6',
    orange: '#F59E0B',
    coral: '#EF4444',
    green: '#27AE60',
    purple: '#8B5CF6',
  },
}
```

---

## 사용법

이 파일은 프로젝트에 배치:
```
CharisG-Platform/docs/design_system_obsidian.md
```

Claude Code에 신 화면 요청 시:
```
docs/design_system_obsidian.md의 Charis Obsidian 디자인 시스템을 적용해줘.
```

향후 PA App, Hub App 리디자인 시에도 동일 시스템 적용으로 플랫폼 전체 일관성 유지.
