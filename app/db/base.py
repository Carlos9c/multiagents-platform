from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from app.models import project  # noqa
from app.models import task  # noqa
from app.models import execution_run  # noqa
from app.models import artifact  # noqa
from app.models import conversation  # noqa
from app.models import supervisor_report  # noqa
from app.models import agent_evaluation  # noqa
from app.models import aggregate_report  # noqa
