"""AST-based code validator for LLM-generated bot strategy code.

Defense-in-depth layer: validates generated Python code before it's stored.
The primary sandbox is WASM (Pyodide), but this catches obvious bad code
before it ever reaches the executor.
"""

from __future__ import annotations

import ast
import re

import structlog

logger = structlog.get_logger(__name__)

# Functions that are never allowed in generated code
BLOCKED_CALLS = frozenset({
    "exec", "eval", "compile", "__import__", "open",
    "getattr", "setattr", "delattr", "globals", "locals",
    "dir", "vars", "type", "breakpoint", "input", "print",
    "exit", "quit", "help",
})

# Module names that should never appear (imports are blocked anyway,
# but this catches string-based references)
BLOCKED_MODULES = frozenset({
    "os", "sys", "subprocess", "shutil", "pathlib",
    "socket", "http", "urllib", "requests", "httpx",
    "pickle", "shelve", "ctypes", "importlib",
})

# Imports that are harmless because these modules are pre-loaded in the
# sandbox. LLMs habitually add them; stripping avoids wasting retries.
_SAFE_IMPORT_RE = re.compile(
    r"^\s*(?:"
    r"import\s+polars(?:\s+as\s+pl)?"
    r"|import\s+math"
    r"|from\s+math\s+import\s+.+"
    r")\s*$",
    re.MULTILINE,
)


class CodeValidationError(Exception):
    """Raised when generated code fails validation."""


def sanitize_strategy_code(code: str) -> str:
    """Strip harmless import lines that LLMs habitually add.

    Removes ``import polars as pl``, ``import math``, etc. since these
    are already pre-loaded in the sandbox and would otherwise fail
    validation.
    """
    cleaned = _SAFE_IMPORT_RE.sub("", code)
    # Collapse leading blank lines left after stripping
    cleaned = re.sub(r"\A\n+", "", cleaned)
    if cleaned != code:
        logger.debug("sanitized_safe_imports")
    return cleaned


def validate_strategy_code(code: str) -> None:
    """Validate generated strategy code. Raises CodeValidationError on failure.

    Checks:
    1. Code must parse as valid Python
    2. Must contain a top-level `evaluate` function
    3. No import / import-from statements (safe ones are pre-stripped)
    4. No calls to blocked builtins (exec, eval, open, etc.)
    5. No access to dunder attributes (__class__, __globals__, etc.)
    """
    # 1. Parse
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise CodeValidationError(f"Syntax error in generated code: {e}") from e

    # 2. Must have an evaluate function
    top_level_funcs = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    ]
    if "evaluate" not in top_level_funcs:
        raise CodeValidationError(
            "Generated code must contain a top-level `evaluate` function"
        )

    # 3-5. Walk the AST and check for violations
    for node in ast.walk(tree):
        # Block imports (safe ones already stripped by sanitize_strategy_code)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import) and node.names:
                module = node.names[0].name
            raise CodeValidationError(
                f"Import statements are not allowed: '{module}'. "
                f"Libraries (polars, ta) are pre-injected in the sandbox."
            )

        # Block calls to dangerous builtins
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name and name in BLOCKED_CALLS:
                raise CodeValidationError(
                    f"Call to '{name}' is not allowed in strategy code"
                )

        # Block dunder attribute access
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise CodeValidationError(
                    f"Access to dunder attribute '{node.attr}' is not allowed"
                )

        # Block string references to blocked modules
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for mod in BLOCKED_MODULES:
                if node.value == mod:
                    raise CodeValidationError(
                        f"Reference to blocked module '{mod}' is not allowed"
                    )

    logger.debug("code_validation_passed", func_count=len(top_level_funcs))
