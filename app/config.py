"""全局配置管理(支持 .env 与环境变量覆盖,前缀 RAG_)"""

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """系统全局配置。所有字段均可通过环境变量 RAG_<字段名大写> 覆盖。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="RAG_", extra="ignore"
    )

    # ---------- 应用 ----------
    app_name: str = "RAG 智能助手"
    app_version: str = "0.1.0"
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: List[str] = ["*"]

    # ---------- 存储路径 ----------
    data_dir: str = "./data"
    manuals_dir: str = "./data/manuals"          # 产品说明书目录(批量导入)
    uploads_dir: str = "./data/uploads"          # API 上传文件落地目录
    upload_max_mb: int = 20                      # 单文件上传大小上限(MB), 超限前端/后端拦截
    history_db_path: str = "./data/history.db"   # 对话历史 SQLite

    # ---------- Milvus ----------
    # 开发/单机: "./data/milvus_lite.db" (milvus-lite 嵌入式)
    # 生产/服务: "http://localhost:19530" (docker-compose 启动的 Milvus)
    milvus_uri: str = "./data/milvus_lite.db"
    milvus_collection: str = "manual_chunks"
    milvus_user: str = ""
    milvus_password: str = ""

    # ---------- 向量化 ----------
    # provider: fastembed(本地 ONNX, 离线) | openai(兼容 API)
    embedding_provider: str = "fastembed"
    fastembed_model: str = "BAAI/bge-small-zh-v1.5"
    hf_endpoint: str = "https://hf-mirror.com"   # 模型下载镜像(huggingface.co 不可达时)
    hf_disable_xet: bool = True                  # 禁用 hf-xet 下载协议(部分网络 401)
    embedding_api_base: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_batch_size: int = 32

    # ---------- LLM ----------
    # provider: mock(抽取式离线演示) | openai(OpenAI 兼容 API: GLM/GPT/Qwen...)
    llm_provider: str = "mock"
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_api_key: str = ""
    llm_model: str = "glm-4-flash"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 1024
    llm_timeout: float = 60.0

    # ---------- 检索与防幻觉 ----------
    retrieval_top_k: int = 12                    # Milvus 召回条数(去重后)
    retrieval_score_threshold: float = 0.42      # 向量相关性阈值(COSINE)
    keyword_overlap_min: float = 0.10            # 关键词覆盖率下限(双重过滤)
    max_context_chunks: int = 4                  # 进入生成阶段的最大片段数
    generation_max_retries: int = 1              # 一致性校验失败后的重试次数
    history_window_messages: int = 8             # 多轮对话携带的历史消息数

    # ---------- 混合检索(BM25 + RRF) ----------
    hybrid_bm25_k: int = 12                      # BM25 每路召回条数
    rrf_k: int = 60                              # RRF 融合常量
    rrf_vector_weight: float = 1.0               # 向量路权重
    rrf_bm25_weight: float = 1.0                 # BM25 路权重
    bm25_k1: float = 1.5                         # Okapi BM25 k1
    bm25_b: float = 0.75                         # Okapi BM25 b

    # ---------- Rerank(可选, 用 bge-reranker CrossEncoder, 未装时自动降级) ----------
    rerank_enabled: bool = False                 # 是否启用重排序
    rerank_model: str = "BAAI/bge-reranker-base" # 精排模型名
    rerank_top_k: int = 8                        # 参与精排的候选条数
    rerank_keep: int = 4                         # 精排后保留条数(<=max_context_chunks)
    rerank_threshold: float = 0.0                # 重排相关度阈值, 低于则丢弃(0 = 不过滤)

    # ---------- 文档版本管理 ----------
    doc_versions_db_path: str = "./data/versions.db"

    # ---------- 会话分享 / 知识库统计 ----------
    shares_db_path: str = "./data/shares.db"       # 分享链接映射
    doc_stats_db_path: str = "./data/doc_stats.db" # 每文档命中/覆盖统计
    followup_count: int = 3                         # 每次回答生成的追问数

    # ---------- 认证与多租户(RBAC) ----------
    # off : 演示模式(默认), 无认证, 隐式使用 default 租户, 行为与旧版本一致
    # on  : 生产模式, 启用 JWT 认证 / 角色权限 / 租户隔离
    auth_mode: str = "off"
    jwt_secret: str = ""                         # auth on 时必填, 用于 JWT 签名
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24
    auth_db_path: str = "./data/auth.db"
    default_tenant: str = "default"              # auth off 时的隐式租户名

    # ---------- 可观测性(全链路追踪) ----------
    trace_db_path: str = "./data/trace.db"       # 链路 span 落库(可选持久化)
    trace_ttl_seconds: int = 3600                # 内存 trace 保留时长

    # ---------- 后台运维(第四批 P2) ----------
    endpoint_metrics_ttl: int = 300              # 接口性能统计窗口(秒)
    audit_db_path: str = "./data/audit.db"       # 敏感操作审计日志
    llm_usage_db_path: str = "./data/llm_usage.db"  # LLM token 消耗统计
    prompt_store_path: str = "./data/prompts.json"  # 可热更新的 System Prompt
    # 知识库文档元数据(分类/权限), 与版本/审计一致走 SQLite, 零迁移
    doc_meta_db_path: str = "./data/kb_meta.db"
    # 知识库管理面板: 文档记录(状态/失败原因/展示名/版本号/源文件), 支撑 CRUD 状态机与前后端一致
    doc_records_db_path: str = "./data/kb_docs.db"
    # 资源水位监控(CPU/内存/磁盘, 24h 曲线)
    resource_db_path: str = "./data/resource.db"    # 资源采样 SQLite
    resource_sample_interval: int = 60              # 聚合采样间隔(秒)
    # 慢请求阈值(ms), 高于此值的全程请求进入慢请求明细
    slow_request_threshold_ms: int = 500

    # ---------- 评估 ----------
    eval_dataset_path: str = "./eval/dataset.jsonl"

    # ---------- 通知中心 + 工单系统 ----------
    notify_db_path: str = "./data/notify.db"       # 用户通知(SQLite, 保留策略自动清理)
    notify_retention_days: int = 90                # 通知自动保留天数, 超期清理
    ticket_db_path: str = "./data/tickets.db"      # 工单系统(SQLite)

    # ---------- 分块 ----------
    chunk_size: int = 480                         # 单块目标字符数(中文)
    chunk_overlap: int = 64                       # 相邻块重叠字符数

    # ---------- 启动行为 ----------
    auto_ingest_on_startup: bool = True           # 启动时自动导入 manuals_dir


@lru_cache
def get_settings() -> Settings:
    return Settings()
