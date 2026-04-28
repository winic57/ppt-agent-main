from __future__ import annotations

import re
import time
from dataclasses import dataclass
from textwrap import shorten
from typing import Any

from app.services.model_gateway import ModelGateway
from app.services.prompt_contracts import get_prompt_text, render_prompt

STYLE_PACKS: dict[str, dict[str, Any]] = {
    "minimalism": {
        "style_id": "minimalism",
        "style_name": "极简主义",
        "description": "留白优先，信息秩序清晰，适合正式汇报。",
        "palette": {
            "background": "#FFFFFF",
            "surface": "#F5F5F5",
            "surface_alt": "#EBEEF2",
            "text_primary": "#1F2937",
            "text_secondary": "#6B7280",
            "accent_primary": "#002FA7",
        },
    },
    "consulting": {
        "style_id": "consulting",
        "style_name": "咨询风",
        "description": "结构克制，结论优先，强调信息分层。",
        "palette": {
            "background": "#FFFFFF",
            "surface": "#EEF4F8",
            "surface_alt": "#DCE9F2",
            "text_primary": "#0F172A",
            "text_secondary": "#475569",
            "accent_primary": "#003366",
        },
    },
    "tech-dark": {
        "style_id": "tech-dark",
        "style_name": "科技暗色",
        "description": "偏科技感和对比度，适合技术演示。",
        "palette": {
            "background": "#0B1120",
            "surface": "#111827",
            "surface_alt": "#172033",
            "text_primary": "#F8FAFC",
            "text_secondary": "#94A3B8",
            "accent_primary": "#00E5FF",
        },
    },
    "swiss-style": {
        "style_id": "swiss-style",
        "style_name": "瑞士风格",
        "description": "强网格、少装饰、版式先行。",
        "palette": {
            "background": "#FFFFFF",
            "surface": "#F5F5F5",
            "surface_alt": "#EFE7E7",
            "text_primary": "#111111",
            "text_secondary": "#5E5E5E",
            "accent_primary": "#D90429",
        },
    },
    "brand-blue": {
        "style_id": "brand-blue",
        "style_name": "品牌蓝",
        "description": "明亮专业，适合标准商务汇报。",
        "palette": {
            "background": "#F8FBFF",
            "surface": "#EEF5FF",
            "surface_alt": "#D8E6FF",
            "text_primary": "#14213D",
            "text_secondary": "#486581",
            "accent_primary": "#016BFF",
        },
    },
}

_EMPTY_DATA_UPDATES = {
    "question_patch": None,
    "answer_patch": None,
    "outline_patch": None,
    "page_patch": None,
    "summary_patch": None,
}
_URL_RE = re.compile(r"https?://\S+")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class _SvgPromptAttempt:
    system_prompt: str
    user_payload: str | dict[str, Any]
    timeout_seconds: int
    retries: int = 1


