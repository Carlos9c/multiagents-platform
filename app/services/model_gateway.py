from app.schemas.planner import PlannerOutput
from app.services.llm.factory import get_llm_provider


def call_planner_model(project_name: str, project_description: str) -> PlannerOutput:
    provider = get_llm_provider()

    system_prompt = (
        "You are a technical planning agent. "
        "Generate highly detailed, executable software tasks. "
        "Avoid vague tasks. Explain what should be done and how it should be approached. "
        "Return valid JSON only."
    )

    user_prompt = f"""
Project name: {project_name}

Project description:
{project_description}
"""

    raw = provider.generate_json(system_prompt=system_prompt, user_prompt=user_prompt)
    import json

    print("RAW PLANNER RESPONSE:")
    print(json.dumps(raw, ensure_ascii=False, indent=2))
    return PlannerOutput.model_validate(raw)
