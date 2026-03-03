import inspect
import types
import typing
from typing import Any, Callable, Optional, Union, get_type_hints

# Python 타입 → JSON Schema 타입 매핑
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

# 글로벌 도구 레지스트리
_tool_registry: dict[str, dict[str, Any]] = {}


def _resolve_type(raw_type: type) -> tuple[type, bool]:
    """타입 힌트를 언래핑한다.
    Optional[X] (Union[X, None]) → (X, True)
    일반 타입 → (type, False)
    """
    origin = getattr(raw_type, "__origin__", None)

    # typing.Union 처리 (Optional[X] = Union[X, None] 포함)
    if origin is Union:
        args = raw_type.__args__
        non_none = [a for a in args if a is not type(None)]
        has_none = type(None) in args
        if len(non_none) == 1 and has_none:
            return non_none[0], True
        return non_none[0] if non_none else str, False

    # Python 3.10+ union 문법: X | None
    if isinstance(raw_type, types.UnionType):
        args = raw_type.__args__
        non_none = [a for a in args if a is not type(None)]
        has_none = type(None) in args
        if len(non_none) == 1 and has_none:
            return non_none[0], True
        return non_none[0] if non_none else str, False

    return raw_type, False


def _generate_input_schema(func: Callable) -> dict[str, Any]:
    """함수의 타입 힌트와 시그니처로부터 JSON Schema를 자동 생성한다."""
    sig = inspect.signature(func)
    hints = get_type_hints(func)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name == "self":
            continue

        prop: dict[str, Any] = {}

        if name in hints:
            resolved, is_optional = _resolve_type(hints[name])
            prop["type"] = _TYPE_MAP.get(resolved, "string")
        else:
            prop["type"] = "string"

        properties[name] = prop

        # required 판단: 기본값 없고 Optional이 아닌 경우
        if param.default is inspect.Parameter.empty:
            if name in hints:
                _, is_optional = _resolve_type(hints[name])
                if not is_optional:
                    required.append(name)
            else:
                required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    return schema


def tool(
    func: Optional[Callable] = None,
    *,
    description: Optional[str] = None,
    input_schema: Optional[dict[str, Any]] = None,
) -> Callable:
    """함수를 MCP 도구로 등록하는 데코레이터.

    사용법:
        @tool
        def my_func(...): ...

        @tool(description="...", input_schema={...})
        def my_func(...): ...
    """

    def decorator(fn: Callable) -> Callable:
        tool_name = fn.__name__
        tool_desc = description or inspect.getdoc(fn) or f"Tool: {tool_name}"
        tool_schema = input_schema or _generate_input_schema(fn)

        _tool_registry[tool_name] = {
            "name": tool_name,
            "description": tool_desc,
            "inputSchema": tool_schema,
            "func": fn,
        }
        return fn

    # @tool (bare) vs @tool(...) (factory) 지원
    if func is not None:
        return decorator(func)
    return decorator


def list_tools() -> list[dict[str, Any]]:
    """MCP tools/list 응답용 도구 목록을 반환한다."""
    return [
        {
            "name": entry["name"],
            "description": entry["description"],
            "inputSchema": entry["inputSchema"],
        }
        for entry in _tool_registry.values()
    ]


def call_tool(name: str, arguments: dict[str, Any]) -> Any:
    """이름으로 도구를 찾아 실행한다."""
    if name not in _tool_registry:
        raise KeyError(f"Unknown tool: {name}")
    return _tool_registry[name]["func"](**arguments)
