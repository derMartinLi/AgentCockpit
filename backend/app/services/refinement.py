from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from backend.app.domain.models import RefinementQuestion, TaskSpec

IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
}
TEXT_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".md",
    ".sql",
}
SENSITIVE_FILE_MARKERS = {".env", "secret", "credential", "token", "key.pem"}


class RefinementAssessment(BaseModel):
    spec: TaskSpec
    questions: list[RefinementQuestion] = Field(default_factory=list, max_length=3)
    rationale: str = ""
    engine: str = "agent"


class RepositoryInspector:
    def inspect(self, repository_path: str, raw_request: str) -> dict[str, Any]:
        root = Path(repository_path)
        tokens = {token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", raw_request)}
        files: list[str] = []
        relevant: list[str] = []
        manifests: list[str] = []
        for path in root.rglob("*"):
            if len(files) >= 300:
                break
            if not path.is_file() or any(part in IGNORED_DIRECTORIES for part in path.parts):
                continue
            relative = path.relative_to(root).as_posix()
            files.append(relative)
            if path.name in {
                "pyproject.toml",
                "package.json",
                "Cargo.toml",
                "go.mod",
                "README.md",
            }:
                manifests.append(relative)
            if path.suffix.lower() in TEXT_EXTENSIONS and any(
                token in relative.lower() for token in tokens
            ):
                relevant.append(relative)
        excerpt_candidates = [*manifests, *relevant, *files]
        excerpts: dict[str, str] = {}
        for relative in excerpt_candidates:
            if len(excerpts) >= 8:
                break
            if relative in excerpts or any(
                marker in relative.lower() for marker in SENSITIVE_FILE_MARKERS
            ):
                continue
            path = root / relative
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            try:
                excerpts[relative] = path.read_text(encoding="utf-8")[:2500]
            except (OSError, UnicodeDecodeError):
                continue
        return {
            "repository": str(root),
            "files_scanned": len(files),
            "manifests": manifests[:20],
            "relevant_files": relevant[:30],
            "sample_files": files[:30],
            "file_excerpts": excerpts,
        }


class RefinementService:
    def __init__(self, inspector: RepositoryInspector | None = None) -> None:
        self.inspector = inspector or RepositoryInspector()

    def questions(self, raw_request: str, round_number: int) -> list[RefinementQuestion]:
        if round_number > 2:
            raise ValueError("Refinement is limited to two rounds")
        if round_number == 1:
            return [
                RefinementQuestion(
                    id="behavior",
                    question="用户最终应观察到什么行为变化？",
                    reason="用于形成可验证的验收标准。",
                ),
                RefinementQuestion(
                    id="scope",
                    question="哪些模块必须包含或明确排除在本次范围外？",
                    reason="用于控制任务边界和潜在范围漂移。",
                ),
                RefinementQuestion(
                    id="constraints",
                    question="是否有兼容性、依赖、数据结构或基础设施限制？",
                    reason="用于避免产生不可接受的实现取舍。",
                ),
            ]
        return [
            RefinementQuestion(
                id="edge_cases",
                question="哪些失败场景或边界情况必须被覆盖？",
                reason="用于补足第一轮答案产生的关键验收缺口。",
            ),
            RefinementQuestion(
                id="interface",
                question="是否存在必须保持稳定的 API、命令或数据接口？",
                reason="用于确定最小共享边界。",
            ),
            RefinementQuestion(
                id="validation",
                question="哪些测试、检查或人工步骤必须用于确认本次变更？",
                reason="用于明确可重复执行的验证方式。",
            ),
        ]

    def assess(
        self,
        *,
        raw_request: str,
        inspection: dict[str, Any],
        provider: dict[str, Any],
        api_key: str | None,
    ) -> RefinementAssessment:
        if provider.get("provider") != "demo" and api_key:
            try:
                return self._agent_assessment(
                    raw_request=raw_request,
                    inspection=inspection,
                    provider=provider,
                    api_key=api_key,
                )
            except Exception:
                # Task creation remains usable if the refinement model is unavailable.
                pass
        return self._deterministic_assessment(raw_request, inspection)

    def apply_answers(
        self,
        *,
        spec: dict[str, Any],
        questions: list[dict[str, Any]],
        answers: list[dict[str, str]],
    ) -> TaskSpec:
        updated = TaskSpec.model_validate(spec)
        answer_map = {item["question_id"]: item["answer"].strip() for item in answers}
        decisions = list(updated.decisions)
        criteria = list(updated.acceptance_criteria)
        for question in questions:
            answer = answer_map.get(question["id"], "")
            if not answer:
                continue
            decisions.append(f"{question['question']}：{answer}")
            if question.get("blocking"):
                criteria.append(answer)
        return updated.model_copy(
            update={
                "decisions": self._unique(decisions),
                "acceptance_criteria": self._unique(criteria),
            }
        )

    def _agent_assessment(
        self,
        *,
        raw_request: str,
        inspection: dict[str, Any],
        provider: dict[str, Any],
        api_key: str,
    ) -> RefinementAssessment:
        model = ChatOpenAI(
            model=provider["model"],
            api_key=api_key,
            base_url=provider.get("base_url"),
            temperature=0,
        ).with_structured_output(RefinementAssessment)
        prompt = (
            "You are a repository-aware task refinement agent. Produce an editable Task Spec. "
            "Do not ask the user to repeat information already present in the request "
            "or repository. "
            "Return zero questions when the change is already sufficiently clear. If a decision is "
            "materially ambiguous, return at most three focused questions and prefill each "
            "with your best suggested answer. Mark blocking only when choosing incorrectly "
            "would materially alter "
            "behavior or data compatibility.\n\n"
            f"Request:\n{raw_request}\n\n"
            f"Repository inspection:\n{json.dumps(inspection, ensure_ascii=False)[:24000]}"
        )
        result = model.invoke(prompt)
        return RefinementAssessment.model_validate(result)

    def _deterministic_assessment(
        self, raw_request: str, inspection: dict[str, Any]
    ) -> RefinementAssessment:
        request = raw_request.strip()
        vague = len(request) < 10 or bool(
            re.search(r"(优化一下|改进一下|处理一下|随便|待定|tbd|somehow)", request, re.I)
        )
        questions: list[RefinementQuestion] = []
        if vague:
            questions.append(
                RefinementQuestion(
                    id="observable_outcome",
                    question="这次变更最重要的可观察结果是什么？",
                    reason="原始需求不足以形成唯一的验收边界。",
                    suggested_answer=request,
                    blocking=True,
                )
            )
        relevant = inspection.get("relevant_files") or inspection.get("manifests") or []
        scope = [str(item) for item in relevant[:5]] or ["仅修改实现该需求所需的最小模块"]
        spec = TaskSpec(
            goal=request,
            scope=scope,
            acceptance_criteria=[request],
            constraints=["遵循仓库已有结构、依赖与测试约定"],
            decisions=[
                f"Repository Inspection 扫描了 {inspection.get('files_scanned', 0)} 个文件",
                "仅在存在实质歧义时请求人工澄清",
            ],
        )
        return RefinementAssessment(
            spec=spec,
            questions=questions,
            rationale="需求已自动整理；仅保留无法安全推断的问题。",
            engine="local",
        )

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    def build_spec(
        self, raw_request: str, answers: list[dict[str, str]], inspection: dict[str, Any]
    ) -> TaskSpec:
        answer_map = {answer["question_id"]: answer["answer"].strip() for answer in answers}
        scope_text = answer_map.get("scope") or "根据 Repository Inspection 限制在相关模块"
        constraints_text = answer_map.get("constraints") or "复用仓库现有模式和依赖"
        behavior = answer_map.get("behavior") or raw_request
        edge_cases = answer_map.get("edge_cases")
        criteria = [behavior]
        if edge_cases:
            criteria.append(edge_cases)
        validation = answer_map.get("validation")
        if validation:
            criteria.append(validation)
        decisions = [
            "实际实现前检查现有代码与测试约定",
            f"Repository Inspection 扫描了 {inspection.get('files_scanned', 0)} 个文件",
        ]
        return TaskSpec(
            goal=raw_request.strip(),
            scope=[item.strip() for item in re.split(r"[,，;；\n]", scope_text) if item.strip()],
            acceptance_criteria=criteria,
            constraints=[
                item.strip() for item in re.split(r"[,，;；\n]", constraints_text) if item.strip()
            ],
            decisions=decisions,
            interface=answer_map.get("interface") or None,
        )
