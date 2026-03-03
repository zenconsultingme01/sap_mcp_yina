from tool import tool

# ── 도구 정의 ──
# @tool 데코레이터로 등록하면 tools/list에 자동 노출됩니다.
# 별도 모듈에 정의 후 import해도 됩니다.


@tool
def hello(name: str) -> str:
    """인사 도구 (테스트용)"""
    return f"Hello, {name}!"

@tool
def company_name() :
    """회사 이름"""
    return "젠컨설팅"