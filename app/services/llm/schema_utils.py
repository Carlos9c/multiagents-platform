from copy import deepcopy
from typing import Any

_OPENAI_UNSUPPORTED_KEYS = {"default"}
_ANTHROPIC_UNSUPPORTED_KEYS = {"default", "$schema", "$defs"}


def to_openai_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Adapts a Pydantic JSON Schema to OpenAI Structured Outputs:
    - additionalProperties: false on all objects
    - required populated with all defined properties
    - strips unsupported keys (default)
    OpenAI supports $ref/$defs natively, so no resolution is needed.
    """
    schema = deepcopy(schema)

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            for key in _OPENAI_UNSUPPORTED_KEYS:
                node.pop(key, None)

            for key, value in list(node.items()):
                node[key] = _walk(value)

            if node.get("type") == "object":
                props = node.get("properties", {})
                node["additionalProperties"] = False
                node["required"] = list(props.keys())

            return node

        if isinstance(node, list):
            return [_walk(item) for item in node]

        return node

    return _walk(schema)


def to_anthropic_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Adapts a Pydantic JSON Schema to Anthropic tool input_schema format:
    - Resolves all $ref/$defs inline (Anthropic tool schemas don't support $ref)
    - additionalProperties: false on all objects
    - required populated with all defined properties
    - strips unsupported keys (default, $schema)
    """
    schema = deepcopy(schema)
    defs = schema.get("$defs", {})

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_path = node["$ref"]  # e.g. "#/$defs/SomeName"
                def_name = ref_path.rsplit("/", 1)[-1]
                return _resolve(deepcopy(defs.get(def_name, {})))

            result: dict[str, Any] = {}
            for key, value in node.items():
                if key in _ANTHROPIC_UNSUPPORTED_KEYS:
                    continue
                result[key] = _resolve(value)

            if result.get("type") == "object":
                props = result.get("properties", {})
                result["additionalProperties"] = False
                if props:
                    result["required"] = list(props.keys())

            return result

        if isinstance(node, list):
            return [_resolve(item) for item in node]

        return node

    return _resolve(schema)
