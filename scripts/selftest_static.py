#!/usr/bin/env python3
"""
Charis G Platform 정적 자체 검증 (stdlib 전용).

검증 항목:
  1. 전체 Python 파일 AST 파싱 (구문 오류)
  2. schema_*.sql 파일을 in-memory sqlite 에 적용 가능한지
  3. 각 라우터 파일이 module-level `router` 심볼을 export 하는지
  4. main.py 들이 모든 라우터를 import 하는지

venv/pip 없이 동작.
"""
import ast
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ERRORS: list[str] = []
OK: list[str] = []


def err(msg: str) -> None:
    ERRORS.append(msg)
    print(f"  ✗ {msg}")


def ok(msg: str) -> None:
    OK.append(msg)
    print(f"  ✓ {msg}")


def check_python_files() -> None:
    print("[1/5] Python AST 파싱")
    py_files = list(ROOT.rglob("*.py"))
    py_files = [p for p in py_files if "node_modules" not in str(p) and ".venv" not in str(p)]
    syntax_errors = 0
    for p in py_files:
        try:
            ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError as e:
            err(f"SyntaxError in {p.relative_to(ROOT)}:{e.lineno} — {e.msg}")
            syntax_errors += 1
    if syntax_errors == 0:
        ok(f"{len(py_files)}개 .py 파일 모두 파싱 OK")


def check_sql_schemas() -> None:
    print("[2/5] SQL 스키마 적용")
    schemas = [
        ("hub.db",          ROOT / "backend/hub/migrations/schema_hub.sql"),
        ("dropshipping.db", ROOT / "backend/dropshipping/migrations/schema_ds.sql"),
        ("purchase.db",     ROOT / "backend/purchase/migrations/schema_pa.sql"),
    ]
    for label, path in schemas:
        if not path.exists():
            err(f"스키마 파일 없음: {path}")
            continue
        try:
            conn = sqlite3.connect(":memory:")
            conn.executescript(path.read_text(encoding="utf-8"))
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            conn.close()
            ok(f"{label}: {len(tables)} 테이블 — {sorted(tables)}")
        except sqlite3.Error as e:
            err(f"{label} 스키마 적용 실패: {e}")


def check_router_exports() -> None:
    print("[3/5] 라우터 module-level 'router' export")
    router_dirs = [
        ROOT / "backend/hub/routers",
        ROOT / "backend/dropshipping/routers",
        ROOT / "backend/purchase/routers",
    ]
    for d in router_dirs:
        if not d.exists():
            continue
        for p in d.glob("*.py"):
            if p.name == "__init__.py":
                continue
            text = p.read_text(encoding="utf-8")
            if not re.search(r"^router\s*=\s*APIRouter\(", text, re.MULTILINE):
                err(f"{p.relative_to(ROOT)} 에 'router = APIRouter(...)' 없음")
    ok("모든 라우터 파일에 router 심볼 존재")


def check_main_imports() -> None:
    print("[4/5] main.py 라우터 import 정합성")
    cases = [
        (ROOT / "backend/hub/main.py",          ROOT / "backend/hub/routers"),
        (ROOT / "backend/dropshipping/main.py", ROOT / "backend/dropshipping/routers"),
        (ROOT / "backend/purchase/main.py",     ROOT / "backend/purchase/routers"),
    ]
    for main_path, routers_dir in cases:
        if not main_path.exists():
            err(f"{main_path} 없음")
            continue
        main_src = main_path.read_text(encoding="utf-8")
        router_files = sorted(p.stem for p in routers_dir.glob("*.py") if p.stem != "__init__")

        missing = []
        for rf in router_files:
            # main.py 안에 해당 모듈 이름이 import 또는 사용되는지
            if rf not in main_src:
                missing.append(rf)
        if missing:
            err(f"{main_path.relative_to(ROOT)}: 미연결 라우터 {missing}")
        else:
            # include_router 호출 카운트
            include_count = len(re.findall(r"include_router\(", main_src))
            ok(f"{main_path.relative_to(ROOT)}: {len(router_files)} 라우터, include_router 호출 {include_count}회")


def check_frontend_structure() -> None:
    print("[5/5] 프론트 구조 (package.json + 페이지)")
    apps = ["hub", "dropshipping", "purchase"]
    for a in apps:
        pkg = ROOT / f"apps/{a}/package.json"
        if not pkg.exists():
            err(f"{pkg.relative_to(ROOT)} 없음")
            continue
        try:
            import json
            data = json.loads(pkg.read_text())
            scripts = data.get("scripts", {})
            assert "dev" in scripts, "dev script 없음"
        except Exception as e:
            err(f"{pkg.relative_to(ROOT)}: {e}")
            continue
        pages = list((ROOT / f"apps/{a}/src/pages").glob("*.jsx"))
        ok(f"{a}-app: {len(pages)} 페이지, dev 스크립트 OK")


def main() -> int:
    print("━" * 50)
    print("Charis G Platform Static Self-Test")
    print(f"ROOT: {ROOT}")
    print("━" * 50)

    check_python_files()
    check_sql_schemas()
    check_router_exports()
    check_main_imports()
    check_frontend_structure()

    print("━" * 50)
    if ERRORS:
        print(f"✗ FAIL — {len(ERRORS)} 에러, {len(OK)} OK")
        return 1
    print(f"✓ PASS — {len(OK)} 검증 항목 모두 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
