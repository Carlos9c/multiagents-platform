from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from app.models import project  # noqa
from app.models import task  # noqa
from app.models import execution_run  # noqa
from app.models import artifact  # noqa