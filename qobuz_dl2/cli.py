import asyncio
import logging
import os
import sys
from typing import Any, Awaitable, Callable, Dict, Optional

import click

from qobuz_dl2 import config as config_store
from qobuz_dl2.bundle import Bundle
from qobuz_dl2.color import GREEN, RED, RESET, YELLOW
from qobuz_dl2.core import QobuzDL
from qobuz_dl2.downloader import DEFAULT_DOWNLOAD_LIMIT as DEFAULT_DOWNLOAD_LIMIT
from qobuz_dl2.downloader import DEFAULT_FOLDER as DEFAULT_FOLDER
from qobuz_dl2.downloader import DEFAULT_TRACK as DEFAULT_TRACK
from qobuz_dl2.exceptions import (
    AuthenticationError,
    IneligibleError,
    InvalidAppSecretError,
)
from qobuz_dl2.results import BatchResult

# Reset styling at the end of every line so colors don't bleed (colorama's old
# autoreset behavior, reproduced without the dependency).


if os.name == "nt":
    # Ensure a valid string path even if APPDATA is unset
    OS_CONFIG = os.environ.get("APPDATA") or os.path.expanduser("~")
else:
    OS_CONFIG = os.path.expanduser("~/.config")

CONFIG_PATH = os.path.join(OS_CONFIG, "qobuz-dl")
CONFIG_FILE = os.path.join(CONFIG_PATH, "config.ini")
QOBUZ_DB = os.path.join(CONFIG_PATH, "qobuz_dl.db")


TOKEN_PROMPT = (
    "Enter your Qobuz user_auth_token.\n"
    "Qobuz removed email/password login, so qobuz-dl authenticates with a\n"
    "token from a logged-in browser session.\n"
    "\n"
    "EASIEST: leave this blank and afterwards run 'qobuz-dl2 login' to capture\n"
    "the token automatically in a browser window (needs the [browser] extra).\n"
    "\n"
    "Or paste it manually: log in at https://play.qobuz.com, open\n"
    "DevTools > Network, filter by 'login', click the 'user/login' request,\n"
    "open the Response tab, and copy the value of 'user_auth_token'.\n"
    "- "
)


def _reset_config(config_file: str) -> None:
    cfg = config_store.default_config()
    cfg["DEFAULT"]["email"] = click.prompt("Email (optional)", default="", show_default=False)
    cfg["DEFAULT"]["password"] = click.prompt(
        "Qobuz token (leave blank to use login later)", default="", hide_input=True, show_default=False
    ).strip()
    cfg["DEFAULT"]["default_folder"] = click.prompt("Download directory", default="Qobuz Downloads")
    cfg["DEFAULT"]["default_quality"] = click.prompt("Quality", default="6", type=click.Choice(["5", "6", "7", "27"]))
    os.makedirs(os.path.dirname(config_file), exist_ok=True, mode=0o700)
    from qobuz_dl2.storage import file_lock

    with file_lock(config_file + ".lock"):
        config_store.write_config_atomic(config_file, cfg)
    click.echo(f"Configuration saved to {config_file}")


def _save_token(token: str) -> None:
    config_store.update_token(CONFIG_FILE, token)


def _save_browser_token(token: str) -> None:
    config_store.update_token(CONFIG_FILE, token)


def _remove_leftovers(directory: str) -> None:
    # Kept for library compatibility; cleanup belongs to each staging owner.
    return None


def _ensure_config_exists() -> None:
    if not os.path.exists(CONFIG_FILE):
        config_store.update_config(CONFIG_FILE, {})


def _load_config() -> Dict[str, Any]:
    return config_store.load_settings(CONFIG_FILE)


async def _build_qobuz(
    ctx: click.Context,
    *,
    directory: Optional[str] = None,
    quality: Optional[int] = None,
    embed_art: Optional[bool] = None,
    albums_only: Optional[bool] = None,
    no_m3u: Optional[bool] = None,
    no_fallback: Optional[bool] = None,
    og_cover: Optional[bool] = None,
    no_cover: Optional[bool] = None,
    no_db: Optional[bool] = None,
    folder_format: Optional[str] = None,
    track_format: Optional[str] = None,
    smart_discography: Optional[bool] = None,
    download_limit: Optional[int] = None,
) -> QobuzDL:
    cfg = ctx.obj
    dflt = cfg["defaults"]
    effective: Dict[str, Any] = {
        "directory": directory or dflt["default_folder"],
        "quality": int(quality if quality is not None else dflt["default_quality"]),
        "embed_art": bool(embed_art if embed_art is not None else dflt["embed_art"]),
        "ignore_singles_eps": bool(albums_only if albums_only is not None else dflt["albums_only"]),
        "no_m3u_for_playlists": bool(no_m3u if no_m3u is not None else dflt["no_m3u"]),
        "quality_fallback": not (no_fallback if no_fallback is not None else dflt["no_fallback"]),
        "cover_og_quality": bool(og_cover if og_cover is not None else dflt["og_cover"]),
        "no_cover": bool(no_cover if no_cover is not None else dflt["no_cover"]),
        "downloads_db": (None if (no_db if no_db is not None else dflt["no_database"]) else QOBUZ_DB),
        "folder_format": folder_format or dflt["folder_format"],
        "track_format": track_format or dflt["track_format"],
        "smart_discography": bool(smart_discography if smart_discography is not None else dflt["smart_discography"]),
        "download_limit": max(
            1,
            int(download_limit if download_limit is not None else dflt["download_limit"]),
        ),
    }
    config_store.validate_pattern(effective["folder_format"])
    config_store.validate_pattern(effective["track_format"], True)
    effective["directory"] = os.path.expanduser(effective["directory"])
    if not cfg["password"]:
        raise AuthenticationError("No token configured. Run qobuz-dl2 login or qobuz-dl2 set-token.")
    if not cfg["app_id"] or not cfg["secrets"]:
        bundle = await asyncio.get_running_loop().run_in_executor(None, Bundle)
        cfg["app_id"] = str(bundle.get_app_id())
        cfg["secrets"] = [s for s in bundle.get_secrets().values() if s]
        config_store.update_config(CONFIG_FILE, {"app_id": cfg["app_id"], "secrets": ",".join(cfg["secrets"])})
    q = QobuzDL(**effective)
    try:
        await q.initialize_client(cfg["password"], cfg["app_id"], cfg["secrets"], on_token_refresh=_save_token)
    except BaseException:
        await q.aclose()
        raise
    return q


