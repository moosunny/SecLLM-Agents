"""langchain create_agent 기반 fix-suggestion 파이프라인 (실험 하네스).

설계: docs/deepagents_pipeline_design.md — 완전형 안(§9).
2026-08-19: deepagents(create_deep_agent)에서 langchain.agents.create_agent로
전환. 같은 날 Option A 개정 — 소스는 harness.make_read_code_tool의
read_code(offset, limit)로 페이징하고 meta는 초기 태스크 메시지에 JSON 인라인.
메인 프로젝트와 격리된 uv 프로젝트로, 접점은 repo_paths 경로 import뿐이다.
"""
