# SecLLM-Agents (active-suggestion)

SAST 결과(sink/source trace)를 입력으로 받아 수정 제안을 생성하는 LangGraph 기반 에이전트.
이 저장소는 에이전트 로직(`src/agents`), 로컬 의존성(`src/astchunk`), 빌드 스크립트만 추적합니다.

## 요구 사항

- Python 3.12 (`.python-version` 참조 — uv가 자동으로 맞춰줍니다)
- [uv](https://docs.astral.sh/uv/) (의존성 및 빌드 실행)
- Linux x86_64 또는 Windows (PyInstaller는 크로스 컴파일이 안 되므로 **타깃 서버와 같은 OS/아키텍처에서 빌드**해야 합니다)

## 설치

```bash
uv sync          # uv.lock 기준으로 재현 가능한 환경 구성 (astchunk는 src/astchunk 로컬 경로에서 설치됨)
```

## 빌드

PyInstaller onedir 패키징. 결과물은 `dist/active_suggestion/active_suggestion` (Windows는 `.exe`).

```bash
uv run build.py                     # 기본 빌드 (langfuse 트레이싱 포함)
uv run build.py --release           # 릴리스 빌드: langfuse를 번들에서 제외
uv run build.py --obfuscate         # agents/graph 서브패키지만 PyArmor 난독화 후 패키징
uv run build.py --output out        # 출력 경로 변경 (기본: dist)
```

참고:

- `--obfuscate`는 PyArmor 트라이얼 라이선스 용량 제한 때문에 `agents/graph`만 난독화합니다.
- `active_suggestion.spec`은 PyInstaller가 생성하는 파일이라 추적하지 않습니다. `build.py`가 CLI 인자로 매번 동일한 구성을 재생성합니다.

## 실행

빌드된 바이너리는 실행 디렉토리의 환경변수(또는 `.env`)를 읽습니다. `.env`는 커밋하지 마세요.

| 변수 | 용도 |
|---|---|
| `LITELLM_MASTER_KEY` | LLM 엔드포인트 인증 키 (필수) |
| `LANGFUSE_TRACING_ENABLED` | 트레이싱 on/off (릴리스 빌드에서는 무시됨) |
| `LANGFUSE_HOST` / `LANGFUSE_BASE_URL` | Langfuse 서버 주소 |

주의: `.env`를 다른 서버로 복사할 때 CRLF 줄바꿈이면 `source`가 깨집니다. `dotenv` 로딩은 무관하지만, 셸에서 직접 source할 경우 `dos2unix`로 변환하세요.

## 새 서버에서 처음부터 빌드하기

```bash
# 1. uv 설치
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 저장소 클론
git clone <repo-url> && cd active-suggestion-nipa

# 3. 의존성 설치 (Python 3.12도 uv가 자동 설치)
uv sync

# 4. 빌드
uv run build.py --release
```