def _common_options() -> Callable:
    def decorator(f: Callable) -> Callable:
        options = [
            click.option(
                "-d",
                "--directory",
                metavar="PATH",
                type=click.Path(path_type=str),
                help="directory for downloads (default from config)",
            ),
            click.option(
                "-q",
                "--quality",
                metavar="int",
                type=click.Choice(["5", "6", "7", "27"]),
                help='audio "quality" (5, 6, 7, 27)',
            ),
            click.option(
                "--albums-only",
                is_flag=True,
                default=None,
                help="don't download singles, EPs and VA releases",
            ),
            click.option(
                "--no-m3u",
                is_flag=True,
                default=None,
                help="don't create .m3u files when downloading playlists",
            ),
            click.option(
                "--no-fallback",
                is_flag=True,
                default=None,
                help="disable quality fallback (skip releases not available in set quality)",
            ),
            click.option("-e", "--embed-art", is_flag=True, default=None, help="embed cover art into files"),
            click.option(
                "--og-cover",
                is_flag=True,
                default=None,
                help="download cover art in its original quality (bigger file)",
            ),
            click.option(
                "--no-cover", is_flag=True, default=None, help="don't retain cover art (embedding still fetches art)"
            ),
            click.option("--no-db", is_flag=True, default=None, help="don't call the database"),
            click.option(
                "-ff",
                "--folder-format",
                metavar="PATTERN",
                help="pattern for formatting folder names",
            ),
            click.option(
                "-tf",
                "--track-format",
                metavar="PATTERN",
                help="pattern for formatting track names",
            ),
            click.option(
                "-s",
                "--smart-discography",
                is_flag=True,
                default=None,
                help="Enable heuristics to reduce spam-like albums in artist discographies",
            ),
            click.option(
                "--download-limit",
                metavar="int",
                type=click.IntRange(1),
                help="maximum simultaneous track downloads (default from config)",
            ),
        ]
        for opt in reversed(options):
            f = opt(f)
        return f

    return decorator


@click.group(
    invoke_without_command=True, no_args_is_help=True, context_settings=dict(help_option_names=["-h", "--help"])
)
@click.option("-r", "--reset", is_flag=True, default=None, help="create/reset config file")
@click.option("-p", "--purge", is_flag=True, default=None, help="purge/delete downloaded-IDs database")
@click.option("-sc", "--show-config", is_flag=True, default=None, help="show configuration")
@click.pass_context
def cli(ctx: click.Context, reset: bool, purge: bool, show_config: bool) -> None:
    """The ultimate Qobuz music downloader."""
    try:
        _maintenance(ctx, reset, purge, show_config)
    except (config_store.ConfigError, OSError) as error:
        raise click.ClickException(str(error)) from error


def _maintenance(ctx: click.Context, reset: bool, purge: bool, show_config: bool) -> None:
    if reset:
        _reset_config(CONFIG_FILE)
        ctx.exit(0)
    if show_config:
        cfg = config_store.read_config(CONFIG_FILE)
        for section in [cfg["DEFAULT"], *[cfg[name] for name in cfg.sections()]]:
            for key in list(section):
                if key in {"password", "secrets"}:
                    section[key] = "[redacted]"
        click.echo(f"Configuration: {CONFIG_FILE}\nDatabase: {QOBUZ_DB}")
        from io import StringIO

        output = StringIO()
        cfg.write(output)
        click.echo(output.getvalue())
        ctx.exit(0)
    if purge:
        try:
            os.remove(QOBUZ_DB)
        except FileNotFoundError:
            pass
        click.echo("Download history removed; media and rollback backups are preserved.")
        ctx.exit(0)


