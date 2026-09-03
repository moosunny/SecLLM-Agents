"""메인 repo 결정적 verifier로의 경로 import 배선 (설계 §1.3).

`evals/subagent/verify_fix.py`는 자기 위치 기준으로 `<repo>/src`를 sys.path에
넣어 `agents.graph.guards`를 배선하므로, 여기서는 `evals/subagent`만 넣으면
된다. 메인 프로젝트 설치·lock 변경 없음.

주의(결합 명시): `import agents.graph.guards`는 `agents/__init__.py` →
`agents.schema`(pydantic)·`agents.config`까지 실행한다. guards 모듈 자체는
stdlib-only지만 import 경로는 아니므로, 메인 repo의 `agents/` 패키지 루트에
새 서드파티 의존성이 생기면 이 격리 venv에서 깨질 수 있다. 그 시점의 대안은
`importlib.util.spec_from_file_location`으로 guards.py를 파일 단위 로드하는 것.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFY_DIR = REPO_ROOT / "evals" / "subagent"


def ensure_repo_paths() -> None:
    path = str(VERIFY_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


def load_check_candidate():
    """verify_fix.check_candidate를 격리 venv에서 로드한다."""
    ensure_repo_paths()
    from verify_fix import check_candidate

    return check_candidate
