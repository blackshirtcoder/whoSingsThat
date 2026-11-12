"""FastAPI server package for Who Sings That?."""

from .app import app, create_app
from .config import ServerSettings

__all__ = ["ServerSettings", "app", "create_app"]
