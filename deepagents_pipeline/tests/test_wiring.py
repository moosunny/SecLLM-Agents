"""G001 배선 검증: 격리 venv에서 langchain create_agent import + verify_fix 경로 import."""

from deepagents_pipeline.repo_paths import REPO_ROOT, load_check_candidate

JAVA = """\
public class Demo {
    public int add(int a, int b) {
        return a + b;
    }
}
"""


def test_repo_root_points_at_main_repo():
    assert (REPO_ROOT / "evals" / "subagent" / "verify_fix.py").is_file()
    assert (REPO_ROOT / "src" / "agents" / "graph" / "guards.py").is_file()


def test_create_agent_importable():
    from langchain import agents

    assert hasattr(agents, "create_agent")


def test_check_candidate_pass_roundtrip():
    check_candidate = load_check_candidate()
    code_lines = JAVA.splitlines()
    candidate = {
        "desc": "덧셈 결과를 long으로 승격해 오버플로를 방지",
        "codeModifications": [
            {"type": "delete", "startLine": 3, "endLine": 3},
            {"type": "add", "startLine": 3, "content": "        return Math.addExact(a, b);"},
        ],
        "multi_site": False,
    }
    result = check_candidate(code_lines, candidate)
    assert result["errors"] == []
    assert result["new_lines"] is not None


def test_check_candidate_structured_fail():
    check_candidate = load_check_candidate()
    code_lines = JAVA.splitlines()
    candidate = {
        "desc": "no-op",
        "codeModifications": [
            {"type": "delete", "startLine": 999, "endLine": 999},
        ],
        "multi_site": False,
    }
    result = check_candidate(code_lines, candidate)
    assert result["errors"], "존재하지 않는 라인 삭제는 구조화 오류를 반환해야 한다"
