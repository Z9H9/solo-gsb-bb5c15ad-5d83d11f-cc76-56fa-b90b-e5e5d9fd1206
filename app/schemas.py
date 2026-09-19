"""Pydantic request/response models for the language negotiation API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .negotiation import (
    LocaleFallbackError,
    TAG_RE,
    normalize_fallbacks,
    validate_fallback_chains,
)


class NegotiationRequest(BaseModel):
    """Negotiation input.

    ``accept_language`` is the raw header value; ``null``/missing or an
    empty string means "no valid ranges" and triggers the default
    fallback. ``locale_fallbacks`` is an optional directed-chain mapping
    of source BCP 47 tag -> next BCP 47 tag consulted only when standard
    lookup has no acceptable candidate.
    """

    model_config = ConfigDict(extra="forbid")

    accept_language: str | None = Field(
        default=None,
        title="原始 Accept-Language 头",
        description="例如 'zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5'；缺失/空串视为无有效范围",
    )
    supported_languages: list[str] = Field(
        min_length=1,
        title="服务端支持的 BCP 47 语言标签（按优先级/稳定顺序排列）",
    )
    default_language: str = Field(
        title="默认语言标签，必须包含在 supported_languages 中"
    )
    locale_fallbacks: dict[str, str] | None = Field(
        default=None,
        title="可选的语言回退有向链（源标签 -> 下一标签）",
        description=(
            "仅当标准 lookup 没有可接受候选时启用：按正 q 具体范围权重降序、"
            "头部顺序依次沿链查找首个受支持语言；被任一 q=0 具体范围排除的"
            "候选不会被链选中。源标签不可重复（大小写不敏感），标签必须合法，"
            "不允许环路，且每条链必须能到达某个受支持语言。"
        ),
    )

    @field_validator("supported_languages")
    @classmethod
    def _validate_supported(cls, value: list[str]) -> list[str]:
        normalized_seen: set[str] = set()
        for tag in value:
            if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
                raise ValueError(f"不是合法的 BCP 47 标签: {tag!r}")
            key = tag.lower()
            if key in normalized_seen:
                raise ValueError(f"supported_languages 存在重复标签（大小写不敏感）: {tag!r}")
            normalized_seen.add(key)
        return value

    @field_validator("default_language")
    @classmethod
    def _validate_default(cls, value: str) -> str:
        if not TAG_RE.fullmatch(value):
            raise ValueError(f"default_language 不是合法的 BCP 47 标签: {value!r}")
        return value

    @field_validator("locale_fallbacks", mode="before")
    @classmethod
    def _validate_fallbacks_shape(cls, value: Any) -> Any:
        if value is None:
            return None
        try:
            return normalize_fallbacks(value)
        except LocaleFallbackError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("locale_fallbacks")
    @classmethod
    def _validate_fallback_chains(
        cls, value: dict[str, str] | None, info: ValidationInfo
    ) -> dict[str, str] | None:
        # Runs after supported_languages/default_language validators
        # (field declaration order), so topology errors are located at
        # ["body", "locale_fallbacks"].
        if value is not None and info.data.get("supported_languages"):
            try:
                validate_fallback_chains(value, info.data["supported_languages"])
            except LocaleFallbackError as exc:
                raise ValueError(str(exc)) from exc
        return value

    @model_validator(mode="after")
    def _default_must_be_supported(self) -> "NegotiationRequest":
        supported_lower = {tag.lower() for tag in self.supported_languages}
        if self.default_language.lower() not in supported_lower:
            raise ValueError(
                "default_language 必须包含在 supported_languages 中（大小写不敏感）"
            )
        return self


class TraceEvent(BaseModel):
    truncated_to: str = Field(description="逐级截断后的标签前缀，或 '*' 通配符")
    outcome: Literal["match", "no-match", "skipped"]
    matched_range: str | None = None
    q: float | None = None
    header_position: int | None = None
    detail: str | None = None


class CandidateReport(BaseModel):
    tag: str
    supported_index: int
    truncation_chain: list[str]
    events: list[TraceEvent]
    matched_range: str | None
    q: float | None = Field(description="命中范围的客户端权重；未命中为 null")
    status: Literal["selected", "eligible", "excluded", "unmatched", "no-range"]
    detail: str


class ParsedRange(BaseModel):
    range: str
    q: float
    position: int = Field(description="在 Accept-Language 头中的序号（从 1 开始）")


class IgnoredDuplicate(BaseModel):
    range: str
    position: int


class LocaleFallbackReport(BaseModel):
    triggered: bool = Field(description="是否由 locale_fallbacks 链选中语言")
    source_range: str = Field(
        description="触发回退的客户端具体范围（小写、去重后的范围）"
    )
    source_q: float = Field(description="触发范围的客户端权重（严格为正）")
    source_position: int = Field(description="触发范围在 Accept-Language 头中的序号")
    path: list[str] = Field(
        description="实际走过的有向链路径（小写标签，含源与终点）"
    )
    selected_language: str = Field(description="链最终命中的受支持标签（服务端原始大小写）")


class NegotiationResponse(BaseModel):
    selected_language: str
    selected_supported_index: int
    fallback: bool
    fallback_reason: Literal["no-valid-range", "no-acceptable-candidate"] | None
    message: str
    parsed_ranges: list[ParsedRange]
    ignored_duplicates: list[IgnoredDuplicate]
    candidates: list[CandidateReport]
    # Present only when a locale_fallbacks chain actually selected the
    # language; the endpoint drops this key entirely when unused, so
    # requests without locale_fallbacks keep the exact legacy payload.
    locale_fallback: LocaleFallbackReport | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
