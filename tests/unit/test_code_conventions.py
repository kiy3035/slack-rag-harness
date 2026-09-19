import ast
from collections.abc import Iterator
from pathlib import Path
import re


SOURCE_ROOTS = (Path("app"), Path("tests"))
KOREAN_PATTERN = re.compile("[가-힣]")


def iter_python_files() -> Iterator[Path]:
    """직접 작성한 애플리케이션과 테스트 Python 파일을 순회한다."""
    for source_root in SOURCE_ROOTS:
        yield from source_root.rglob("*.py")


def iter_functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """중첩 함수를 포함해 모든 함수와 메서드 정의를 찾는다."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_all_functions_have_korean_docstrings_and_type_hints() -> None:
    """직접 작성한 모든 함수가 한글 설명과 타입 경계를 갖는지 검증한다."""
    failures: list[str] = []
    for path in iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in iter_functions(tree):
            docstring = ast.get_docstring(function) or ""
            if KOREAN_PATTERN.search(docstring) is None:
                failures.append(f"{path}:{function.lineno} 한글 Docstring 누락")
            arguments = [
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            ]
            for argument in arguments:
                if argument.arg not in {"self", "cls"} and argument.annotation is None:
                    failures.append(
                        f"{path}:{function.lineno} {argument.arg} 타입 힌트 누락"
                    )
            if function.returns is None:
                failures.append(f"{path}:{function.lineno} 반환 타입 힌트 누락")
        for node in ast.walk(tree):
            if isinstance(node, ast.Lambda):
                failures.append(f"{path}:{node.lineno} 설명할 수 없는 lambda 사용")

    assert failures == []
