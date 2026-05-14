import logging

logger = logging.getLogger("agent.retrieval")


class RetrievalManager:
    def __init__(self, base_url: str = "", token: str = ""):
        self.base_url = base_url
        self.token = token

    def configure(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        logger.info(f"RAG 知识库已配置: {self.base_url}")

    def is_configured(self) -> bool:
        return bool(self.base_url)
