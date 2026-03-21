from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "agente_desarrollador",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.task_track_started = True

# Import explícito de tareas
import app.workers.tasks  # noqa: E402,F401