"""Shared FastAPI dependency accessors for the API routers.

Every route reads its collaborators through these tiny dependencies rather
than importing managers or storage. They are transport wiring: the
application commands they hand out never see a request object.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy import Engine

from ompire_daemon.advisories import AdvisorySampler
from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.application.launch import LaunchService
from ompire_daemon.config import Config
from ompire_daemon.delivery import WorkspaceGuard
from ompire_daemon.events import EventHub
from ompire_daemon.gh import GitHubProbe
from ompire_daemon.gpg import GpgProbe
from ompire_daemon.notifications import AttentionNotifier
from ompire_daemon.registry.settings import SettingsStore
from ompire_daemon.result_exports import ResultExportManager
from ompire_daemon.results import ResultManager
from ompire_daemon.review import ReviewManager
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.ship import ShipManager
from ompire_daemon.work.setup import ProjectSetupManager


def _engine(request: Request) -> Engine:
    return request.app.state.engine


def _config(request: Request) -> Config:
    return request.app.state.config


def _settings(request: Request) -> SettingsStore:
    return request.app.state.settings_store


def _events(request: Request) -> EventHub:
    return request.app.state.events


def _sessions(request: Request) -> SessionTracker:
    return request.app.state.sessions


def _advisories(request: Request) -> AdvisorySampler:
    return request.app.state.advisories


def _notifications(request: Request) -> AttentionNotifier:
    return request.app.state.notifications


def _reviews(request: Request) -> ReviewManager:
    return request.app.state.reviews


def _ships(request: Request) -> ShipManager:
    return request.app.state.ships


def _guard(request: Request) -> WorkspaceGuard:
    return request.app.state.workspace_guard


def _gpg(request: Request) -> GpgProbe:
    return request.app.state.gpg


def _gh(request: Request) -> GitHubProbe:
    return request.app.state.gh


def _project_setup(request: Request) -> ProjectSetupManager:
    return request.app.state.project_setup


def _results(request: Request) -> ResultManager:
    return request.app.state.results


def _exports(request: Request) -> ResultExportManager:
    return request.app.state.result_exports


def _supervisor(request: Request) -> AgentSupervisor:
    return request.app.state.agents


def _launch_service(request: Request) -> LaunchService:
    return request.app.state.launch_service
