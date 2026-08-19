"""防幻觉机制单元测试: 一致性校验 / 相关性判定 / 抽取式生成"""

from app.core.llm import LLMService, _CITE_MARK, keyword_overlap

BLOCKS = [
    {
        "index": 1,
        "doc_name": "说明书A",
        "section_path": "3 安装部署",
        "score": 0.7,
        "text": "系统默认管理员账号为 admin，初始密码 Admin@2024。首次登录时系统强制要求修改初始密码。",
    },
    {
        "index": 2,
        "doc_name": "说明书B",
        "section_path": "6 维保服务",
        "score": 0.6,
        "text": "服务热线 400-800-1234，7×24 小时。Critical 级别 15 分钟内远程响应。",
    },
]


def test_verify_supported_answer():
    answer = "1. 系统默认管理员账号为 admin，初始密码 Admin@2024。 [1]"
    r = LLMService._containment_verify(answer, BLOCKS)
    assert r.supported, r.notes


def test_verify_catches_hallucination():
    fabricated = "1. 初始密码是 P@ssw0rd123，且支持指纹登录。 [1]"
    r = LLMService._containment_verify(fabricated, BLOCKS)
    assert not r.supported
    assert r.unsupported


def test_verify_requires_citation():
    no_cite = "系统默认管理员账号为 admin。"
    r = LLMService._containment_verify(no_cite, BLOCKS)
    assert not r.supported
    assert "来源编号" in r.notes


def test_verify_wrong_source_index():
    mis_cited = "1. 服务热线 400-800-1234。 [1]"  # 内容在块2, 却引块1
    r = LLMService._containment_verify(mis_cited, BLOCKS)
    assert not r.supported


def test_keyword_overlap():
    assert keyword_overlap("默认管理员账号", "系统默认管理员账号为 admin") > 0.3
    assert keyword_overlap("春天写诗", "系统默认管理员账号为 admin") < 0.1


def test_extractive_answer_citations():
    svc = LLMService()
    pieces = svc._extractive_answer("默认管理员账号和初始密码", BLOCKS)
    answer = "".join(pieces)
    assert _CITE_MARK.search(answer)
    assert "admin" in answer
