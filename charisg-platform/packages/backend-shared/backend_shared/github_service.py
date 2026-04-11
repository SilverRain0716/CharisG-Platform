"""
github_service.py — GitHub API 연동
- Actions 워크플로우 트리거 (workflow_dispatch)
- 워크플로우 실행 상태 조회
"""
import logging
from typing import Optional

import requests

import os
from backend_shared._config import GITHUB_TOKEN, GITHUB_REPO

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}"

# 워크플로우 파일명 → 표시명 매핑
WORKFLOW_MAP = {
    "cj_crawl": "cj_crawl.yml",
    "smartstore_list": "smartstore_list.yml",
    "delete_and_relist": "delete_and_relist.yml",
}


def _headers() -> dict:
    if not GITHUB_TOKEN:
        raise ValueError("GITHUB_TOKEN이 설정되지 않았습니다")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def trigger_workflow(workflow_key: str, ref: str = "main") -> dict:
    """
    GitHub Actions workflow_dispatch 트리거
    workflow_key: 'cj_crawl' | 'smartstore_list' | 'delete_and_relist'
    """
    filename = WORKFLOW_MAP.get(workflow_key)
    if not filename:
        return {"success": False, "message": f"알 수 없는 워크플로우: {workflow_key}"}

    url = f"{BASE_URL}/actions/workflows/{filename}/dispatches"

    try:
        resp = requests.post(url, json={"ref": ref}, headers=_headers(), timeout=15)

        if resp.status_code == 204:
            logger.info("✅ 워크플로우 트리거 성공: %s", filename)
            return {"success": True, "message": f"{filename} 실행 시작됨"}
        else:
            body = resp.text[:200]
            logger.error("워크플로우 트리거 실패 [%d]: %s", resp.status_code, body)
            return {"success": False, "message": f"GitHub API 오류 ({resp.status_code}): {body}"}

    except requests.RequestException as e:
        logger.error("GitHub API 요청 실패: %s", e)
        return {"success": False, "message": f"연결 실패: {str(e)}"}


def get_recent_runs(workflow_key: Optional[str] = None, limit: int = 10) -> list[dict]:
    """최근 워크플로우 실행 이력 조회"""
    if workflow_key:
        filename = WORKFLOW_MAP.get(workflow_key)
        if not filename:
            return []
        url = f"{BASE_URL}/actions/workflows/{filename}/runs?per_page={limit}"
    else:
        url = f"{BASE_URL}/actions/runs?per_page={limit}"

    try:
        resp = requests.get(url, headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()

        runs = []
        for run in data.get("workflow_runs", [])[:limit]:
            runs.append({
                "id": run["id"],
                "name": run["name"],
                "status": run["status"],          # queued | in_progress | completed
                "conclusion": run.get("conclusion"),  # success | failure | cancelled
                "created_at": run["created_at"],
                "updated_at": run["updated_at"],
                "html_url": run["html_url"],
            })
        return runs

    except Exception as e:
        logger.error("GitHub runs 조회 실패: %s", e)
        return []
