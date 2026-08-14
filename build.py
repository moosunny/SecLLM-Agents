"""
Build script: optional PyArmor obfuscation + PyInstaller packaging.

Usage:
  uv run build.py                     # agent, onedir, plain
  uv run build.py --obfuscate         # agent, onedir + graph obfuscation
  uv run build.py --release           # agent, langfuse 트레이싱 제외
  uv run build.py --output out        # 최종 바이너리를 out/ 에 저장

Note:
  --obfuscate obfuscates only the `agents/graph` subpackage
  (registry/builder/helper/middleware/state). The rest stays plain text.
  This keeps the obfuscated code volume within the PyArmor trial license
  limit; obfuscating the whole `agents` package exceeds it.
"""
import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
DIST_PYARMOR = DIST / "pyarmor"
SEP = ";" if platform.system() == "Windows" else ":"
EXT = ".exe" if platform.system() == "Windows" else ""


def run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"[ERROR] 명령 실패: {' '.join(cmd)}")
        sys.exit(result.returncode)


def clean(output_dir: Path) -> None:
    print("=== 이전 빌드 정리 ===")
    for path in [ROOT / "build", DIST_PYARMOR]:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            print(f"  삭제: {path}")
    for name in [f"active_suggestion{EXT}", "active_suggestion"]:
        path = output_dir / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            print(f"  삭제: {path}")
        elif path.exists():
            path.unlink()
            print(f"  삭제: {path}")


def pyarmor_agent_graph() -> None:
    """Obfuscate only the agents/graph subpackage (trial-license friendly)."""
    print("\n=== PyArmor 난독화 (agents/graph만) ===")
    cmd = [
        "pyarmor", "gen",
        "--recursive",
        "--output", str(DIST_PYARMOR),
        "src/agents/graph",
    ]
    run(cmd)


def _langfuse_pyinstaller_args(*, release: bool) -> list[str]:
    """릴리스 빌드는 langfuse를 번들에서 제외; 그 외에는 hidden-import로 포함."""
    if release:
        return ["--exclude-module", "langfuse", "--exclude-module", "langfuse.langchain"]
    return ["--hidden-import", "langfuse", "--hidden-import", "langfuse.langchain"]


