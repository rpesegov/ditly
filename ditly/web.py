"""Страницы сервиса и переход по короткой ссылке."""

from __future__ import annotations

import hmac
import re
import secrets
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import access, clicks, db, identity, links
from .config import Settings
from .errors import UserError

_VISITOR_COOKIE = "ditly_visitor"
_VISITOR_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
_ROOT = Path(__file__).resolve().parent


def create_app(settings: Settings) -> Flask:
    db.configure(settings)
    db.init_db()
    app = Flask(
        __name__,
        template_folder=str(_ROOT / "templates"),
        static_folder=str(_ROOT / "static"),
    )
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = settings.base_url.startswith("https://")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    _register(app)
    return app


def short_url(code: str) -> str:
    return f"{db.get_settings().base_url}/{code}"


def _register(app: Flask) -> None:
    @app.context_processor
    def inject():
        user_id = identity.current_user_id()
        return {
            "csrf_token": session.get("csrf_token", ""),
            "current_login": identity.login_name(user_id) if user_id else None,
            "base_url": db.get_settings().base_url,
            "role_labels": access.ROLE_LABELS,
            "role_options": [(key, access.ROLE_LABELS[key]) for key in access.ROLES],
            "can": access.enough,
        }

    @app.before_request
    def guard_session():
        if request.endpoint in {None, "static", "follow"}:
            return
        if request.endpoint not in {"login_form", "login_submit"} and identity.current_user_id() is None:
            return redirect(url_for("login_form", next=request.path))
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        if request.method == "POST" and not _csrf_ok():
            abort(400)

    @app.get("/")
    def home():
        if identity.current_user_id() is None:
            return redirect(url_for("login_form"))
        return redirect(url_for("project_list"))

    @app.get("/login")
    def login_form():
        if identity.current_user_id():
            return redirect(url_for("project_list"))
        return render_template("login.html", next_url=_safe_next(request.args.get("next")))

    @app.post("/login")
    def login_submit():
        user_id = identity.login(
            request.form.get("login", ""),
            request.form.get("password", ""),
        )
        if user_id is None:
            return (
                render_template(
                    "login.html",
                    error="Неверный логин или пароль.",
                    login_value=request.form.get("login", ""),
                    next_url=_safe_next(request.form.get("next")),
                ),
                401,
            )
        return redirect(_safe_next(request.form.get("next")))

    @app.post("/logout")
    def logout():
        identity.logout()
        return redirect(url_for("login_form"))

    @app.get("/users/new")
    def user_new():
        _user()
        return render_template("user_form.html")

    @app.post("/users/new")
    def user_create():
        _user()
        login_value = request.form.get("login", "")
        password = request.form.get("password", "")
        again = request.form.get("password_again", "")
        if password != again:
            return (
                render_template(
                    "user_form.html",
                    error="Пароли не совпадают.",
                    login_value=login_value,
                ),
                400,
            )
        try:
            identity.create_user(login_value, password)
        except UserError as exc:
            return (
                render_template(
                    "user_form.html",
                    error=exc.message,
                    login_value=login_value,
                ),
                400,
            )
        flash("Учётка создана. Чужие проекты она не видит, пока ей не откроют доступ.")
        return redirect(url_for("project_list"))

    @app.get("/projects")
    def project_list():
        user_id = _user()
        return render_template("projects.html", projects=access.list_projects(user_id))

    @app.get("/projects/new")
    def project_new():
        _user()
        return render_template("project_form.html")

    @app.post("/projects/new")
    def project_create():
        user_id = _user()
        name = request.form.get("name", "")
        try:
            project_id = access.create_project(user_id, name)
        except UserError as exc:
            return render_template("project_form.html", error=exc.message, name=name), 400
        return redirect(url_for("project_detail", project_id=project_id))

    @app.get("/projects/<project_id>")
    def project_detail(project_id: str):
        project, role = _guard(project_id, "view")
        members = []
        if access.enough(role, "manage"):
            members = _members(project_id)
        return render_template(
            "project.html",
            project=project,
            role=role,
            links=links.list_links(project_id),
            members=members,
            short_url=short_url,
        )

    @app.post("/projects/<project_id>/rename")
    def project_rename(project_id: str):
        _guard(project_id, "manage")
        try:
            access.rename_project(project_id, request.form.get("name", ""))
        except UserError as exc:
            flash(exc.message)
        return redirect(url_for("project_detail", project_id=project_id))

    @app.post("/projects/<project_id>/delete")
    def project_delete(project_id: str):
        _guard(project_id, "manage")
        access.delete_project(project_id)
        flash("Проект удалён.")
        return redirect(url_for("project_list"))

    @app.get("/projects/<project_id>/links/new")
    def link_new(project_id: str):
        project, role = _guard(project_id, "edit")
        return render_template("link_form.html", project=project, role=role)

    @app.post("/projects/<project_id>/links/new")
    def link_create(project_id: str):
        user_id = identity.current_user_id()
        project, role = _guard(project_id, "edit")
        label = request.form.get("label", "")
        url = request.form.get("url", "")
        try:
            created = links.create_link(project_id, user_id or "", label, url)
        except UserError as exc:
            return (
                render_template(
                    "link_form.html",
                    project=project,
                    role=role,
                    error=exc.message,
                    label=label,
                    url=url,
                ),
                400,
            )
        flash(f"Ссылка создана: {short_url(created['code'])}")
        return redirect(url_for("project_detail", project_id=project_id))

    @app.get("/projects/<project_id>/links/<link_id>")
    def link_stats(project_id: str, link_id: str):
        project, role = _guard(project_id, "view")
        link = links.get_link(project_id, link_id)
        if link is None:
            abort(404)
        include_bots = request.args.get("bots") == "1"
        return render_template(
            "stats.html",
            project=project,
            role=role,
            link=link,
            short=short_url(link["code"]),
            stats=clicks.stats_for(link_id, include_bots=include_bots),
        )

    @app.get("/projects/<project_id>/links/<link_id>/edit")
    def link_edit(project_id: str, link_id: str):
        project, role = _guard(project_id, "edit")
        link = links.get_link(project_id, link_id)
        if link is None:
            abort(404)
        return render_template(
            "link_form.html",
            project=project,
            role=role,
            link=link,
            label=link["label"],
            url=link["url"],
            enabled=bool(link["enabled"]),
        )

    @app.post("/projects/<project_id>/links/<link_id>/edit")
    def link_update(project_id: str, link_id: str):
        project, role = _guard(project_id, "edit")
        link = links.get_link(project_id, link_id)
        if link is None:
            abort(404)
        label = request.form.get("label", "")
        url = request.form.get("url", "")
        enabled = request.form.get("enabled") == "1"
        try:
            links.update_link(
                project_id,
                link_id,
                label=label,
                url=url,
                enabled=enabled,
            )
        except UserError as exc:
            return (
                render_template(
                    "link_form.html",
                    project=project,
                    role=role,
                    link=link,
                    error=exc.message,
                    label=label,
                    url=url,
                    enabled=enabled,
                ),
                400,
            )
        flash("Ссылка сохранена.")
        return redirect(url_for("project_detail", project_id=project_id))

    @app.post("/projects/<project_id>/links/<link_id>/delete")
    def link_delete(project_id: str, link_id: str):
        _guard(project_id, "edit")
        try:
            links.delete_link(project_id, link_id)
        except UserError as exc:
            flash(exc.message)
        else:
            flash("Ссылка удалена.")
        return redirect(url_for("project_detail", project_id=project_id))

    @app.post("/projects/<project_id>/members")
    def member_set(project_id: str):
        _guard(project_id, "manage")
        role = request.form.get("role", "")
        user_id = request.form.get("user_id", "").strip()
        if not user_id:
            user_id = identity.find_user_id(request.form.get("login", "")) or ""
            if not user_id:
                flash(
                    "Учётки с таким логином нет. Сначала создайте её: "
                    "новая учётка сама по себе проект не видит."
                )
                return redirect(url_for("project_detail", project_id=project_id))
        try:
            access.set_member(project_id, user_id, role)
        except UserError as exc:
            flash(exc.message)
        else:
            flash("Доступ сохранён.")
        return redirect(url_for("project_detail", project_id=project_id))

    @app.post("/projects/<project_id>/members/remove")
    def member_remove(project_id: str):
        _guard(project_id, "manage")
        try:
            access.remove_member(project_id, request.form.get("user_id", "").strip())
        except UserError as exc:
            flash(exc.message)
        else:
            flash("Доступ убран.")
        return redirect(url_for("project_detail", project_id=project_id))

    @app.get("/<code>")
    def follow(code: str):
        link = links.find_enabled_by_code(code)
        if link is None:
            return render_template("missing.html"), 404
        visitor_key = _visitor_key()
        is_bot, device = clicks.classify_user_agent(request.headers.get("User-Agent"))
        clicks.record_click(
            link["id"],
            is_bot=is_bot,
            referrer=clicks.clean_referrer(request.headers.get("Referer")),
            device=device,
            country=clicks.country_from_ip(
                _client_ip(),
                db.get_settings().geoip_db,
            ),
            visitor_key=visitor_key,
        )
        response = redirect(link["url"], code=302)
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            _VISITOR_COOKIE,
            visitor_key,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="Lax",
            secure=db.get_settings().base_url.startswith("https://"),
            path="/",
        )
        return response

    @app.errorhandler(400)
    def bad_request(_err):
        return (
            render_template(
                "error.html",
                title="Форма устарела",
                message="Обновите страницу и отправьте форму ещё раз.",
            ),
            400,
        )

    @app.errorhandler(403)
    def forbidden(_err):
        return (
            render_template(
                "error.html",
                title="Недостаточно прав",
                message="Этого действия у вашей роли нет.",
            ),
            403,
        )

    @app.errorhandler(404)
    def not_found(_err):
        return (
            render_template(
                "error.html",
                title="Страница не найдена",
                message="Такой страницы нет.",
            ),
            404,
        )


def _user() -> str:
    user_id = identity.current_user_id()
    if user_id is None:
        abort(401)
    return user_id


def _guard(project_id: str, min_role: str) -> tuple[dict, str]:
    user_id = _user()
    project = access.get_project(project_id)
    role = access.get_role(user_id, project_id)
    if project is None or role is None:
        abort(404)
    if not access.enough(role, min_role):
        abort(403)
    return project, role


def _members(project_id: str) -> list[dict]:
    members = access.list_members(project_id)
    for member in members:
        member["login"] = identity.login_name(member["user_id"]) or "учётка без имени"
    members.sort(key=lambda item: (-access.ROLES.index(item["role"]), item["login"].casefold()))
    return members


def _csrf_ok() -> bool:
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not isinstance(sent, str) or not isinstance(expected, str):
        return False
    if not sent or not expected or len(sent) != len(expected):
        return False
    return hmac.compare_digest(sent, expected)


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return url_for("project_list")
    return value


def _visitor_key() -> str:
    raw = request.cookies.get(_VISITOR_COOKIE, "")
    if _VISITOR_RE.fullmatch(raw):
        return raw
    return secrets.token_urlsafe(24)


def _client_ip() -> str | None:
    if db.get_settings().trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    return request.remote_addr
