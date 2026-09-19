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
from pydantic_core import PydanticCustomError

from .negotiation import (
    AcceptLanguageError,
    LocaleFallbackError,
    TAG_RE,
    normalize_fallbacks,
    parse_accept_language,
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


class GroupParticipant(BaseModel):
    """One group negotiation participant.

    ``accept_language`` is the raw header for that participant; ``null``
    or an empty string means the participant expresses no preference.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, title="参与者唯一标识")
    accept_language: str | None = Field(
        default=None,
        title="该参与者的原始 Accept-Language 头",
        description="缺失/null/空白串视为无偏好；语法非法时整个请求返回 422",
    )

    @field_validator("accept_language")
    @classmethod
    def _validate_header_syntax(cls, value: str | None) -> str | None:
        try:
            parse_accept_language(value)
        except AcceptLanguageError as exc:
            # Same error type as the single-header endpoint's custom
            # handler, so clients can branch uniformly on the type code.
            raise PydanticCustomError(
                "accept_language.syntax_error", str(exc)
            ) from exc
        return value


class GroupNegotiationRequest(BaseModel):
    """Request for the multi-participant group negotiation endpoint."""

    model_config = ConfigDict(extra="forbid")

    participants: list[GroupParticipant] = Field(
        min_length=1,
        title="参与者列表（非空）",
        description="每位参与者携带唯一 id 与可空的 accept_language",
    )
    supported_languages: list[str] = Field(
        min_length=1,
        title="服务端支持的 BCP 47 语言标签（按优先级/稳定顺序排列）",
    )
    default_language: str = Field(
        title="默认语言标签，必须包含在 supported_languages 中"
    )

    @field_validator("participants")
    @classmethod
    def _validate_unique_ids(cls, value: list[GroupParticipant]) -> list[GroupParticipant]:
        seen: set[str] = set()
        for participant in value:
            if participant.id in seen:
                raise ValueError(f"participants 存在重复 id: {participant.id!r}")
            seen.add(participant.id)
        return value

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

    @model_validator(mode="after")
    def _default_must_be_supported(self) -> "GroupNegotiationRequest":
        supported_lower = {tag.lower() for tag in self.supported_languages}
        if self.default_language.lower() not in supported_lower:
            raise ValueError(
                "default_language 必须包含在 supported_languages 中（大小写不敏感）"
            )
        return self


class GroupParsedRange(BaseModel):
    range: str
    q: float
    position: int = Field(description="在该参与者 Accept-Language 头中的序号（从 1 开始）")


class GroupIgnoredDuplicate(BaseModel):
    range: str
    position: int


class GroupCellBase(BaseModel):
    matched_range: str | None = Field(
        description="该参与者在该候选上实际命中的范围（具体范围或 '*'）；未命中为 null"
    )
    matched_via: Literal["concrete", "wildcard"] | None = Field(
        description="命中方式：具体范围 / 通配符；无偏好或未命中为 null"
    )
    q: float | None = Field(description="命中范围的权重；无偏好记 1.0，未命中为 null")
    header_position: int | None = Field(description="命中范围在头中的序号")
    status: Literal["eligible", "excluded", "unmatched", "no-range"]


class GroupCell(GroupCellBase):
    """Matrix cell inside a candidate report, aligned with participants order."""


class GroupParticipantCell(GroupCellBase):
    supported_index: int = Field(description="对应 supported_languages 的下标")


class GroupVeto(BaseModel):
    participant_id: str
    matched_range: str
    q: float = Field(description="固定为 0：具体范围 q=0 才构成全局否决")
    header_position: int


class GroupRejection(BaseModel):
    participant_id: str
    matched_range: str | None
    q: float | None
    status: Literal["excluded", "unmatched"]


class GroupCandidateReport(BaseModel):
    tag: str
    supported_index: int
    truncation_chain: list[str]
    status: Literal["selected", "eligible", "vetoed", "not-common"]
    min_q: float | None = Field(
        description="所有可接受参与者个人 q 的最低值；候选被否决/非共同可接受时为 null"
    )
    total_q: float | None = Field(
        description="所有可接受参与者个人 q 之和；候选被否决/非共同可接受时为 null"
    )
    vetoed_by: list[GroupVeto] = Field(description="以具体 q=0 范围否决该候选的参与者")
    rejected_by: list[GroupRejection] = Field(
        description="以非否决方式不接受该候选的参与者（通配符 q=0 排除或未命中）"
    )
    cells: list[GroupCell] = Field(
        description="与 participants 同序的逐人命中矩阵单元格（含无偏好标记）"
    )


class GroupParticipantReport(BaseModel):
    id: str
    accept_language: str | None = Field(description="回显该参与者的原始请求头")
    parsed_ranges: list[GroupParsedRange]
    ignored_duplicates: list[GroupIgnoredDuplicate]
    cells: list[GroupParticipantCell] = Field(
        description="该参与者针对每个候选实际命中的范围与状态"
    )


class GroupNegotiationResponse(BaseModel):
    selected_language: str
    selected_supported_index: int
    fallback: bool
    fallback_reason: Literal["no-common-acceptable-candidate"] | None
    message: str
    candidates: list[GroupCandidateReport]
    participants: list[GroupParticipantReport]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