class GenerationService:
    def __init__(self) -> None:
        self.models = ModelGateway()

    def route_workspace_intent(
        self,
        *,
        router_payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = self.models.context_json(
            get_prompt_text("workspace.intent_router.system"),
            render_prompt(
                "workspace.intent_router.user",
                {
                    "project_id": router_payload["project_id"],
                    "project_stage": router_payload["project_stage"],
                    "ui_surface": router_payload["ui_surface"],
                    "latest_user_message": router_payload["latest_user_message"],
                    "recent_messages_json": router_payload["recent_messages"],
                    "project_request": router_payload["project_request"],
                    "workflow_constraints_json": router_payload["workflow_constraints"],
                    "fixed_fields_json": router_payload["fixed_fields"],
                    "project_level_status_summary_json": router_payload["project_level_status_summary"],
                    "outline_state_snapshot_json": router_payload["outline_state_snapshot"],
                    "page_context_json": router_payload["page_context"],
                },
            ),
        )
        action_type = str(result.get("action_type") or "reject")
        decision = {
            "scope_type": str(result.get("scope_type") or router_payload.get("default_scope_type") or "project"),
            "target_stage": str(result.get("target_stage") or router_payload["project_stage"]),
            "target_page_id": result.get("target_page_id"),
            "intent_type": str(result.get("intent_type") or action_type),
            "action_type": action_type,
            "should_execute": bool(result.get("should_execute", False)),
            "needs_clarification": bool(result.get("needs_clarification", False)),
            "requires_confirmation": bool(result.get("requires_confirmation", False)),
            "missing_data": self._coerce_string_list(result.get("missing_data")),
            "data_updates": self._normalize_data_updates(result.get("data_updates")),
            "execution_plan": self._normalize_execution_plan(result.get("execution_plan")),
            "next_recommendations": self._normalize_recommendations(result.get("next_recommendations")),
            "reason": str(result.get("reason") or ""),
        }
        if not decision["execution_plan"] and action_type != "reject":
            decision["execution_plan"] = [
                {
                    "step_code": action_type,
                    "step_name": action_type,
                    "reason": decision["reason"] or "按当前动作执行。",
                }
            ]
        return decision

    def generate_project_title(self, request_text: str) -> str:
        cleaned = re.sub(r"\s+", " ", request_text).strip()
        if not cleaned:
            return "未命名项目"
        return shorten(cleaned, width=24, placeholder="...")

    def generate_init_fast_questions(
        self,
        *,
        project_title: str,
        request_text: str,
        init_search_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self.models.context_json(
            get_prompt_text("init.fast_question_generate.system"),
            render_prompt(
                "init.fast_question_generate.user",
                {
                    "project_title": project_title,
                    "request_text": request_text,
                    "init_search_results_json": init_search_results,
                },
            ),
        )
        page_count_options = self._normalize_page_count_options(result.get("page_count_options"))
        ai_questions = self._normalize_questions(result.get("ai_questions"))
        if not page_count_options or not ai_questions:
            raise RuntimeError("init.fast_question_generate 返回内容不完整")
        return {
            "page_count_options": page_count_options,
            "ai_questions": ai_questions,
        }

    def generate_outline(
        self,
        *,
        project_title: str,
        request_text: str,
        page_count_target: int,
        style_preset: str,
        background_asset_path: str | None,
        answers: dict[str, Any],
        init_corpus_evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self.models.context_json(
            get_prompt_text("outline.generate.system"),
            render_prompt(
                "outline.generate.user",
                {
                    "project_title": project_title,
                    "request_text": request_text,
                    "page_count_target_json": page_count_target,
                    "style_preset": style_preset,
                    "background_asset_path": background_asset_path or "",
                    "answers_json": answers,
                    "init_corpus_evidence_json": init_corpus_evidence,
                },
            ),
        )
        if "ppt_outline" not in result:
            raise RuntimeError("outline.generate 缺少 ppt_outline")
        return result

    def generate_page_search_queries(
        self,
        *,
        project_title: str,
        project_request: str,
        page_id: str,
        page_title: str,
        page_bullets: list[str],
        page_section_title: str | None,
        outline_full_snapshot: list[dict[str, Any]],
        latest_instruction: str,
    ) -> list[dict[str, str]]:
        result = self.models.context_json(
            get_prompt_text("page.search_query_expand.system"),
            render_prompt(
                "page.search_query_expand.user",
                {
                    "project_title": project_title,
                    "project_request": project_request,
                    "page_id": page_id,
                    "page_title": page_title,
                    "page_bullets_json": page_bullets,
                    "page_section_title": page_section_title or "",
                    "outline_full_snapshot_json": outline_full_snapshot,
                    "latest_instruction": latest_instruction,
                },
            ),
        )
        queries = self._normalize_search_queries(result.get("page_search_queries"))
        if not queries:
            raise RuntimeError("page.search_query_expand 未返回有效搜索词")
        return queries

    def generate_page_outline_patch(
        self,
        *,
        latest_user_message: str,
        page_id: str,
        page_title: str,
        page_bullets: list[str],
        page_section_title: str | None,
        outline_full_snapshot: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self.models.context_json(
            get_prompt_text("page.outline_patch.system"),
            render_prompt(
                "page.outline_patch.user",
                {
                    "latest_user_message": latest_user_message,
                    "page_id": page_id,
                    "page_title": page_title,
                    "page_bullets_json": page_bullets,
                    "page_section_title": page_section_title or "",
                    "outline_full_snapshot_json": outline_full_snapshot,
                },
            ),
        )
        page_patch = result.get("page_patch")
        if not isinstance(page_patch, dict):
            raise RuntimeError("page.outline_patch 缺少 page_patch")
        title = str(page_patch.get("title") or "").strip()
        bullets = [str(item).strip() for item in page_patch.get("content_outline", []) if str(item).strip()]
        if not title or not bullets:
            raise RuntimeError("page.outline_patch 返回的标题或要点为空")
        return {
            "title": title,
            "content_outline": bullets,
            "section_title": (str(page_patch.get("section_title")).strip() or None)
            if page_patch.get("section_title") is not None
            else None,
            "change_summary": str(page_patch.get("change_summary") or ""),
        }

    def generate_summary_patch(
        self,
        *,
        latest_user_message: str,
        page_title: str,
        page_bullets: list[str],
        current_summary_md: str,
    ) -> dict[str, str]:
        result = self.models.context_json(
            get_prompt_text("page.summary_patch.system"),
            render_prompt(
                "page.summary_patch.user",
                {
                    "latest_user_message": latest_user_message,
                    "page_title": page_title,
                    "page_bullets_json": page_bullets,
                    "current_summary_md": current_summary_md,
                },
            ),
        )
        patch = result.get("summary_patch")
        if not isinstance(patch, dict):
            raise RuntimeError("page.summary_patch 缺少 summary_patch")
        summary_md = str(patch.get("summary_md") or "").strip()
        if not summary_md:
            raise RuntimeError("page.summary_patch 返回空 summary")
        return {"summary_md": summary_md}

    def summarize_selected_sources(
        self,
        *,
        scope_type: str,
        research_goal: str,
        selected_sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self.models.context_json(
            get_prompt_text("research.summary.system"),
            render_prompt(
                "research.summary.user",
                {
                    "scope_type": scope_type,
                    "research_goal": research_goal,
                    "selected_sources_json": selected_sources,
                },
            ),
        )
        summary_md = str(result.get("summary_md") or "").strip()
        if not summary_md:
            raise RuntimeError("research.summary 缺少 summary_md")
        return {
            "summary_md": summary_md,
            "key_findings": self._coerce_string_list(result.get("key_findings")),
            "open_questions": self._coerce_string_list(result.get("open_questions")),
        }

    def generate_draft_svg(self, *, page_context: dict[str, Any]) -> str:
        return self._run_svg_prompt_attempts(self._build_draft_svg_attempts(page_context))

    def generate_design_svg(
        self,
        *,
        draft_svg: str,
        style_pack_id: str,
        background_asset_path: str | None,
    ) -> str:
        return self.models.svg_text(
            get_prompt_text("design.svg_generate.system"),
            render_prompt(
                "design.svg_generate.user",
                {
                    "draft_svg_markup": draft_svg,
                    "style_pack_json": self.get_style_pack(style_pack_id),
                    "background_asset_json": {"asset_path": background_asset_path} if background_asset_path else None,
                },
            ),
        )

    def get_style_pack(self, style_id: str | None) -> dict[str, Any]:
        if not style_id:
            raise RuntimeError("style_pack_id 不能为空")
        style_pack = STYLE_PACKS.get(style_id)
        if style_pack is None:
            style_hint = str(style_id).strip()
            style_pack = {
                "style_id": "custom",
                "style_name": "自定义风格",
                "description": f"用户自定义风格要求：{style_hint}",
                "palette": {
                    "background": "#F8FAFC",
                    "surface": "#FFFFFF",
                    "surface_alt": "#E2E8F0",
                    "text_primary": "#0F172A",
                    "text_secondary": "#475569",
                    "accent_primary": "#2563EB",
                },
            }
        return style_pack

    def list_style_options(self) -> list[dict[str, Any]]:
        return [STYLE_PACKS[key] for key in STYLE_PACKS]

    def _build_draft_svg_attempts(self, page_context: dict[str, Any]) -> list[_SvgPromptAttempt]:
        page = page_context["page"]
        summary = page_context["summary"]
        page_role = str(page.get("page_role") or "content").strip() or "content"
        primary_timeout = max(45, min(self.models.settings.svg_llm_timeout_seconds, 75))
        fallback_timeout = max(35, min(self.models.settings.svg_llm_timeout_seconds, 50))
        compact_outline = self._compact_outline(page.get("content_outline"))
        compact_summary = self._compact_text(summary.get("summary_md") or page.get("content_summary") or page.get("title") or "", 240)
        compact_instruction = self._compact_text(page_context.get("latest_instruction") or "", 160)

        if page_role != "content":
            return [
                _SvgPromptAttempt(
                    system_prompt=self._build_fixed_page_system_prompt(page_role),
                    user_payload={
                        "canvas": {"width": 1280, "height": 720},
                        "page_role": page_role,
                        "title": self._compact_text(page.get("title") or "", 120),
                        "content_outline": compact_outline[:6],
                        "summary": compact_summary,
                        "latest_instruction": compact_instruction,
                    },
                    timeout_seconds=fallback_timeout,
                    retries=2,
                ),
                _SvgPromptAttempt(
                    system_prompt=(
                        "Generate one readable 1280x720 slide as a complete SVG document. "
                        "Output only a single <svg>. Keep neutral styling, simple blocks, and real text content."
                    ),
                    user_payload={
                        "title": self._compact_text(page.get("title") or "", 120),
                        "bullets": compact_outline[:5],
                        "summary": compact_summary,
                        "latest_instruction": compact_instruction,
                    },
                    timeout_seconds=fallback_timeout,
                ),
            ]

        sanitized_summary = self._compact_text(summary.get("summary_md") or "", 560)
        sanitized_sources = self._compact_sources(summary.get("selected_sources"), max_items=2)
        evidence_lines = self._extract_summary_points(summary.get("summary_md") or "", max_items=4)
        evidence_lines.extend(self._source_evidence_lines(sanitized_sources, max_items=1))

        return [
            _SvgPromptAttempt(
                system_prompt=(
                    "Generate one 1280x720 content slide as a complete SVG document. "
                    "Output only a single <svg>. Build a clear information hierarchy from the real title and key points. "
                    "Use 2-4 simple aligned cards, readable typography, short evidence text, and minimal decoration."
                ),
                user_payload={
                    "title": self._compact_text(page.get("title") or "", 140),
                    "content_outline": compact_outline[:4],
                    "content_summary": self._compact_text(page.get("content_summary") or "", 160),
                    "summary": sanitized_summary,
                    "evidence_lines": evidence_lines[:4],
                    "latest_instruction": compact_instruction,
                },
                timeout_seconds=primary_timeout,
            ),
            _SvgPromptAttempt(
                system_prompt=(
                    "Generate one 1280x720 content slide as a complete SVG document. "
                    "Output only a single <svg>. Use the real title, 3-5 key bullets, and a few short evidence lines. "
                    "Prefer 2-4 clean cards, strong readability, and minimal decoration."
                ),
                user_payload={
                    "title": self._compact_text(page.get("title") or "", 140),
                    "content_outline": compact_outline[:4],
                    "content_summary": self._compact_text(page.get("content_summary") or "", 160),
                    "evidence_lines": evidence_lines[:4],
                    "latest_instruction": compact_instruction,
                },
                timeout_seconds=fallback_timeout,
            ),
            _SvgPromptAttempt(
                system_prompt=(
                    "Generate one simple 1280x720 slide as SVG. Output only the SVG. "
                    "Use a clear title and up to four concise bullet points. Neutral colors, no complex graphics."
                ),
                user_payload={
                    "title": self._compact_text(page.get("title") or "", 140),
                    "bullets": (compact_outline or evidence_lines or [compact_summary])[:4],
                    "latest_instruction": compact_instruction,
                },
                timeout_seconds=fallback_timeout,
            ),
        ]

    def _run_svg_prompt_attempts(self, attempts: list[_SvgPromptAttempt]) -> str:
        last_error: Exception | None = None
        for attempt in attempts:
            for retry_index in range(attempt.retries):
                try:
                    return self.models.svg_text(
                        attempt.system_prompt,
                        attempt.user_payload,
                        timeout_seconds=attempt.timeout_seconds,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if self._is_hard_rate_limited_svg_error(exc):
                        raise exc
                    if retry_index + 1 >= attempt.retries or not self._is_retryable_svg_error(exc):
                        break
                    time.sleep(min(2 * (retry_index + 1), 4))
        if last_error is not None:
            raise last_error
        raise RuntimeError("SVG 生成失败")

    def _build_fixed_page_system_prompt(self, page_role: str) -> str:
        role_instructions = {
            "cover": "Create a clean cover slide with a strong title and one supporting subtitle block.",
            "toc": "Create a clean agenda slide with the title and up to six concise agenda items.",
            "end": "Create a closing slide with the title and one short closing line.",
        }
        return (
            "Generate one 1280x720 slide as a complete SVG document. "
            "Output only a single <svg>. Keep the slide readable, neutral, and presentation-ready. "
            + role_instructions.get(page_role, "Create a simple informational slide with clear text hierarchy.")
        )

    def _compact_outline(self, payload: Any) -> list[str]:
        if not isinstance(payload, list):
            return []
        items: list[str] = []
        for raw_item in payload:
            text = self._compact_text(raw_item, 120)
            if text:
                items.append(text)
        return items

    def _compact_sources(self, payload: Any, *, max_items: int) -> list[dict[str, str]]:
        if not isinstance(payload, list):
            return []
        items: list[dict[str, str]] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            title = self._compact_text(raw_item.get("title") or raw_item.get("source_title") or "", 100)
            snippet = self._compact_text(raw_item.get("snippet") or raw_item.get("summary") or raw_item.get("excerpt") or "", 180)
            if not title and not snippet:
                continue
            item: dict[str, str] = {}
            if title:
                item["title"] = title
            if snippet:
                item["snippet"] = snippet
            items.append(item)
            if len(items) >= max_items:
                break
        return items

    def _extract_summary_points(self, summary_md: str, *, max_items: int) -> list[str]:
        points: list[str] = []
        for raw_line in str(summary_md or "").splitlines():
            line = re.sub(r"^[#>*\-\d.\s]+", "", raw_line).strip()
            line = self._compact_text(line, 140)
            if line:
                points.append(line)
            if len(points) >= max_items:
                break
        return points

    def _source_evidence_lines(self, sources: list[dict[str, str]], *, max_items: int) -> list[str]:
        lines: list[str] = []
        for source in sources[:max_items]:
            title = source.get("title") or ""
            snippet = source.get("snippet") or ""
            line = self._compact_text(" - ".join(part for part in [title, snippet] if part), 180)
            if line:
                lines.append(line)
        return lines

    def _compact_text(self, value: Any, max_chars: int) -> str:
        text = str(value or "")
        text = _URL_RE.sub("[link]", text)
        text = text.replace("```", " ")
        text = _WHITESPACE_RE.sub(" ", text).strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "…"

    def _is_retryable_svg_error(self, exc: Exception) -> bool:
        if self._is_hard_rate_limited_svg_error(exc):
            return False
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        message = str(exc).lower()
        return status_code in {408, 409, 425, 429, 500, 502, 503, 504} or any(
            token in message
            for token in ["gateway time-out", "fair use policy", "timed out", "rate limit", "temporarily unavailable"]
        )

    def _is_hard_rate_limited_svg_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        message = str(exc).lower()
        if status_code != 429:
            return False
        return any(
            token in message
            for token in [
                "fair use policy",
                "request rate has been restricted",
                "personal center",
                "code': '1313'",
                '"code": "1313"',
                '"code":"1313"',
            ]
        )

    def _normalize_search_queries(self, payload: Any) -> list[dict[str, str]]:
        if not isinstance(payload, list):
            return []
        items: list[dict[str, str]] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            query_text = str(raw_item.get("query_text") or raw_item.get("query") or "").strip()
            query_purpose = str(raw_item.get("query_purpose") or raw_item.get("query_intent") or raw_item.get("intent") or "").strip()
            if query_text:
                items.append({"query_text": query_text, "query_purpose": query_purpose})
        return items

    def _normalize_page_count_options(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        options: list[dict[str, Any]] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            label = str(raw_item.get("label") or "").strip()
            reason = str(raw_item.get("reason") or "").strip()
            page_count = raw_item.get("page_count")
            if not label or not isinstance(page_count, int):
                continue
            options.append(
                {
                    "option_code": str(raw_item.get("option_code") or f"OPT-{len(options) + 1}"),
                    "label": label,
                    "page_count": page_count,
                    "reason": reason,
                }
            )
        return options

    def _normalize_questions(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        questions: list[dict[str, Any]] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            question_code = str(raw_item.get("question_code") or "").strip()
            label = str(raw_item.get("label") or "").strip()
            if not question_code or not label:
                continue
            options = []
            for option in raw_item.get("options", []):
                if not isinstance(option, dict):
                    continue
                option_label = str(option.get("label") or "").strip()
                if not option_label:
                    continue
                options.append(
                    {
                        "option_code": str(option.get("option_code") or f"OPT-{len(options) + 1}"),
                        "label": option_label,
                    }
                )
            if len(options) != 3:
                continue
            questions.append(
                {
                    "question_code": question_code,
                    "label": label,
                    "description": str(raw_item.get("description") or "").strip(),
                    "options": options,
                    "allow_custom": bool(raw_item.get("allow_custom", True)),
                }
            )
        return questions

    def _normalize_execution_plan(self, payload: Any) -> list[dict[str, str]]:
        if not isinstance(payload, list):
            return []
        plan: list[dict[str, str]] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            step_code = str(raw_item.get("step_code") or "").strip()
            step_name = str(raw_item.get("step_name") or step_code).strip()
            reason = str(raw_item.get("reason") or "").strip()
            if not step_code:
                continue
            plan.append(
                {
                    "step_code": step_code,
                    "step_name": step_name,
                    "reason": reason,
                }
            )
        return plan

    def _normalize_recommendations(self, payload: Any) -> list[dict[str, str]]:
        if not isinstance(payload, list):
            return []
        recommendations: list[dict[str, str]] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            code = str(raw_item.get("code") or "").strip()
            label = str(raw_item.get("label") or "").strip()
            reason = str(raw_item.get("reason") or "").strip()
            if code and label:
                recommendations.append({"code": code, "label": label, "reason": reason})
        return recommendations

    def _coerce_string_list(self, payload: Any) -> list[str]:
        if not isinstance(payload, list):
            return []
        return [str(item).strip() for item in payload if str(item).strip()]

    def _normalize_data_updates(self, payload: Any) -> dict[str, Any]:
        normalized = dict(_EMPTY_DATA_UPDATES)
        if not isinstance(payload, dict):
            return normalized
        for key in normalized:
            if key in payload:
                normalized[key] = payload[key]
        return normalized
