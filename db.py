from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
from sqlalchemy import BigInteger


class Base(DeclarativeBase):
    pass


class ChatState(Base):
    __tablename__ = 'chat_state'
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_url: Mapped[str] = mapped_column(nullable=True)
    check_interval: Mapped[int] = mapped_column(nullable=True)

    def __repr__(self):
        return f"ChatState(chat_id={self.chat_id}, repo_url={self.repo_url}, check_interval={self.check_interval})"
