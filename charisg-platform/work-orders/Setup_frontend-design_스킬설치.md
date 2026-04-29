# frontend-design 스킬 설치서

> **목적**: Claude Code가 AI 느낌 나지 않는 프로덕션 UI를 작성하도록 Anthropic 공식 frontend-design 스킬을 설치
> **선행 조건**: Claude Code 설치 완료
> **예상 시간**: 2분
> **비용**: 무료 (Anthropic 공식 오픈소스)

---

## 방법 1: 플러그인 설치 (★ 권장, 가장 간단)

Anthropic 공식 플러그인 마켓플레이스에서 설치합니다.

```bash
claude plugin install frontend-design
```

이 한 줄이면 됩니다. 스킬 파일 + Agent Skills가 함께 설치됩니다.

**설치 확인:**
```bash
# Claude Code 실행 후
claude
# 프롬프트에서 입력:
> 너는 어떤 스킬을 가지고 있니?
# → frontend-design이 목록에 표시되면 성공
```

---

## 방법 2: 수동 설치 (플러그인이 안 될 경우)

### Step 1: 스킬 폴더 생성

```bash
# 글로벌 설치 (모든 프로젝트에서 사용)
mkdir -p ~/.claude/skills/frontend-design

# 또는 프로젝트별 설치 (현재 프로젝트에서만 사용)
# mkdir -p .claude/skills/frontend-design
```

### Step 2: SKILL.md 다운로드

GitHub URL: `https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md`

```bash
curl -o ~/.claude/skills/frontend-design/SKILL.md \
  https://raw.githubusercontent.com/anthropics/skills/main/skills/frontend-design/SKILL.md
```

### Step 3: 설치 확인

```bash
# 파일 존재 확인
ls -la ~/.claude/skills/frontend-design/SKILL.md

# 파일 내용 확인 (첫 몇 줄)
head -20 ~/.claude/skills/frontend-design/SKILL.md
# → "name: frontend-design" 이 보여야 함

# Claude Code에서 확인
claude
> 너는 어떤 스킬을 가지고 있니?
# → frontend-design 목록에 표시
```

---

## 사용법

설치 후 Claude Code에서 UI 관련 작업 시 자동으로 활성화됩니다.

**자동 트리거 (스킬이 알아서 적용):**
```
> 대시보드 페이지를 만들어줘
> 로그인 화면을 디자인해줘
> KPI 카드 컴포넌트를 만들어줘
```

**명시적 호출 (더 확실하게):**
```
> frontend-design 스킬을 사용해서 Hub 대시보드를 디자인해줘
> /frontend-design 스타일로 GlobalTopBar 컴포넌트를 만들어줘
```

---

## 스킬이 하는 일

설치 전후 차이:

| 항목 | 설치 전 | 설치 후 |
|------|--------|--------|
| 폰트 | Arial, Inter 등 기본 | 프로젝트에 맞는 감성 있는 폰트 선택 |
| 색상 | 무난한 파란색/회색 | 의도적이고 독특한 컬러 팔레트 |
| 레이아웃 | 표준 SaaS 템플릿 | 대담하고 기억에 남는 구성 |
| 애니메이션 | 없거나 기본 | 목적이 있는 의도적 모션 |
| 전체 인상 | "AI가 만든 거 같다" | "디자이너가 검토한 것 같다" |

---

## Charis G 프로젝트에서의 활용

이 스킬은 Phase 1~3에서 신규 화면을 만들 때 반드시 적용:

- Shell App: 로그인 페이지, Hub 대시보드
- DS App: 8개 뷰 전체 (피드, 마켓플레이스 히트맵, 상세뷰, 칸반 등)
- PA App: 7개 메뉴 전체
- 공유 컴포넌트: GlobalTopBar, Sidebar, DataTable, KPICard 등

---

## 트러블슈팅

**Q: 플러그인 설치 시 "not found" 에러**
```bash
# Claude Code 업데이트 후 재시도
npm update -g @anthropic-ai/claude-code
claude plugin install frontend-design
```

**Q: 스킬이 자동으로 활성화되지 않음**
```bash
# Claude Code 재시작
exit
claude
# 명시적으로 호출
> frontend-design 스킬을 사용해서 시작해줘
```

**Q: 수동 설치 시 curl 다운로드 실패**
```bash
# 브라우저에서 직접 다운로드
# URL: https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md
# "Raw" 버튼 클릭 → 내용 복사 → 파일로 저장
```
