from copy import deepcopy
from typing import Any


def to_openai_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Adapta un JSON Schema de Pydantic al subconjunto estricto esperado por
    Structured Outputs de OpenAI:
    - additionalProperties: false en todos los objetos
    - required con todas las propiedades definidas en cada objeto
    """
    schema = deepcopy(schema)

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            # Procesar recursivamente primero
            for key, value in list(node.items()):
                node[key] = _walk(value)

            # Si es un objeto JSON Schema, endurecerlo
            if node.get("type") == "object":
                props = node.get("properties", {})
                node["additionalProperties"] = False
                node["required"] = list(props.keys())

            return node

        if isinstance(node, list):
            return [_walk(item) for item in node]

        return node

    return _walk(schema)