def _stage_agent_package(src_root: Path, *, obfuscate_graph: bool) -> tuple[Path, Path]:
    agents_src = src_root / "agents"
    if not agents_src.exists():
        raise FileNotFoundError(f"Agent source not found: {agents_src}")

    stage_root = ROOT / "build" / "agents_stage"
    package_dir = stage_root / "agents"
    if stage_root.exists():
        shutil.rmtree(stage_root, ignore_errors=True)
    shutil.copytree(agents_src, package_dir)

    if obfuscate_graph:
        obf_graph = DIST_PYARMOR / "graph"
        if not obf_graph.exists():
            raise FileNotFoundError(f"Obfuscated graph not found: {obf_graph}")
        target_graph = package_dir / "graph"
        shutil.rmtree(target_graph, ignore_errors=True)
        shutil.copytree(obf_graph, target_graph)
        print(f"  graph 난독화본 적용: {target_graph}")

    entry = package_dir / "__main__.py"
    entry.write_text(
        '"""agents package entry point for PyInstaller."""\n'
        "from agents.app import main\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    return stage_root, entry


def pyinstaller_agent(*, obfuscate: bool, release: bool, output_dir: Path) -> None:
    print("\n=== PyInstaller 패키징 (agent) ===")
    if obfuscate:
        src_root = ROOT / "src"
        stage_root, entry = _stage_agent_package(src_root, obfuscate_graph=obfuscate)
        prompts_path = stage_root / "agents" / "prompts"
    else:
        src_root = ROOT / "src"
        entry = src_root / "agents" / "__main__.py"
        stage_root = src_root
        prompts_path = src_root / "agents" / "prompts"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--paths", str(stage_root),
        "--add-data", f"{prompts_path}{SEP}agents/prompts",
        "--collect-all", "langgraph",
        "--collect-all", "langchain",
        "--collect-all", "langchain_core",
        "--collect-all", "pyrsistent",
        "--collect-all", "tree_sitter",
        "--collect-submodules", "anyio",
        "--hidden-import", "agents",
        "--hidden-import", "agents.app",
        "--hidden-import", "agents.config",
        "--hidden-import", "agents.error_handling",
        "--hidden-import", "agents.graph",
        "--hidden-import", "agents.graph.ast_slice",
        "--hidden-import", "agents.graph.builder",
        "--hidden-import", "agents.graph.chunking",
        "--hidden-import", "agents.graph.engine_trace",
        "--hidden-import", "agents.graph.explore",
        "--hidden-import", "agents.graph.fix",
        "--hidden-import", "agents.graph.fix_loop",
        "--hidden-import", "agents.graph.forced_tool",
        "--hidden-import", "agents.graph.guards",
        "--hidden-import", "agents.graph.middleware",
        "--hidden-import", "agents.graph.registry",
        "--hidden-import", "agents.graph.select",
        "--hidden-import", "agents.graph.state",
        "--hidden-import", "agents.graph.trace",
        "--hidden-import", "agents.prompt_loader",
        "--hidden-import", "agents.schema",
        "--hidden-import", "agents.tracing",
        "--hidden-import", "astchunk",
        "--hidden-import", "astchunk.astchunk_builder",
        "--hidden-import", "langchain",
        "--hidden-import", "langchain.agents",
        "--hidden-import", "langchain.agents.middleware",
        "--hidden-import", "langchain.agents.middleware.tool_call_limit",
        "--hidden-import", "langchain.agents.middleware.tool_retry",
        "--hidden-import", "langchain.agents.middleware.types",
        "--hidden-import", "langchain.chat_models",
        "--hidden-import", "langchain.tools",
        "--hidden-import", "langchain_core",
        "--hidden-import", "langchain_core.tools",
        "--hidden-import", "langchain_openai",
        "--hidden-import", "langgraph",
        "--hidden-import", "langsmith",
        "--hidden-import", "anyio._backends._asyncio",
        "--hidden-import", "anyio._backends._trio",
        "--hidden-import", "pyrsistent",
        "--hidden-import", "tree_sitter_c",
        "--hidden-import", "tree_sitter_cpp",
        "--hidden-import", "tree_sitter_c_sharp",
        "--hidden-import", "tree_sitter_go",
        "--hidden-import", "tree_sitter_java",
        "--hidden-import", "tree_sitter_javascript",
        "--hidden-import", "tree_sitter_python",
        "--hidden-import", "tree_sitter_typescript",
        "--name", "active_suggestion",
    ]
    cmd += _langfuse_pyinstaller_args(release=release)
    cmd += ["--distpath", str(output_dir)]
    if obfuscate:
        cmd += [
            "--add-data", f"dist/pyarmor/pyarmor_runtime_000000{SEP}pyarmor_runtime_000000",
            "--add-data", f"dist/pyarmor/graph{SEP}agents/graph",
        ]
    cmd.append(str(entry))
    run(cmd)


def main() -> None:
    parser = argparse.ArgumentParser(description="active-suggestion 빌드 스크립트")
    parser.add_argument("--obfuscate", action="store_true",
                        help="PyArmor 난독화 적용 (agents/graph 서브패키지만)")
    parser.add_argument("--release", action="store_true",
                        help="릴리스 빌드: langfuse 트레이싱을 번들에서 제외 (default: 포함)")
    parser.add_argument("--output", default="dist",
                        help="최종 바이너리 출력 경로 (default: dist)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir

    clean(output_dir)

    if args.obfuscate:
        pyarmor_agent_graph()

    pyinstaller_agent(obfuscate=args.obfuscate, release=args.release, output_dir=output_dir)

    output_name = "active_suggestion"
    output = output_dir / output_name / f"{output_name}{EXT}"
    print("\n=== 빌드 완료 ===")
    print(f"  난독화  : {'ON (graph 서브패키지만)' if args.obfuscate else 'OFF'}")
    print(f"  릴리스  : {'ON (langfuse 제외)' if args.release else 'OFF'}")
    print(f"  출력    : {output}")


if __name__ == "__main__":
    main()
