from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    postgres_user: str = Field(default="conceptgraph", alias="POSTGRES_USER")
    postgres_password: str = Field(default="conceptgraph_password", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="conceptgraph", alias="POSTGRES_DB")
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    database_pool_size: int = Field(default=3, ge=1, le=10, alias="DATABASE_POOL_SIZE")
    database_max_overflow: int = Field(
        default=2,
        ge=0,
        le=10,
        alias="DATABASE_MAX_OVERFLOW",
    )

    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_username: str = Field(default="neo4j", alias="NEO4J_USERNAME")
    neo4j_password: str = Field(default="conceptgraph_password", alias="NEO4J_PASSWORD")

    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    qdrant_collection_name: str = Field(
        default="conceptgraph_chunks",
        alias="QDRANT_COLLECTION_NAME",
    )
    qdrant_api_key: SecretStr | None = Field(default=None, alias="QDRANT_API_KEY")
    cors_allowed_origins: str = Field(
        default="",
        validation_alias=AliasChoices("ALLOWED_ORIGINS", "CORS_ALLOWED_ORIGINS"),
    )
    demo_access_token: SecretStr | None = Field(default=None, alias="DEMO_ACCESS_TOKEN")
    auth_cookie_name: str = Field(default="conceptgraph_access", alias="AUTH_COOKIE_NAME")
    auth_cookie_secure: bool = Field(default=False, alias="AUTH_COOKIE_SECURE")
    auth_cookie_samesite: str = Field(default="lax", alias="AUTH_COOKIE_SAMESITE")
    auth_session_ttl_seconds: int = Field(
        default=12 * 60 * 60,
        ge=300,
        le=7 * 24 * 60 * 60,
        alias="AUTH_SESSION_TTL_SECONDS",
    )
    rate_limit_requests_per_minute: int = Field(
        default=300,
        ge=1,
        alias="RATE_LIMIT_REQUESTS_PER_MINUTE",
    )
    rate_limit_expensive_per_minute: int = Field(
        default=30,
        ge=1,
        alias="RATE_LIMIT_EXPENSIVE_PER_MINUTE",
    )
    rate_limit_login_per_minute: int = Field(
        default=10,
        ge=1,
        alias="RATE_LIMIT_LOGIN_PER_MINUTE",
    )
    processing_concurrency: int = Field(
        default=1,
        ge=1,
        le=4,
        alias="PROCESSING_CONCURRENCY",
    )
    processing_queue_capacity: int = Field(
        default=8,
        ge=1,
        le=100,
        alias="PROCESSING_QUEUE_CAPACITY",
    )
    processing_lease_seconds: int = Field(
        default=180,
        ge=60,
        le=3600,
        alias="PROCESSING_LEASE_SECONDS",
    )
    processing_heartbeat_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        alias="PROCESSING_HEARTBEAT_SECONDS",
    )
    processing_dispatch_interval_seconds: float = Field(
        default=2.0,
        ge=0.25,
        le=30,
        alias="PROCESSING_DISPATCH_INTERVAL_SECONDS",
    )
    max_pdf_size_mb: int = Field(default=10, ge=1, le=100, alias="MAX_PDF_SIZE_MB")
    max_pdfs_per_installation: int = Field(
        default=50,
        ge=1,
        le=10_000,
        alias="MAX_PDFS_PER_INSTALLATION",
    )
    require_upload_auth: bool = Field(
        default=False,
        alias="REQUIRE_UPLOAD_AUTH",
    )
    public_sample_course_id: str = Field(
        default="",
        alias="PUBLIC_SAMPLE_COURSE_ID",
    )
    demo_upload_retention_days: int = Field(
        default=3,
        ge=1,
        le=30,
        alias="DEMO_UPLOAD_RETENTION_DAYS",
    )
    demo_cleanup_interval_seconds: int = Field(
        default=6 * 60 * 60,
        ge=60,
        le=24 * 60 * 60,
        alias="DEMO_CLEANUP_INTERVAL_SECONDS",
    )
    strict_startup_validation: bool = Field(
        default=False,
        alias="STRICT_STARTUP_VALIDATION",
    )

    object_storage_backend: str = Field(default="s3", alias="OBJECT_STORAGE_BACKEND")
    s3_bucket: str = Field(default="conceptgraph-pdfs", alias="S3_BUCKET")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_endpoint_url: str | None = Field(
        default="http://localhost:9000",
        alias="S3_ENDPOINT_URL",
    )
    s3_access_key_id: str | None = Field(
        default="conceptgraph",
        alias="S3_ACCESS_KEY_ID",
    )
    s3_secret_access_key: str | None = Field(
        default="conceptgraph_local_only",
        alias="S3_SECRET_ACCESS_KEY",
    )
    s3_force_path_style: bool = Field(default=True, alias="S3_FORCE_PATH_STYLE")
    s3_auto_create_bucket: bool = Field(default=True, alias="S3_AUTO_CREATE_BUCKET")
    s3_server_side_encryption: str | None = Field(
        default=None,
        alias="S3_SERVER_SIDE_ENCRYPTION",
    )
    legacy_upload_dir: Path = Field(default=Path("data/uploads"), alias="LEGACY_UPLOAD_DIR")

    embedding_model_name: str = Field(
        default="all-MiniLM-L6-v2",
        alias="EMBEDDING_MODEL_NAME",
    )
    embedding_provider: str = Field(default="local", alias="EMBEDDING_PROVIDER")
    embedding_dimension: int = Field(
        default=384,
        ge=1,
        alias="EMBEDDING_DIMENSION",
    )
    rerank_provider: str = Field(default="local", alias="RERANK_PROVIDER")
    rerank_model_name: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L6-v2",
        alias="RERANK_MODEL_NAME",
    )
    cohere_api_key: SecretStr | None = Field(default=None, alias="COHERE_API_KEY")
    provider_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        alias="PROVIDER_TIMEOUT_SECONDS",
    )

    llm_provider: str = Field(default="groq", alias="LLM_PROVIDER")
    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    groq_model: str = Field(default="openai/gpt-oss-20b", alias="GROQ_MODEL")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-1.5-flash", alias="GEMINI_MODEL")
    evidence_min_score: float = Field(default=0.35, ge=0, le=1, alias="EVIDENCE_MIN_SCORE")
    evidence_medium_score: float = Field(default=0.5, ge=0, le=1, alias="EVIDENCE_MEDIUM_SCORE")
    evidence_high_score: float = Field(default=0.7, ge=0, le=1, alias="EVIDENCE_HIGH_SCORE")

    @model_validator(mode="after")
    def validate_evidence_thresholds(self) -> "Settings":
        if not (
            self.evidence_min_score
            <= self.evidence_medium_score
            <= self.evidence_high_score
        ):
            raise ValueError(
                "Evidence thresholds must satisfy min <= medium <= high."
            )
        return self

    @model_validator(mode="after")
    def validate_storage(self) -> "Settings":
        backend = self.object_storage_backend.strip().lower()
        if backend not in {"s3", "local"}:
            raise ValueError("OBJECT_STORAGE_BACKEND must be either 's3' or 'local'.")
        self.object_storage_backend = backend
        if self.s3_endpoint_url is not None:
            self.s3_endpoint_url = self.s3_endpoint_url.strip() or None
        if backend == "s3" and not self.s3_bucket.strip():
            raise ValueError("S3_BUCKET is required when object storage is enabled.")
        return self

    @model_validator(mode="after")
    def validate_demo_security(self) -> "Settings":
        self.auth_cookie_samesite = self.auth_cookie_samesite.strip().lower()
        self.public_sample_course_id = self.public_sample_course_id.strip()
        if self.auth_cookie_samesite not in {"lax", "strict", "none"}:
            raise ValueError("AUTH_COOKIE_SAMESITE must be lax, strict, or none.")
        if self.auth_cookie_samesite == "none" and not self.auth_cookie_secure:
            raise ValueError(
                "AUTH_COOKIE_SECURE must be true when AUTH_COOKIE_SAMESITE=none."
            )
        if not self.auth_cookie_name.strip():
            raise ValueError("AUTH_COOKIE_NAME cannot be empty.")
        token = self.demo_access_token_value
        if token is not None and len(token) < 24:
            raise ValueError("DEMO_ACCESS_TOKEN must contain at least 24 characters.")
        if self.require_upload_auth and token is None:
            raise ValueError(
                "DEMO_ACCESS_TOKEN is required when REQUIRE_UPLOAD_AUTH=true."
            )
        return self

    @model_validator(mode="after")
    def validate_processing_timing(self) -> "Settings":
        if self.processing_heartbeat_seconds * 2 >= self.processing_lease_seconds:
            raise ValueError(
                "PROCESSING_LEASE_SECONDS must be more than twice "
                "PROCESSING_HEARTBEAT_SECONDS."
            )
        return self

    @model_validator(mode="after")
    def validate_retrieval_providers(self) -> "Settings":
        self.embedding_provider = self.embedding_provider.strip().lower()
        self.rerank_provider = self.rerank_provider.strip().lower()
        if self.embedding_provider not in {"local", "qdrant_cloud"}:
            raise ValueError(
                "EMBEDDING_PROVIDER must be either local or qdrant_cloud."
            )
        if self.rerank_provider not in {"local", "cohere"}:
            raise ValueError("RERANK_PROVIDER must be either local or cohere.")
        if not self.embedding_model_name.strip():
            raise ValueError("EMBEDDING_MODEL_NAME cannot be empty.")
        if not self.rerank_model_name.strip():
            raise ValueError("RERANK_MODEL_NAME cannot be empty.")
        if self.rerank_provider == "cohere" and not self.cohere_api_key_value:
            raise ValueError("COHERE_API_KEY is required when RERANK_PROVIDER=cohere.")
        return self

    @model_validator(mode="after")
    def validate_public_deployment(self) -> "Settings":
        if not self.strict_startup_validation:
            return self
        missing: list[str] = []
        if not self.database_url:
            missing.append("DATABASE_URL")
        required_explicit = {
            "neo4j_uri": "NEO4J_URI",
            "neo4j_username": "NEO4J_USERNAME",
            "neo4j_password": "NEO4J_PASSWORD",
            "qdrant_url": "QDRANT_URL",
            "s3_bucket": "S3_BUCKET",
            "s3_access_key_id": "S3_ACCESS_KEY_ID",
            "s3_secret_access_key": "S3_SECRET_ACCESS_KEY",
        }
        for field_name, environment_name in required_explicit.items():
            if field_name not in self.model_fields_set:
                missing.append(environment_name)
        if not self.qdrant_api_key_value:
            missing.append("QDRANT_API_KEY")
        if self.embedding_provider != "qdrant_cloud":
            missing.append("EMBEDDING_PROVIDER=qdrant_cloud")
        if self.rerank_provider != "cohere":
            missing.append("RERANK_PROVIDER=cohere")
        if not self.configured_cors_origins:
            missing.append("ALLOWED_ORIGINS")
        if not self.demo_access_token_value:
            missing.append("DEMO_ACCESS_TOKEN")
        if not self.require_upload_auth:
            missing.append("REQUIRE_UPLOAD_AUTH=true")
        if not self.public_sample_course_id:
            missing.append("PUBLIC_SAMPLE_COURSE_ID")
        if self.object_storage_backend == "s3" and not self.s3_endpoint_url:
            missing.append("S3_ENDPOINT_URL")
        provider = self.llm_provider.strip().lower()
        if provider == "groq" and not self.groq_api_key:
            missing.append("GROQ_API_KEY")
        elif provider == "gemini" and not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        elif provider not in {"groq", "gemini"}:
            raise ValueError("LLM_PROVIDER must be either groq or gemini.")
        if missing:
            raise ValueError(
                "Missing required public-deployment environment variables: "
                + ", ".join(sorted(set(missing)))
            )
        return self

    @property
    def postgres_dsn(self) -> str:
        if self.database_url:
            if self.database_url.startswith("postgresql+asyncpg://"):
                dsn = self.database_url
            elif self.database_url.startswith("postgresql://"):
                dsn = self.database_url.replace(
                    "postgresql://",
                    "postgresql+asyncpg://",
                    1,
                )
            elif self.database_url.startswith("postgres://"):
                dsn = self.database_url.replace(
                    "postgres://",
                    "postgresql+asyncpg://",
                    1,
                )
            else:
                raise ValueError("DATABASE_URL must use a PostgreSQL URL.")

            # Managed PostgreSQL providers commonly emit libpq parameters.
            # asyncpg accepts `ssl` instead of `sslmode` and does not accept
            # libpq's `channel_binding` parameter.
            parsed = urlsplit(dsn)
            query = parse_qsl(parsed.query, keep_blank_values=True)
            has_ssl = any(key == "ssl" for key, _ in query)
            normalized_query: list[tuple[str, str]] = []
            for key, value in query:
                if key == "channel_binding":
                    continue
                if key == "sslmode":
                    if not has_ssl:
                        normalized_query.append(("ssl", value))
                    continue
                normalized_query.append((key, value))
            return urlunsplit(parsed._replace(query=urlencode(normalized_query)))
        return (
            "postgresql+asyncpg://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def configured_cors_origins(self) -> list[str]:
        return [
            origin.strip().rstrip("/")
            for origin in self.cors_allowed_origins.split(",")
            if origin.strip()
        ]

    @property
    def qdrant_api_key_value(self) -> str | None:
        if self.qdrant_api_key is None:
            return None
        return self.qdrant_api_key.get_secret_value().strip() or None

    @property
    def demo_access_token_value(self) -> str | None:
        if self.demo_access_token is None:
            return None
        return self.demo_access_token.get_secret_value().strip() or None

    @property
    def cohere_api_key_value(self) -> str | None:
        if self.cohere_api_key is None:
            return None
        return self.cohere_api_key.get_secret_value().strip() or None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
