from dataclasses import dataclass
import hashlib

@dataclass(frozen=True)
class Chunk:
    path: str
    lang: str
    symbol_fqn: str          # напр. "UserService.create"
    kind: str                # 'function' | 'class' | 'method' | 'module'
    start_line: int          # 1-based, включает декораторы
    end_line: int
    text: str

    @property
    def node_id(self) -> str:
        return f"{self.path}#{self.symbol_fqn}"   # ЕДИНЫЙ ключ чанка и узла графа

    @property
    def content_hash(self) -> str:
        norm = "\n".join(line.rstrip() for line in self.text.splitlines()).strip()
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()
