"""LLM 服务封装: OpenAI 兼容 API(生产) 或 离线抽取式 Mock(演示/兜底)。

为 LangGraph 对话图统一提供四项能力:
    1. condense_question  多轮问题改写(指代消解,提升检索质量)
    2. grade_relevance    检索片段相关性精筛(防幻觉·第一道闸)
    3. astream_answer     基于受限上下文的流式生成
    4. verify_answer      回答-资料一致性校验(防幻觉·第二道闸)

Mock 模式完全离线: 生成采用"原文抽取式"策略(直接摘录说明书原句),
天然零幻觉, 保证流水线在无 API Key 环境下可完整演示与测试。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from app.config import Settings, get_settings
from app.core.embeddings import BaseEmbedding, get_embedding_provider

logger = logging.getLogger(__name__)

_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])\s*|\n+")
_PRONOUNS = re.compile(r"它|他|她|这个|那个|该|上述|前面提到的|刚才")
_CITE_MARK = re.compile(r"\[(\d+)\]")


def split_sentences(text: str) -> List[str]:
    """中文友好的句子切分"""
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]
    return parts


def char_bigrams(text: str) -> set:
    t = re.sub(r"[\s，。、；：？！,.:;?!\[\]（）()【】\"'“”‘’]", "", text)
    return {t[i : i + 2] for i in range(max(len(t) - 1, 0))} or ({t} if t else set())


def keyword_overlap(question: str, text: str) -> float:
    """问题字符 bigram 在文本中的覆盖率"""
    q = char_bigrams(question)
    if not q:
        return 0.0
    t = re.sub(r"\s+", "", text)
    hit = sum(1 for g in q if g in t)
    return hit / len(q)


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。、；：？！,.:;?!\[\]()（）【】\"'“”‘’]", "", text)


@dataclass
class VerifyResult:
    supported: bool
    notes: str = ""
    unsupported: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
CONDENSE_SYSTEM = (
    "你是查询改写器。请把用户的最新问题结合对话历史改写为一个语义完整、"
    "不依赖上下文指代的独立问题。只输出改写后的问题, 不要任何解释。"
)

GRADE_SYSTEM = (
    "你是相关性判定器。判断给定资料片段是否包含与问题直接相关的信息"
    "(能帮助回答该问题)。只输出 yes 或 no, 不要输出其他内容。"
)

GENERATE_SYSTEM = """你是"RAG 智能助手", 一名严谨的企业软件维保技术支持专家。你必须严格遵守:
1. 只能依据 <参考资料> 回答, 严禁使用资料之外的知识, 严禁编造任何命令、参数、数值、版本号。
2. 回答中每个事实性陈述后必须紧跟来源编号, 格式如 [1]、[2], 编号对应参考资料。
3. 指明出处时只能使用 [n] 编号, 严禁自行编写章节编号或章节标题(如"5.1 登录网关"), 出处由前端引用卡片展示。
4. 若参考资料不足以回答问题, 必须回答: "根据现有产品说明书资料, 暂时无法回答该问题", 并建议联系人工维保支持。
5. 步骤、命令、参数、路径、端口号等一律照抄资料原文, 保持精确。
6. 使用简体中文, 条理清晰, 直接给出可操作的答案。"""

VERIFY_SYSTEM = """你是事实一致性审核员。请逐句审核 <回答> 中的事实性陈述是否被 <参考资料> 支持。
每条资料首行的"来源:"标注了其文档名与章节位置, 这些元数据同样作为审核依据:
回答中出现的产品名、文档名、章节名只要与来源行一致即视为有据, 不算幻觉。
重点审核: (a) 是否存在资料正文与来源行中都没有的命令/参数/数值/结论(幻觉);
(b) 是否为事实性陈述标注了 [n] 来源编号。
不要因为措辞改写、语句顺序调整而判定不支持; 只有核心事实无依据时才判 false。
只输出 JSON, 格式: {"supported": true/false, "unsupported": ["不被支持的陈述", ...], "notes": "简要说明"}
"""  # noqa: E501

FOLLOWUP_SYSTEM = (
    "你是检索式问答助手的追问推荐器。根据用户的问题与回答引用到的资料文档, "
    "生成几个简短、具体、可继续追问的下一个问题(用于点击继续对话)。"
    "问题必须贴合已引用文档的内容, 长度不超过 30 字, 不要重复用户原问题。"
    "只输出 JSON, 格式: {\"questions\": [\"追问1\", \"追问2\", \"追问3\"]}"
)


class LLMService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.embedder: BaseEmbedding = get_embedding_provider()
        self._client = None
        self.reload()

    def reload(self) -> None:
        """按当前(动态)LLM 配置重建 client, 支持运行热切换 mock<->openai。

        开启 openai 但缺 API KEY 时回退 mock; 写回 self.provider 供 is_mock 判断。
        """
        from app.core.settings_rt import effective_settings

        eff = effective_settings()
        provider = str(eff.llm_provider or self.settings.llm_provider).lower()
        base_url = eff.llm_base_url or self.settings.llm_base_url
        api_key = eff.llm_api_key or self.settings.llm_api_key
        timeouts = self.settings.llm_timeout
        self._client = None
        if provider == "openai":
            if not api_key:
                logger.warning("LLM_PROVIDER=openai 但未配置 API KEY, 回退 Mock 模式")
                self.provider = "mock"
            else:
                from openai import AsyncOpenAI

                self._client = AsyncOpenAI(
                    base_url=base_url or None,
                    api_key=api_key,
                    timeout=timeouts,
                )
                self.provider = "openai"
                logger.info(
                    "LLM(OpenAI 兼容) 就绪: base=%s model=%s",
                    base_url,
                    eff.llm_model or self.settings.llm_model,
                )
        else:
            self.provider = "mock"
            logger.info("LLM 使用离线抽取式 Mock 模式(零幻觉演示)")

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"

    def _eff(self):
        """动态配置视图(LLM 各项 Warm 覆盖优先)。"""
        from app.core.settings_rt import effective_settings

        return effective_settings()

    def _current_model(self) -> str:
        eff = self._eff()
        return eff.llm_model or self.settings.llm_model

    def _current_temperature(self) -> float:
        eff = self._eff()
        return eff.llm_temperature if eff.is_overridden("llm_temperature") else self.settings.llm_temperature

    # ------------------------------------------------------------------
    # 基础调用
    # ------------------------------------------------------------------
    async def _acomplete(self, system: str, user: str, json_mode: bool = False) -> str:
        kwargs: Dict[str, Any] = dict(
            model=self._current_model(),
            temperature=self._current_temperature(),
            max_tokens=self.settings.llm_max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except Exception:
            # 部分推理模型/网关不支持 response_format, 去掉后重试
            if json_mode and "response_format" in kwargs:
                kwargs.pop("response_format")
                resp = await self._client.chat.completions.create(**kwargs)
            else:
                raise
        self._record_usage(getattr(resp, "usage", None))
        return resp.choices[0].message.content or ""

    def _record_usage(self, usage: Any) -> None:
        """记录一次真实 LLM 调用的 token 消耗(供成本核算)。"""
        if usage is None:
            return
        try:
            from app.services.llm_usage import get_llm_usage_store
            get_llm_usage_store().record(
                self.settings.llm_model, self.provider,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("记录 LLM 用量失败: %s", exc)

    @staticmethod
    def _extract_json(raw: str) -> Dict[str, Any]:
        """宽松解析模型输出的 JSON(容忍 ```json 围栏与前后缀文本)"""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        return json.loads(cleaned)

    # ------------------------------------------------------------------
    # 1) 多轮问题改写
    # ------------------------------------------------------------------
    async def condense_question(
        self, question: str, history: List[Dict[str, str]]
    ) -> str:
        if not history:
            return question
        if self.is_mock:
            return self._heuristic_condense(question, history)
        try:
            lines = "\n".join(
                f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:200]}"
                for m in history[-self.settings.history_window_messages :]
            )
            rewritten = await self._acomplete(
                CONDENSE_SYSTEM,
                f"<对话历史>\n{lines}\n</对话历史>\n最新问题: {question}\n改写后的独立问题:",
            )
            rewritten = rewritten.strip().strip('"“”')
            return rewritten or question
        except Exception as exc:  # noqa: BLE001
            logger.warning("问题改写失败, 使用原问题: %s", exc)
            return self._heuristic_condense(question, history)

    @staticmethod
    def _heuristic_condense(question: str, history: List[Dict[str, str]]) -> str:
        """离线指代消解: 短问题/含指示代词时拼接上一轮用户问题"""
        prev_qs = [m["content"] for m in history if m["role"] == "user"]
        if prev_qs and (len(question) <= 12 or _PRONOUNS.search(question)):
            return f"{prev_qs[-1]} {question}"
        return question

    # ------------------------------------------------------------------
    # 2) 相关性精筛(防幻觉·第一道闸)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_yes_no(text: str) -> Optional[bool]:
        """鲁棒解析判定模型的 yes/no 输出(兼容中英文与带前缀的回答)"""
        t = text.strip().lower()
        m = re.search(r"\b(yes|no)\b", t)
        if m:
            return m.group(1) == "yes"
        zh = re.search(r"不相关|无关|不是|否|相关|是", t)
        if zh:
            return zh.group(0) in ("相关", "是")
        return None

    async def grade_relevance(
        self, question: str, chunk_text: str, score: float
    ) -> bool:
        overlap = keyword_overlap(question, chunk_text)
        heuristic = overlap >= self.settings.keyword_overlap_min or score >= 0.60
        if self.is_mock:
            return heuristic
        try:
            answer = await self._acomplete(
                GRADE_SYSTEM,
                f"问题: {question}\n资料片段: {chunk_text[:600]}\n是否相关:",
            )
            parsed = self._parse_yes_no(answer)
            if parsed is None:
                logger.warning("相关性判定输出无法解析(%r), 回退启发式", answer[:50])
                return heuristic
            return parsed
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 相关性判定失败, 回退启发式: %s", exc)
            return heuristic

    # ------------------------------------------------------------------
    # 3) 受限生成(流式)
    # ------------------------------------------------------------------
    async def astream_answer(
        self,
        question: str,
        blocks: List[Dict[str, Any]],
        history: Optional[List[Dict[str, str]]] = None,
        retry_feedback: str = "",
    ) -> AsyncIterator[str]:
        """blocks: [{"index":1,"doc_name":..,"section_path":..,"text":..}, ...]"""
        if self.is_mock:
            for piece in self._extractive_answer(question, blocks):
                await asyncio.sleep(0.01)  # 模拟打字机节奏
                yield piece
            return

        context = "\n\n".join(
            f"[{b['index']}] 《{b['doc_name']}》 {b['section_path']}\n{b['text']}"
            for b in blocks
        )
        parts = [f"<参考资料>\n{context}\n</参考资料>\n\n问题: {question}"]
        if history:
            lines = "\n".join(
                f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:200]}"
                for m in history[-self.settings.history_window_messages :]
            )
            parts.append(f"\n<对话历史(仅作背景, 回答仍只依据参考资料)>\n{lines}\n</对话历史>")
        if retry_feedback:
            parts.append(
                f"\n注意, 上一次回答未通过一致性审核, 请修正后重新回答: {retry_feedback}"
            )
        parts.append("\n请回答(务必标注来源编号):")

        # 提示词热更新: 每次生成时动态读取, 空则回退内置默认(无需重启生效)
        from app.services.prompt_store import get_prompt_store
        system_prompt = get_prompt_store().get_generate() or GENERATE_SYSTEM

        stream = await self._client.chat.completions.create(
            model=self._current_model(),
            temperature=self._current_temperature(),
            max_tokens=self.settings.llm_max_tokens,
            stream=True,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "".join(parts)},
            ],
        )
        last_usage = None
        async for delta in stream:
            if delta.choices and delta.choices[0].delta.content:
                yield delta.choices[0].delta.content
            if getattr(delta, "usage", None):
                last_usage = delta.usage
        self._record_usage(last_usage)

    def _extractive_answer(
        self, question: str, blocks: List[Dict[str, Any]]
    ) -> List[str]:
        """离线抽取式回答: 语义排序摘录原文句子, 天然与说明书一致"""
        candidates: List[tuple] = []  # (block_idx, order, sentence, block_score)
        for b in blocks:
            for order, raw in enumerate(split_sentences(b["text"])):
                sent = re.sub(r"^\s*\d{1,2}[\.、\)]\s*", "", raw)   # 去源文编号
                sent = re.sub(r"^[\-\*]\s*", "", sent).strip()
                if len(sent) >= 10:
                    candidates.append((b["index"], order, sent, b.get("score", 0)))
        if not candidates:
            return ["根据现有产品说明书资料, 暂时无法回答该问题。"]

        q_vec = self.embedder.embed_query(question)
        s_vecs = self.embedder.embed_documents([c[2] for c in candidates])
        # 语义相似度为主, 叠加检索得分的小幅偏置, 优先高分块中的句子
        ranked = sorted(
            range(len(candidates)),
            key=lambda i: -(
                self.embedder.cosine_similarity(q_vec, s_vecs[i])
                + 0.2 * candidates[i][3]
            ),
        )[:5]
        picked = sorted(ranked, key=lambda i: (candidates[i][0], candidates[i][1]))

        lines = [f"根据《{blocks[0]['doc_name']}》等说明书资料, 为您整理如下:", ""]
        for n, i in enumerate(picked, 1):
            block_idx, _, sent, _ = candidates[i]
            lines.append(f"{n}. {sent} [{block_idx}]")
        lines.append("")
        lines.append("以上内容摘自产品说明书原文, 已标注来源编号, 可点击查看出处。")
        return [line + "\n" for line in lines]

    # ------------------------------------------------------------------
    # 4) 一致性校验(防幻觉·第二道闸)
    # ------------------------------------------------------------------
    async def verify_answer(
        self, answer: str, blocks: List[Dict[str, Any]]
    ) -> VerifyResult:
        if self.is_mock:
            return self._containment_verify(answer, blocks)
        context = "\n\n".join(
            (
                f"[{b['index']}] 来源: 《{b.get('doc_name', '')}》 {b.get('section_path', '')}"
                + (f" (第{b['page']}页)" if b.get("page", -1) >= 0 else "")
                + f"\n{b['text']}"
            )
            for b in blocks
        )
        try:
            raw = await self._acomplete(
                VERIFY_SYSTEM,
                f"<参考资料>\n{context}\n</参考资料>\n\n<回答>\n{answer}\n</回答>\n审核结果(JSON):",
                json_mode=True,
            )
            data = self._extract_json(raw)
            return VerifyResult(
                supported=bool(data.get("supported")),
                notes=str(data.get("notes", ""))[:500],
                unsupported=[str(x)[:200] for x in data.get("unsupported", [])],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 审核失败, 回退启发式校验: %s", exc)
            return self._containment_verify(answer, blocks)

    @staticmethod
    def _claim_supported(claim_norm: str, block_text: str) -> bool:
        """宽松一致性判定: 陈述的字符 bigram 在资料中的覆盖率。

        LLM 改写后的回答不会与原文逐字相同, 但命令/参数/数值等关键信息
        会保留, 覆盖率仍高; 编造内容(幻觉)的覆盖率显著偏低。
        """
        claim_bg = char_bigrams(claim_norm)
        if len(claim_bg) < 2:
            return True  # 过短无法判定, 不拦截(避免误伤寒暄类语句)
        text_bg = char_bigrams(block_text)
        covered = len(claim_bg & text_bg) / len(claim_bg)
        return covered >= 0.45

    @staticmethod
    def _containment_verify(answer: str, blocks: List[Dict[str, Any]]) -> VerifyResult:
        """离线校验: 带来源编号的陈述必须能在对应资料中找到依据。

        通过条件(满足其一):
          a) 陈述(归一化后)是所引资料的逐字子串 —— 抽取式回答, 最强保证
          b) 陈述与所引资料的 bigram 覆盖率 ≥ 0.45 —— 改写式回答
        """
        by_index = {b["index"]: b for b in blocks}
        unsupported: List[str] = []
        checked = 0
        for line in answer.splitlines():
            line = line.strip()
            if not line or not _CITE_MARK.search(line):
                continue
            checked += 1
            cites = {int(m) for m in _CITE_MARK.findall(line)}
            claim = _CITE_MARK.sub("", line).strip()
            claim = re.sub(r"^[\-\*]\s*", "", claim)
            for _ in range(2):  # 去掉回答序号与原文序号(最多两层)
                claim = re.sub(r"^\d{1,2}[\.、\)]\s*", "", claim)
            claim = _normalize(claim)
            if not claim:
                continue
            ok = False
            for i in cites:
                block = by_index.get(i)
                if not block:
                    continue
                text_norm = _normalize(block["text"])
                haystack = (
                    f"{block.get('doc_name', '')} {block.get('section_path', '')} {block['text']}"
                )
                if claim[:80] in text_norm:  # a) 逐字包含
                    ok = True
                    break
                if LLMService._claim_supported(claim, haystack):  # b) 覆盖率(含元数据)
                    ok = True
                    break
            if not ok:
                unsupported.append(line[:120])
        if checked == 0:
            return VerifyResult(
                False, notes="回答中没有任何带来源编号的事实性陈述", unsupported=[]
            )
        if unsupported:
            return VerifyResult(
                False,
                notes=f"{len(unsupported)} 条陈述未在资料中找到依据",
                unsupported=unsupported,
            )
        return VerifyResult(True, notes="全部陈述均可追溯到资料")


# ------------------------------------------------------------------
    # 5) 追问/推荐问题
    # ------------------------------------------------------------------
    async def generate_followups(
        self,
        question: str,
        citations: Sequence[Dict[str, Any]],
    ) -> List[str]:
        """基于回答引用的资料, 生成若干可点击的追问问题。

        生产模式调用 LLM 生成; Mock 模式离线启发式(从引用文档与关键词派生),
        保证无 API Key 时也能展示可用的"推荐问题"。
        """
        count = self.settings.followup_count
        doc_names = [
            c.get("doc_name", "").strip()
            for c in (citations or [])
            if c.get("doc_name", "").strip()
        ]
        topics = [d for d in doc_names if len(d) >= 2][:2]

        if not self.is_mock:
            try:
                lines = "\n".join(
                    f"- 《{d}》" for d in doc_names[:6]
                ) or "(无引用)"
                raw = await self._acomplete(
                    FOLLOWUP_SYSTEM,
                    f"用户问题: {question}\n回答所引资料文档:\n{lines}\n"
                    f"推荐" + f"{count}个追问(JSON):",
                    json_mode=True,
                )
                data = self._extract_json(raw)
                items = [str(x).strip() for x in data.get("questions", [])]
                items = [x for x in items if x][:count]
                if items:
                    return items
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM 追问生成失败, 回退启发式: %s", exc)

        # 启发式兜底: 从引用文档名派生, 保证数量与稳定性
        kw = _normalize(question)
        seen = set()
        picks: List[str] = []
        for d in topics:
            if len(d) < 4:
                continue
            base = d.replace("手册", "").replace("指南", "").replace(" - ", " ")
            if base in seen:
                continue
            seen.add(base)
            picks.append(f"《{d}》里如何处理常见故障？")
            picks.append(f"{base} 的核心配置项分别有什么作用？")
        generic = [
            "上述操作如果失败，排查步骤是什么？",
            "该场景下有哪些注意事项或约束条件？",
        ]
        if kw and "步骤" not in question and "如何" not in question:
            generic.insert(0, "能否给出具体的操作步骤？")
        for g in generic:
            if g in seen:
                continue
            seen.add(g)
            picks.append(g)
        return picks[:count]


@lru_cache
def get_llm_service() -> LLMService:
    return LLMService()