def _run(
    ctx: click.Context,
    opts: Dict[str, Any],
    action: Callable[[QobuzDL], Awaitable[Any]],
) -> None:
    """Build a client from the shared options and run an async action with
    uniform auth-error handling, Ctrl-C handling and temp-file cleanup."""

    async def runner() -> None:
        try:
            _ensure_config_exists()
            ctx.obj = _load_config()
            q = await _build_qobuz(ctx, **opts)
        except (
            AuthenticationError,
            IneligibleError,
            InvalidAppSecretError,
            config_store.ConfigError,
            RuntimeError,
        ) as e:
            raise click.ClickException(str(e)) from e
        try:
            result = await action(q)
            outcome = result if isinstance(result, BatchResult) else q.last_result
            if outcome.has_failures:
                raise click.ClickException("One or more requested downloads failed")
        except KeyboardInterrupt:
            raise
        finally:
            await q.aclose()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        ctx.exit(130)
    except click.ClickException:
        raise
    except Exception as error:
        raise click.ClickException(f"Operation failed ({type(error).__name__})") from error


@cli.command("fun")
@_common_options()
@click.option(
    "-l",
    "--limit",
    metavar="int",
    type=click.IntRange(1),
    help="limit of search results (default from config)",
)
@click.pass_context
def fun_cmd(ctx: click.Context, limit: Optional[int], **opts: Any) -> None:
    """Interactive mode."""

    async def action(q: QobuzDL) -> None:
        dflt_limit = ctx.obj["defaults"]["default_limit"]
        q.interactive_limit = int(limit) if limit is not None else int(dflt_limit)
        await q.interactive()

    _run(ctx, opts, action)


@cli.command("dl")
@_common_options()
@click.argument("source", nargs=-1, required=True)
@click.pass_context
def dl_cmd(ctx: click.Context, source: tuple[str, ...], **opts: Any) -> None:
    """Input mode: download by album/track/artist/label/playlist/last.fm-playlist URL(s) or a text file."""
    _run(ctx, opts, lambda q: q.download_list_of_urls(list(source)))


@cli.command("lucky")
@_common_options()
@click.option(
    "-t",
    "--type",
    "type_",
    default=None,
    type=click.Choice(["artist", "album", "track", "playlist"], case_sensitive=False),
    show_default=False,
    help="type of items to search (artist, album, track, playlist)",
)
@click.option(
    "-n",
    "--number",
    metavar="int",
    type=click.IntRange(1),
    default=None,
    show_default=False,
    help="number of results to download",
)
@click.argument("query", nargs=-1, required=True)
@click.pass_context
def lucky_cmd(
    ctx: click.Context,
    query: tuple[str, ...],
    type_: Optional[str],
    number: Optional[int],
    **opts: Any,
) -> None:
    """Lucky mode: Download the first <n> results from a Qobuz search."""

    async def action(q: QobuzDL) -> None:
        q.lucky_type = (type_ or "album").lower()
        q.lucky_limit = int(number) if number is not None else 1
        await q.lucky_mode(" ".join(query))

    _run(ctx, opts, action)


@cli.command("login")
@click.option(
    "--timeout",
    type=click.IntRange(1),
    default=300,
    show_default=True,
    help="seconds to wait for you to finish logging in",
)
@click.pass_context
def login_cmd(ctx: click.Context, timeout: int) -> None:
    """Open a browser to log in to Qobuz and capture your token automatically."""
    from qobuz_dl2.browser_login import fetch_token_via_browser

    click.echo(
        f"{YELLOW}Opening a browser window. Log in to Qobuz as usual "
        "(solve any captcha yourself).\nThis window will close and your token "
        "will be saved automatically once you're in."
    )
    try:
        token = fetch_token_via_browser(timeout=timeout)
    except RuntimeError as e:
        click.echo(f"{RED}{e}{RESET}")
        ctx.exit(1)
        return
    if not token:
        click.echo(f"{RED}No token captured (login not completed or window closed). Please try again.")
        ctx.exit(1)
        return
    try:
        _save_browser_token(token)
    except (config_store.ConfigError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"{GREEN}Token captured and saved to {CONFIG_FILE}{RESET}")


@cli.command("set-token")
@click.argument("token", required=False)
@click.option("--stdin", "from_stdin", is_flag=True, help="read a token from standard input")
def set_token_cmd(token: Optional[str], from_stdin: bool) -> None:
    """Store a fresh Qobuz user_auth_token in the config (no browser needed)."""
    if token is not None and from_stdin:
        raise click.UsageError("Use a token argument or --stdin, not both")
    if from_stdin:
        token = sys.stdin.read().strip()
    elif token is None:
        token = click.prompt("Qobuz token", hide_input=True)
    try:
        _save_token(token)
    except (config_store.ConfigError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"{GREEN}Token saved to {CONFIG_FILE}{RESET}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format=f"%(message)s{RESET}")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # If no subcommand provided, Click will show help automatically.
    try:
        cli(prog_name="qobuz-dl2", standalone_mode=True)
        return 0
    except SystemExit as e:
        # Normalize exit code
        return int(e.code) if e.code is not None else 0


if __name__ == "__main__":
    sys.exit(main())
