import asyncio
import contextlib
import functools
import inspect
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum, auto
from logging.handlers import RotatingFileHandler
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    Type,
    TypeGuard,
    TypeVar,
)

import aiofiles
import interactions
import orjson
from interactions.api.events import (
    AutoModExec,
    BanCreate,
    BanRemove,
    BaseEvent,
    ChannelCreate,
    ChannelDelete,
    ChannelUpdate,
    EntitlementCreate,
    EntitlementDelete,
    EntitlementUpdate,
    ExtensionLoad,
    GuildAvailable,
    GuildEmojisUpdate,
    GuildMembersChunk,
    GuildScheduledEventCreate,
    GuildScheduledEventDelete,
    GuildScheduledEventUpdate,
    GuildScheduledEventUserAdd,
    GuildScheduledEventUserRemove,
    GuildStickersUpdate,
    GuildUnavailable,
    GuildUpdate,
    InviteCreate,
    InviteDelete,
    MessageDeleteBulk,
    MessageReactionAdd,
    RoleCreate,
    RoleDelete,
    RoleUpdate,
    StageInstanceCreate,
    StageInstanceDelete,
    StageInstanceUpdate,
    ThreadListSync,
    VoiceStateUpdate,
    VoiceUserDeafen,
    VoiceUserMove,
    VoiceUserMute,
    WebhooksUpdate,
)
from interactions.client.errors import Forbidden, HTTPException

BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
LOG_FILE: str = os.path.join(BASE_DIR, "stats.log")

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    "%(asctime)s | %(process)d:%(thread)d | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
    "%Y-%m-%d %H:%M:%S.%f %z",
)
file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=1024 * 1024, backupCount=1, encoding="utf-8"
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Model

GUILD_ID: int = 1150630510696075404
MONITORED_FORUM_ID: int = 1155914521907568740
EXCLUDED_CATEGORY_ID: int = 1193393448393379980
TRANSPARENCY_FORUM_ID: int = 1159097493875871784
STATS_POST_ID: int = 1279118897454252146
LOG_POST_ID: int = 1279118293936111707
MAX_INTERACTIONS: int = 100
STATS_FILE_PATH: str = f"{os.path.dirname(__file__)}/stats.json"
LOG_ROTATION_THRESHOLD: int = 1 << 20
MONITORED_ROLE_IDS: frozenset[int] = frozenset(
    {
        1200043628899356702,
        1164761892015833129,
        1196405836206059621,
        1200046515046060052,
        1200050048906571816,
        1200050080359649340,
        1200050211821719572,
        1207524248558772264,
        1261328929013108778,
        1277238162095345809,
    }
)

T = TypeVar("T", covariant=True)
AsyncFunc = Callable[..., T]


@dataclass
class InteractionRecord:
    thread_id: int
    member_id: int

    def __hash__(self) -> int:
        return hash((self.thread_id, self.member_id))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, InteractionRecord):
            return NotImplemented
        return (self.thread_id, self.member_id) == (other.thread_id, other.member_id)


@dataclass
class RoleRecord:
    role_id: int
    display_name: str
    count: int


class EmbedColor(IntEnum):
    CREATE = 0x2ECC71
    UPDATE = 0x3498DB
    DELETE = 0xE74C3C
    INFO = 0x95A5A6
    SUCCESS = 0x2ECC71
    ERROR = 0xE74C3C
    WARNING = 0xF39C12


@dataclass
class EventLog:
    title: str
    description: str
    color: EmbedColor
    fields: tuple[tuple[str, Any, bool], ...]


class EventType(IntEnum):
    CHANNEL = auto()
    ROLE = auto()
    VOICE = auto()
    GUILD = auto()
    INVITE = auto()
    BAN = auto()
    SCHEDULED_EVENT = auto()
    INTEGRATION = auto()
    STAGE_INSTANCE = auto()
    AUTOMOD = auto()
    WEBHOOK = auto()
    THREAD = auto()
    MESSAGE = auto()
    STICKER = auto()
    ENTITLEMENT = auto()


async def rotate_file(file_path: str) -> None:
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path must be a non-empty string")

    try:
        loop = asyncio.get_running_loop()
        file_stats = await loop.run_in_executor(None, os.stat, file_path)

        if file_stats.st_size <= LOG_ROTATION_THRESHOLD:
            return

        await loop.run_in_executor(None, os.remove, file_path)
        logger.info("Deleted old file %s", file_path)

        async with aiofiles.open(file_path, mode="wb") as f:
            await f.write(b"{}")
            await loop.run_in_executor(None, os.fsync, f.fileno())
            await loop.run_in_executor(None, os.chmod, file_path, 0o644)

    except OSError as e:
        logger.error("Failed to rotate %s: %s", file_path, str(e))
        raise OSError(f"File rotation failed: {str(e)}") from e


# Check methods


def is_valid_event_class(x: Any) -> TypeGuard[tuple[str, Type[BaseEvent]]]:
    return (
        isinstance(x, tuple)
        and len(x) == 2
        and isinstance(x[0], str)
        and isinstance(x[1], type)
        and issubclass(x[1], BaseEvent)
    )


def find_event_class(event_name: str, action: str) -> Optional[Type[BaseEvent]]:
    corrections: dict[str, str] = {
        "integrati": "integration",
        "chan": "channel",
        "msg": "message",
        "sched": "scheduled",
    }

    words: tuple[str, ...] = tuple(
        filter(
            None, (corrections.get(w.lower(), w.lower()) for w in event_name.split("_"))
        )
    )

    name_variants: tuple[str, ...] = (
        "".join(map(str.capitalize, words)),
        "".join(map(str.capitalize, (w for w in words if w != "channel"))),
        f"{action.capitalize()}{''.join(map(str.capitalize, (w for w in words if w != action.lower())))}",
        f"{action.capitalize()}{words[0].capitalize()}",
        f"{words[-1].capitalize()}{action.capitalize()}",
        f"{''.join(w[0].upper() for w in words)}{action.capitalize()}",
    )

    return next(
        (
            event_class_mapping.get(name)
            for name in name_variants
            if event_class_mapping.get(name) is not None
        ),
        None,
    )


event_class_mapping: dict[str, Type[BaseEvent]] = {
    name: cls
    for name, cls in filter(
        is_valid_event_class,
        (
            (cls.__name__, cls)
            for cls in globals().values()
            if isinstance(cls, type)
            and issubclass(cls, BaseEvent)
            and cls is not BaseEvent
        ),
    )
}

# Decorator


def event_handler(
    event_type: EventType, action: str
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(
        func: Callable[..., Any],
        _event_type: EventType = event_type,
        _action: str = action,
    ) -> Callable[..., Any]:
        event_name: str = func.__name__.removeprefix("on_")

        if not (event_class := find_event_class(event_name, _action)):
            raise ValueError(
                f"No matching event class found for handler `{event_name}` with action `{_action}`"
            )

        @interactions.listen(event_class)
        @functools.wraps(func)
        async def wrapper(
            self,
            event: BaseEvent,
            *,
            _event_type_name: str = _event_type.name,
            _event_action: str = _action,
        ) -> None:
            try:
                event_log = (
                    await func(self, event)
                    if inspect.iscoroutinefunction(func)
                    else func(self, event)
                )
                await log_event(self, f"{_event_type_name}{_event_action}", event_log)
            except Exception as e:
                logger.exception(
                    "Critical error in event handler %s: %s", func.__qualname__, str(e)
                )
                raise

        return wrapper

    return decorator


def error_handler(func: AsyncFunc[T]) -> Callable[..., Awaitable[Optional[T]]]:

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Optional[T]:
        try:
            result = (
                await func(*args, **kwargs)
                if inspect.iscoroutinefunction(func)
                else func(*args, **kwargs)
            )
            return result
        except Exception as e:
            logger.exception(
                "Error in %s: %s\nArgs: %s\nKwargs: %s",
                func.__qualname__,
                str(e),
                repr(args),
                repr(kwargs),
            )
            return None
        finally:
            for name in ("args", "kwargs"):
                if name in locals():
                    del locals()[name]

    return wrapper


# View


def create_embed(event_log: EventLog) -> interactions.Embed:
    if not isinstance(event_log, EventLog):
        logger.error("Expected EventLog, got %s", type(event_log).__name__)
        raise ValueError(f"Expected EventLog, got {type(event_log).__name__}")

    return interactions.Embed(
        title=event_log.title,
        description=event_log.description,
        color=event_log.color.value,
        timestamp=datetime.now(timezone.utc).astimezone(),
        fields=tuple(
            interactions.EmbedField(
                name=str(name),
                value=str(value) if value else "N/A",
                inline=bool(inline),
            )
            for name, value, inline in event_log.fields
        ),
    )


async def log_event(self, event_name: str, event_log: EventLog) -> None:
    if not event_name or not isinstance(event_log, EventLog):
        logger.error(
            f"Invalid arguments for log_event: name={event_name}, log={type(event_log).__name__}"
        )
        return

    embed = await asyncio.to_thread(create_embed, event_log)

    async with event_logger(self, event_name):
        try:
            async with self.send_semaphore:
                try:
                    forum = await self.bot.fetch_channel(TRANSPARENCY_FORUM_ID)
                    if not forum:
                        logger.error("Could not fetch transparency forum channel")
                        return

                    post = await forum.fetch_post(LOG_POST_ID)
                    if not post:
                        logger.error("Could not fetch log post")
                        return

                    if post.archived:
                        try:
                            await post.edit(archived=False)
                        except Exception as e:
                            logger.error(f"Failed to unarchive post: {e}")
                            return

                    try:
                        await post.send(embeds=embed)
                        logger.info("Successfully sent embed")
                    except Exception as e:
                        logger.error(f"Failed to send embed: {e}")
                        return

                except HTTPException as e:
                    logger.error(f"HTTP error while sending event log: {e}")
                except Forbidden as e:
                    logger.error(f"Permission error while sending event log: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error while sending event log: {e}")

        except Exception as e:
            logger.exception(f"Critical error in log_event: {e}")
            raise


@contextlib.asynccontextmanager
async def event_logger(self, event_name: str) -> AsyncGenerator[None, None]:
    start_time = time.monotonic()
    log_id = str(uuid.uuid4())
    extra = {
        "event": event_name,
        "log_id": log_id,
        "start_time": datetime.now(timezone.utc).isoformat(),
    }

    try:
        logger.debug("Starting event log operation", extra=extra)
        yield

    except Exception as e:
        duration = time.monotonic() - start_time
        logger.error(
            f"Failed to log {event_name!r}: {e}",
            exc_info=True,
            extra={
                **extra,
                "duration": f"{duration:.3f}s",
                "error_type": type(e).__name__,
                "error_details": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            stack_info=True,
        )
        raise

    else:
        duration = time.monotonic() - start_time
        logger.debug(
            "Successfully completed event log operation",
            extra={
                **extra,
                "duration": f"{duration:.3f}s",
                "end_time": datetime.now(timezone.utc).isoformat(),
            },
        )


# Controller


class Statistics(interactions.Extension):
    def __init__(self, bot: interactions.Client) -> None:
        self.bot: interactions.Client = bot
        self.recent_interactions: Deque[InteractionRecord] = deque(
            maxlen=MAX_INTERACTIONS, iterable=[]
        )
        self.interaction_count_history: Deque[int] = deque(
            maxlen=MAX_INTERACTIONS, iterable=[0] * MAX_INTERACTIONS
        )
        self.timestamp_history: Deque[str] = deque(
            maxlen=MAX_INTERACTIONS, iterable=[""] * MAX_INTERACTIONS
        )

        self.role_counts: Dict[int, int] = {
            role_id: 0 for role_id in MONITORED_ROLE_IDS
        }

        self._cleanup_tasks: List[asyncio.Task] = []
        self.update_lock: asyncio.Lock = asyncio.Lock()
        self.stats_message: Optional[interactions.Message] = None
        self.send_semaphore: asyncio.Semaphore = asyncio.Semaphore(5)

    # Tasks

    @interactions.Task.create(interactions.IntervalTrigger(hours=1))
    async def check_log_rotation(self) -> None:
        try:
            await rotate_file(STATS_FILE_PATH)
        except Exception as e:
            logger.error(f"Failed to rotate files: {e}")

    @interactions.Task.create(interactions.IntervalTrigger(hours=12))
    async def update_interactions_daily(self) -> None:
        async with self.update_lock:
            self.timestamp_history.append(
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            )

            if not (
                target_forum := await self.fetch_monitored_forum(MONITORED_FORUM_ID)
            ):
                logger.error("Target forum not found during daily update")
                return
            current_valid_interactions = await self.fetch_current_interactions(
                target_forum
            )
            if not current_valid_interactions:
                logger.error("No valid interactions found during daily update")
                return

            current_set = set(await current_valid_interactions)
            existing_set = set(self.recent_interactions)
            new_interactions = tuple(current_set - existing_set)

            self.recent_interactions.extend(new_interactions)
            self.interaction_count_history.append(len(new_interactions))

            log_data = (
                tuple(self.interaction_count_history),
                tuple(self.timestamp_history),
                tuple((role_id, count) for role_id, count in self.role_counts.items()),
            )

            logger.info("Interactions: %s, Datetimes: %s", log_data[0], log_data[1])
            logger.info("Role counts: %s", dict(log_data[2]))

    @interactions.Task.create(interactions.IntervalTrigger(hours=12))
    async def update_stats_daily(self) -> None:
        try:
            async with asyncio.timeout(30):
                async with self.update_lock:
                    await self.update_stats_message()
        except TimeoutError:
            logger.error("update_stats_daily timed out after 30 seconds")
        except Exception as e:
            logger.error("Error in update_stats_daily: %s", str(e))

    # Commands

    module_base: interactions.SlashCommand = interactions.SlashCommand(
        name="stats", description="Activities statistics"
    )

    @module_base.subcommand("forum", sub_cmd_description="Get forum activities")
    @interactions.slash_option(
        name="channel",
        description="Select a forum channel to analyze",
        opt_type=interactions.OptionType.CHANNEL,
        required=True,
    )
    @error_handler
    async def forum_activities(
        self, ctx: interactions.SlashContext, channel: interactions.GuildChannel
    ) -> None:
        await ctx.defer()

        if not isinstance(channel, interactions.GuildForum):
            await ctx.send(
                embeds=(
                    interactions.Embed(
                        title="Error",
                        description="The selected channel is not a forum channel.",
                        color=EmbedColor.ERROR,
                    )
                )
            )
            return

        embed = (
            await self._get_monitored_forum_stats(channel)
            if channel.id == MONITORED_FORUM_ID
            else await self._get_realtime_forum_stats(channel)
        )
        await ctx.send(embeds=embed)

    @module_base.subcommand("guild", sub_cmd_description="Get guild statistics")
    @error_handler
    async def guild_statistics(self, ctx: interactions.SlashContext) -> None:
        await ctx.defer()

        guild = await self.bot.fetch_guild(GUILD_ID)

        stats = {
            attr: (
                len(getattr(guild, attr))
                if attr in ("bots", "channels", "roles")
                else getattr(guild, attr)
            )
            for attr in (
                "bots",
                "channels",
                "member_count",
                "max_members",
                "max_presences",
                "roles",
            )
        }

        embed = interactions.Embed(
            title="Guild Statistics",
            color=EmbedColor.SUCCESS.value,
            fields=(
                interactions.EmbedField(
                    name=f"{name.replace('_', ' ').title()}",
                    value=str(value),
                    inline=True,
                )
                for name, value in stats.items()
            ),
            footer=interactions.EmbedFooter(
                text=f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
            ),
        )

        await ctx.send(embeds=embed)

    @module_base.subcommand("roles", sub_cmd_description="Get role statistics")
    @error_handler
    async def role_statistics(self, ctx: interactions.SlashContext) -> None:
        await ctx.defer()

        role_data = await self.fetch_role_counts(GUILD_ID)
        embed = interactions.Embed(
            title="Role Statistics",
            color=EmbedColor.SUCCESS.value,
            fields=(
                interactions.EmbedField(
                    name=f"Role {role.display_name}",
                    value=f"Count: {role.count}",
                    inline=False,
                )
                for role in role_data
            ),
            footer=interactions.EmbedFooter(
                text=f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
            ),
        )

        await ctx.send(embeds=embed)

    @module_base.subcommand("export", sub_cmd_description="Export statistics")
    @error_handler
    async def export_stats(self, ctx: interactions.SlashContext) -> None:
        await ctx.defer()

        temp_file_path = (
            f"{os.path.dirname(__file__)}/temp_stats_{int(time.time_ns())}.json"
        )

        try:
            if not os.path.exists(STATS_FILE_PATH):
                default_stats = {
                    "interaction_count_history": list(self.interaction_count_history),
                    "timestamp_history": list(self.timestamp_history),
                    "role_counts": {
                        str(role_id): count
                        for role_id, count in self.role_counts.items()
                    },
                }
                async with aiofiles.open(STATS_FILE_PATH, mode="w") as f:
                    await f.write(orjson.dumps(default_stats).decode("utf-8"))

            async with aiofiles.open(STATS_FILE_PATH, mode="rb") as f:
                content = await f.read()

            try:
                orjson.loads(content)
            except orjson.JSONDecodeError:
                await ctx.send(
                    embeds=interactions.Embed(
                        title="Error",
                        description="The stats file is corrupted. Creating a new one.",
                        color=EmbedColor.ERROR,
                    )
                )
                return

            async with aiofiles.open(temp_file_path, mode="wb") as temp_file:
                await temp_file.write(content)
                await temp_file.flush()
                os.fsync(temp_file.fileno())

            await ctx.send(file=interactions.File(temp_file_path))

        except Exception as e:
            await ctx.send(
                embeds=interactions.Embed(
                    title="Error",
                    description=f"Failed to export stats: {str(e)}",
                    color=EmbedColor.ERROR,
                )
            )
            logger.error(f"Failed to export stats: {e}")

        finally:
            if os.path.exists(temp_file_path):
                await asyncio.to_thread(os.unlink, temp_file_path)

    # Serve

    async def fetch_monitored_forum(
        self, forum_id: int
    ) -> Optional[interactions.GuildForum]:
        channels = await (await self.bot.fetch_guild(GUILD_ID)).fetch_channels()
        for channel in channels:
            if isinstance(channel, interactions.GuildForum) and channel.id == forum_id:
                return channel
        return None

    @error_handler
    async def fetch_current_interactions(
        self, target_forum: interactions.GuildForum
    ) -> List[InteractionRecord]:
        interactions_set: set[InteractionRecord] = set()

        try:
            if not (threads := await target_forum.fetch_posts()):
                logger.error("Failed to fetch posts: No threads returned")
                return []

            for thread in threads:
                try:
                    if not (thread_members := await thread.fetch_members()):
                        logger.warning(f"No members found for thread {thread.id}")
                        continue

                    interactions_set.update(
                        InteractionRecord(thread.id, member.id)
                        for member in thread_members
                    )

                    async for message in thread.history(limit=100):
                        if not message.reactions:
                            continue

                        for reaction in message.reactions:
                            async for user in reaction.users():
                                interactions_set.add(
                                    InteractionRecord(thread.id, user.id)
                                )

                except Exception as e:
                    logger.error(f"Error processing thread {thread.id}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to fetch interactions: {e}")
            return []

        return list(interactions_set)

    async def _get_monitored_forum_stats(
        self, channel: interactions.GuildForum
    ) -> interactions.Embed:
        async with aiofiles.open(STATS_FILE_PATH, "r") as f:
            interaction_data: dict = orjson.loads(await f.read())

        recent_data = tuple(
            zip(
                interaction_data["timestamp_history"][-10:],
                interaction_data["interaction_count_history"][-10:],
            )
        )

        return interactions.Embed(
            title=f"Forum Activities for {channel.name} (Last 10 Entries)",
            color=EmbedColor.SUCCESS,
            fields=(
                {"name": dt, "value": f"Activities: {count}", "inline": True}
                for dt, count in recent_data
            ),
        )

    async def _get_realtime_forum_stats(
        self, channel: interactions.GuildForum
    ) -> interactions.Embed:
        current_valid_interactions = await self.fetch_current_interactions(channel)
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        return interactions.Embed(
            title=f"Real-time Forum Activities for {channel.name}",
            color=0x00FF00,
            fields=(
                {
                    "name": "Timestamp",
                    "value": current_time,
                    "inline": True,
                },
                {
                    "name": "Interaction Count",
                    "value": str(
                        len(
                            await current_valid_interactions
                            if current_valid_interactions
                            else []
                        )
                    ),
                    "inline": True,
                },
            ),
        )

    async def fetch_role_counts(self, guild_id: int) -> List[RoleRecord]:
        guild = await self.bot.fetch_guild(guild_id)
        roles = {
            role_id: await guild.fetch_role(role_id) for role_id in MONITORED_ROLE_IDS
        }
        return [
            RoleRecord(role_id=role_id, display_name=role.name, count=len(role.members))
            for role_id, role in roles.items()
            if role is not None
        ]

    async def update_stats_message(self) -> None:
        guild = await self.bot.fetch_guild(GUILD_ID)
        forum = await guild.fetch_channel(TRANSPARENCY_FORUM_ID)

        try:
            post = await forum.fetch_post(STATS_POST_ID)
        except Exception as e:
            logger.error(f"Failed to fetch post {STATS_POST_ID}: {e}")
            return

        if not post:
            logger.error(f"Post {STATS_POST_ID} not found")
            return

        embed = interactions.Embed(title="Guild Statistics", color=EmbedColor.SUCCESS)

        stats = {
            "Bot Count": len(guild.bots),
            "Channel Count": len(guild.channels),
            "Member Count": guild.member_count,
            "Max Members": guild.max_members,
            "Max Presences": guild.max_presences,
            "Role Count": len(guild.roles),
        }

        for name, value in stats.items():
            embed.add_field(name=name, value=str(value), inline=True)

        role_data = await self.fetch_role_counts(GUILD_ID)
        for role in role_data:
            embed.add_field(
                name=f"{role.display_name} Count", value=str(role.count), inline=True
            )

        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        embed.set_footer(text=f"Last updated: {current_time}")
        try:
            if self.stats_message:
                await self.stats_message.edit(embeds=[embed])
                await post.send(f"Statistics updated at {current_time}.")
            else:
                self.stats_message = await post.send(embeds=[embed])
        except Exception as e:
            logger.error(f"Failed to update stats message: {e}")

    # Listeners

    @interactions.listen(MessageReactionAdd)
    async def on_reaction_add(self, event: MessageReactionAdd) -> None:
        if event.message.channel.parent_id != MONITORED_FORUM_ID:
            return

        async with self.update_lock:
            new_interaction = InteractionRecord(
                event.message.channel.id, event.author.id
            )
            if new_interaction in self.recent_interactions:
                return

            self.recent_interactions.append(new_interaction)
            self.interaction_count_history.append(1)
            self.timestamp_history.append(
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            )

    @interactions.listen(ExtensionLoad)
    async def on_load(self, event: ExtensionLoad) -> None:
        self.check_log_rotation.start()
        self.update_interactions_daily.start()
        self.update_stats_daily.start()

    # Event

    @event_handler(EventType.CHANNEL, "Create")
    async def on_channel_create(self, event: ChannelCreate) -> EventLog:
        channel = event.channel
        channel_type = str(channel.type).replace("_", " ").title()
        created_timestamp = channel.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")

        parent_channel = None
        if hasattr(channel, "parent_id") and channel.parent_id:
            try:
                parent_channel = await self.bot.fetch_channel(channel.parent_id)
            except:
                pass

        category_name = parent_channel.name if parent_channel else "None"
        category_id = str(parent_channel.id) if parent_channel else "N/A"

        permission_details = []
        for overwrite in channel.permission_overwrites:
            target_type = "Role" if overwrite.type == 0 else "Member"
            allow_perms = [p for p in overwrite.allow] if overwrite.allow else []
            deny_perms = [p for p in overwrite.deny] if overwrite.deny else []

            if allow_perms or deny_perms:
                permission_details.append(
                    f"{target_type} {overwrite.id}:\n"
                    + (
                        f"  Allow: {', '.join(str(p) for p in allow_perms)}\n"
                        if allow_perms
                        else ""
                    )
                    + (
                        f"  Deny: {', '.join(str(p) for p in deny_perms)}"
                        if deny_perms
                        else ""
                    )
                )

        fields = [
            ("Channel Name", channel.name, True),
            ("Channel Type", channel_type, True),
            ("Channel ID", str(channel.id), True),
            ("Category Name", category_name, True),
            ("Category ID", category_id, True),
            ("Position", str(channel.position), True),
            ("Created At", created_timestamp, True),
            ("NSFW", "Yes" if channel.nsfw else "No", True),
            (
                "Slowmode",
                (
                    f"{channel.slowmode_delay}s"
                    if hasattr(channel, "slowmode_delay")
                    else "N/A"
                ),
                True,
            ),
        ]

        if hasattr(channel, "topic") and channel.topic:
            fields.append(("Topic", channel.topic, False))

        if hasattr(channel, "bitrate"):
            fields.append(("Bitrate", f"{channel.bitrate//1000}kbps", True))
        if hasattr(channel, "user_limit"):
            fields.append(
                (
                    "User Limit",
                    str(channel.user_limit) if channel.user_limit > 0 else "∞",
                    True,
                )
            )

        if permission_details:
            fields.append(
                ("Permission Overwrites", "\n".join(permission_details), False)
            )

        return EventLog(
            title="Channel Created",
            description=f"A new {channel_type.lower()} channel `{channel.name}` has been created.",
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.CHANNEL, "Delete")
    async def on_channel_delete(self, event: ChannelDelete) -> EventLog:
        channel = event.channel
        channel_type = str(channel.type).replace("_", " ").title()
        deleted_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lifetime = datetime.now(timezone.utc) - channel.created_at

        category_name = (
            channel.guild.get_channel(channel.parent_id).name
            if channel.parent_id
            else "None"
        )
        category_id = str(channel.parent_id) if channel.parent_id else "N/A"

        fields = [
            ("Channel Name", channel.name, True),
            ("Channel Type", channel_type, True),
            ("Channel ID", str(channel.id), True),
            ("Category Name", category_name, True),
            ("Category ID", category_id, True),
            ("Position", str(channel.position), True),
            ("Created At", channel.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), True),
            ("Deleted At", deleted_timestamp, True),
            (
                "Channel Age",
                f"{lifetime.days} days, {lifetime.seconds//3600} hours",
                True,
            ),
            ("NSFW", "Yes" if channel.nsfw else "No", True),
        ]

        if hasattr(channel, "topic") and channel.topic:
            fields.append(("Last Topic", channel.topic, False))

        if hasattr(channel, "message_count"):
            fields.append(("Total Messages", str(channel.message_count), True))

        if hasattr(channel, "bitrate"):
            fields.append(("Last Bitrate", f"{channel.bitrate//1000}kbps", True))
        if hasattr(channel, "user_limit"):
            fields.append(
                (
                    "Last User Limit",
                    str(channel.user_limit) if channel.user_limit > 0 else "∞",
                    True,
                )
            )

        if hasattr(channel, "members"):
            fields.append(("Member Count", str(len(channel.members)), True))

        if hasattr(channel, "rtc_region") and channel.rtc_region:
            fields.append(("Voice Region", channel.rtc_region, True))

        if hasattr(channel, "video_quality_mode"):
            quality_mode = "Auto" if channel.video_quality_mode == 1 else "720p"
            fields.append(("Video Quality", quality_mode, True))

        return EventLog(
            title="Channel Deleted",
            description=f"The {channel_type.lower()} channel `{channel.name}` has been permanently deleted.",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.CHANNEL, "Update")
    async def on_channel_update(self, event: ChannelUpdate) -> EventLog:
        # if event.before.parent_id == EXCLUDED_CATEGORY_ID:
        #    return EventLog(
        #        title="Channel Updated (Excluded)",
        #        description="A channel in an excluded category was updated. Details hidden for privacy.",
        #        color=EmbedColor.INFO,
        #        fields=(),
        #    )

        fields = []
        before = event.before
        after = event.after

        if before.name != after.name:
            fields.append(
                ("Name Change", f"From: `{before.name}`\nTo: `{after.name}`", True)
            )

        if before.type != after.type:
            fields.append(
                (
                    "Type Change",
                    f"From: {str(before.type).replace('_', ' ').title()}\nTo: {str(after.type).replace('_', ' ').title()}",
                    True,
                )
            )

        if before.parent_id != after.parent_id:
            before_category = before.parent.name if before.parent else "None"
            after_category = after.parent.name if after.parent else "None"
            fields.append(
                (
                    "Category Change",
                    f"From: `{before_category}` (ID: {before.parent_id or 'None'})\nTo: `{after_category}` (ID: {after.parent_id or 'None'}`)",
                    True,
                )
            )

        if before.position != after.position:
            fields.append(
                (
                    "Position Change",
                    f"From: {before.position}\nTo: {after.position}",
                    True,
                )
            )

        if (
            hasattr(before, "topic")
            and hasattr(after, "topic")
            and before.topic != after.topic
        ):
            fields.append(
                (
                    "Topic Change",
                    f"From: `{before.topic or 'None'}`\nTo: `{after.topic or 'None'}`",
                    False,
                )
            )

        if before.nsfw != after.nsfw:
            fields.append(
                (
                    "NSFW Status",
                    f"From: {'Enabled' if before.nsfw else 'Disabled'}\nTo: {'Enabled' if after.nsfw else 'Disabled'}",
                    True,
                )
            )

        if hasattr(before, "slowmode_delay") and hasattr(after, "slowmode_delay"):
            if before.slowmode_delay != after.slowmode_delay:
                fields.append(
                    (
                        "Slowmode Change",
                        f"From: {before.slowmode_delay}s\nTo: {after.slowmode_delay}s",
                        True,
                    )
                )

        if hasattr(before, "bitrate") and hasattr(after, "bitrate"):
            if before.bitrate != after.bitrate:
                fields.append(
                    (
                        "Bitrate Change",
                        f"From: {before.bitrate//1000}kbps\nTo: {after.bitrate//1000}kbps",
                        True,
                    )
                )

        if hasattr(before, "user_limit") and hasattr(after, "user_limit"):
            if before.user_limit != after.user_limit:
                before_limit = str(before.user_limit) if before.user_limit > 0 else "∞"
                after_limit = str(after.user_limit) if after.user_limit > 0 else "∞"
                fields.append(
                    (
                        "User Limit Change",
                        f"From: {before_limit}\nTo: {after_limit}",
                        True,
                    )
                )

        if before.permission_overwrites != after.permission_overwrites:
            before_overwrites = {po.id: po for po in before.permission_overwrites}
            after_overwrites = {po.id: po for po in after.permission_overwrites}

            added_overwrites = set(after_overwrites.keys()) - set(
                before_overwrites.keys()
            )
            removed_overwrites = set(before_overwrites.keys()) - set(
                after_overwrites.keys()
            )
            modified_overwrites = set(before_overwrites.keys()) & set(
                after_overwrites.keys()
            )

            permission_changes = []

            if added_overwrites:
                permission_changes.append(
                    "Added permissions for: "
                    + ", ".join(str(id) for id in added_overwrites)
                )

            if removed_overwrites:
                permission_changes.append(
                    "Removed permissions for: "
                    + ", ".join(str(id) for id in removed_overwrites)
                )

            for target_id in modified_overwrites:
                before_perms = before_overwrites[target_id]
                after_perms = after_overwrites[target_id]

                if before_perms != after_perms:
                    added_allows = set(after_perms.allow or []) - set(
                        before_perms.allow or []
                    )
                    removed_allows = set(before_perms.allow or []) - set(
                        after_perms.allow or []
                    )
                    added_denies = set(after_perms.deny or []) - set(
                        before_perms.deny or []
                    )
                    removed_denies = set(before_perms.deny or []) - set(
                        after_perms.deny or []
                    )

                    if any(
                        [added_allows, removed_allows, added_denies, removed_denies]
                    ):
                        changes = []
                        if added_allows:
                            changes.append(
                                f"Added allows: {', '.join(str(p) for p in added_allows)}"
                            )
                        if removed_allows:
                            changes.append(
                                f"Removed allows: {', '.join(str(p) for p in removed_allows)}"
                            )
                        if added_denies:
                            changes.append(
                                f"Added denies: {', '.join(str(p) for p in added_denies)}"
                            )
                        if removed_denies:
                            changes.append(
                                f"Removed denies: {', '.join(str(p) for p in removed_denies)}"
                            )

                        permission_changes.append(
                            f"Modified permissions for {target_id}:\n"
                            + "\n".join(changes)
                        )

            if permission_changes:
                fields.append(
                    ("Permission Changes", "\n".join(permission_changes), False)
                )

        fields.extend(
            [
                ("Channel ID", str(after.id), True),
                ("Guild", after.guild.name if after.guild else "N/A", True),
                ("Guild ID", str(after.guild.id) if after.guild else "N/A", True),
                (
                    "Updated At",
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    True,
                ),
            ]
        )

        return EventLog(
            title="Channel Updated",
            description=f"Channel `{before.name}` has been modified with the following changes:",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.ROLE, "Create")
    async def on_role_create(self, event: RoleCreate) -> EventLog:
        role = event.role
        created_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        color_hex = f"#{role.color.value:06x}" if role.color else "Default"

        permissions = [str(p) for p in role.permissions] if role.permissions else []
        permission_text = (
            ", ".join(str(p) for p in permissions) if permissions else "No permissions"
        )

        fields = [
            ("Role Name", role.name, True),
            ("Role ID", str(role.id), True),
            ("Color", color_hex, True),
            ("Position", str(role.position), True),
            ("Hoisted", "Yes" if role.hoist else "No", True),
            ("Mentionable", "Yes" if role.mentionable else "No", True),
            ("Managed", "Yes" if role.managed else "No", True),
            ("Integration", "Yes" if role.integration else "No", True),
            ("Created At", created_timestamp, True),
            ("Permissions", permission_text, False),
        ]

        return EventLog(
            title="Role Created",
            description=f"A new role `{role.name}` has been created.",
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.ROLE, "Delete")
    async def on_role_delete(self, event: RoleDelete) -> EventLog:
        role = event.role
        deleted_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        if not role:
            return EventLog(
                title="Role Deleted",
                description="A role was deleted but details are unavailable.",
                color=EmbedColor.DELETE,
                fields=(
                    ("Role ID", str(event.id), True),
                    ("Deleted At", deleted_timestamp, True),
                ),
            )

        color_hex = f"#{role.color.value:06x}" if role.color else "Default"

        permissions = [str(p) for p in role.permissions] if role.permissions else []
        permission_text = (
            ", ".join(str(p) for p in permissions) if permissions else "No permissions"
        )

        role_age = datetime.now(timezone.utc) - role.created_at
        age_text = f"{role_age.days} days, {role_age.seconds//3600} hours"

        fields = [
            ("Role Name", role.name, True),
            ("Role ID", str(role.id), True),
            ("Color", color_hex, True),
            ("Position", str(role.position), True),
            ("Hoisted", "Yes" if role.hoist else "No", True),
            ("Mentionable", "Yes" if role.mentionable else "No", True),
            ("Managed", "Yes" if role.managed else "No", True),
            ("Integration", "Yes" if role.integration else "No", True),
            ("Created At", role.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), True),
            ("Deleted At", deleted_timestamp, True),
            ("Role Age", age_text, True),
            (
                "Member Count",
                str(len(role.members)) if hasattr(role, "members") else "Unknown",
                True,
            ),
            ("Permissions", permission_text, False),
        ]

        return EventLog(
            title="Role Deleted",
            description=f"Role `{role.name}` has been permanently deleted.",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.ROLE, "Update")
    async def on_role_update(self, event: RoleUpdate) -> EventLog:
        fields = []
        before = event.before
        after = event.after

        if before.color != after.color:
            before_color = f"#{before.color.value:06x}" if before.color else "Default"
            after_color = f"#{after.color.value:06x}" if after.color else "Default"
            fields.append(
                ("Color Change", f"From: {before_color}\nTo: {after_color}", True)
            )

        if before.name != after.name:
            fields.append(
                ("Name Change", f"From: `{before.name}`\nTo: `{after.name}`", True)
            )

        if before.permissions != after.permissions:
            added_perms = [
                str(p) for p in after.permissions if p not in before.permissions
            ]
            removed_perms = [
                str(p) for p in before.permissions if p not in after.permissions
            ]

            if added_perms:
                fields.append(("Added Permissions", "\n".join(added_perms), True))
            if removed_perms:
                fields.append(("Removed Permissions", "\n".join(removed_perms), True))

        if before.position != after.position:
            fields.append(
                (
                    "Position Change",
                    f"From: {before.position}\nTo: {after.position}\n"
                    + f"(Moved {'up' if after.position > before.position else 'down'} "
                    + f"by {abs(after.position - before.position)} positions)",
                    True,
                )
            )

        if before.hoist != after.hoist:
            fields.append(
                (
                    "Hoist Status",
                    f"From: {'Hoisted' if before.hoist else 'Not hoisted'}\n"
                    + f"To: {'Hoisted' if after.hoist else 'Not hoisted'}",
                    True,
                )
            )

        if before.mentionable != after.mentionable:
            fields.append(
                (
                    "Mentionable Status",
                    f"From: {'Mentionable' if before.mentionable else 'Not mentionable'}\n"
                    + f"To: {'Mentionable' if after.mentionable else 'Not mentionable'}",
                    True,
                )
            )

        if before.managed != after.managed:
            fields.append(
                (
                    "Managed Status",
                    f"From: {'Managed' if before.managed else 'Not managed'}\n"
                    + f"To: {'Managed' if after.managed else 'Not managed'}",
                    True,
                )
            )

        if before.integration != after.integration:
            fields.append(
                (
                    "Integration Status",
                    f"From: {'Integrated' if before.integration else 'Not integrated'}\n"
                    + f"To: {'Integrated' if after.integration else 'Not integrated'}",
                    True,
                )
            )

        fields.extend(
            [
                ("Role ID", str(after.id), True),
                ("Guild", after.guild.name if after.guild else "N/A", True),
                ("Guild ID", str(after.guild.id) if after.guild else "N/A", True),
                (
                    "Member Count",
                    str(len(after.members)) if hasattr(after, "members") else "Unknown",
                    True,
                ),
                (
                    "Updated At",
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    True,
                ),
            ]
        )

        return EventLog(
            title="Role Updated",
            description=f"Role `{before.name}` has been modified with the following changes:",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.VOICE, "StateUpdate")
    async def on_voice_state_update(self, event: VoiceStateUpdate) -> EventLog:
        fields = []
        before = event.before
        after = event.after
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        if before and after and before.channel != after.channel:
            before_channel = before.channel.name if before.channel else "None"
            after_channel = after.channel.name if after.channel else "None"

            if not before.channel and after.channel:
                action = "joined"
            elif before.channel and not after.channel:
                action = "left"
            else:
                action = "moved between"

            fields.append(
                (
                    "Channel Change",
                    f"User {action} voice channels\n"
                    + f"From: `{before_channel}`\n"
                    + f"To: `{after_channel}`",
                    True,
                )
            )

        if before and after and before.mute != after.mute:
            fields.append(
                (
                    "Server Mute",
                    f"{'Muted by server' if after.mute else 'Unmuted by server'}",
                    True,
                )
            )

        if before and after and before.deaf != after.deaf:
            fields.append(
                (
                    "Server Deafen",
                    f"{'Deafened by server' if after.deaf else 'Undeafened by server'}",
                    True,
                )
            )

        if before and after and before.self_mute != after.self_mute:
            fields.append(
                (
                    "Self Mute",
                    f"User {'muted themselves' if after.self_mute else 'unmuted themselves'}",
                    True,
                )
            )

        if before and after and before.self_deaf != after.self_deaf:
            fields.append(
                (
                    "Self Deafen",
                    f"User {'deafened themselves' if after.self_deaf else 'undeafened themselves'}",
                    True,
                )
            )

        if before and after and before.self_stream != after.self_stream:
            fields.append(
                (
                    "Streaming Status",
                    f"User {'started' if after.self_stream else 'stopped'} streaming",
                    True,
                )
            )

        if before and after and before.self_video != after.self_video:
            fields.append(
                (
                    "Video Status",
                    f"User {'enabled' if after.self_video else 'disabled'} their camera",
                    True,
                )
            )

        if before and after and before.suppress != after.suppress:
            fields.append(
                (
                    "Suppress Status",
                    f"User {'is now suppressed' if after.suppress else 'is no longer suppressed'}",
                    True,
                )
            )

        if (
            before
            and after
            and before.request_to_speak_timestamp != after.request_to_speak_timestamp
        ):
            if after.request_to_speak_timestamp:
                fields.append(
                    (
                        "Speaking Request",
                        f"User requested to speak at {after.request_to_speak_timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}",
                        True,
                    )
                )
            else:
                fields.append(
                    ("Speaking Request", "User's request to speak was removed", True)
                )

        if after and after.member:
            fields.extend(
                [
                    ("User", after.member.display_name, True),
                    ("User ID", str(after.member.id), True),
                    (
                        "Nickname",
                        after.member.nick if after.member.nick else "None",
                        True,
                    ),
                    (
                        "Roles",
                        (
                            ", ".join(role.name for role in after.member.roles)
                            if after.member.roles
                            else "None"
                        ),
                        False,
                    ),
                    ("Session ID", after.session_id, True),
                    ("Updated At", timestamp, True),
                ]
            )

            current_state = [
                f"Channel: {after.channel.name if after.channel else 'None'}",
                f"Server Muted: {'Yes' if after.mute else 'No'}",
                f"Server Deafened: {'Yes' if after.deaf else 'No'}",
                f"Self Muted: {'Yes' if after.self_mute else 'No'}",
                f"Self Deafened: {'Yes' if after.self_deaf else 'No'}",
                f"Streaming: {'Yes' if after.self_stream else 'No'}",
                f"Camera On: {'Yes' if after.self_video else 'No'}",
                f"Suppressed: {'Yes' if after.suppress else 'No'}",
            ]
            fields.append(("Current Voice State", "\n".join(current_state), False))

            return EventLog(
                title="Voice State Updated",
                description=f"Voice state changes detected for {after.member.display_name}",
                color=EmbedColor.UPDATE,
                fields=tuple(fields),
            )

        return EventLog(
            title="Voice State Updated",
            description="Voice state changes detected but member information unavailable",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.GUILD, "Update")
    async def on_guild_update(self, event: GuildUpdate) -> EventLog:
        fields = []
        before = event.before
        after = event.after
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        if before.name != after.name:
            fields.append(
                ("Name Change", f"From: `{before.name}`\nTo: `{after.name}`", True)
            )

        if before.icon != after.icon:
            fields.append(
                (
                    "Icon",
                    "Server icon has been updated\n"
                    + f"Old: {before.icon_url if before.icon else 'None'}\n"
                    + f"New: {after.icon_url if after.icon else 'None'}",
                    True,
                )
            )

        if before.banner != after.banner:
            fields.append(
                (
                    "Banner",
                    "Server banner has been updated\n"
                    + f"Old: {before.banner_url if before.banner else 'None'}\n"
                    + f"New: {after.banner_url if after.banner else 'None'}",
                    True,
                )
            )

        if before.splash != after.splash:
            fields.append(
                (
                    "Invite Splash",
                    "Invite splash image has been updated\n"
                    + f"Old: {before.splash_url if before.splash else 'None'}\n"
                    + f"New: {after.splash_url if after.splash else 'None'}",
                    True,
                )
            )

        if before.discovery_splash != after.discovery_splash:
            fields.append(
                (
                    "Discovery Splash",
                    "Discovery splash image has been updated\n"
                    + f"Old: {before.discovery_splash_url if before.discovery_splash else 'None'}\n"
                    + f"New: {after.discovery_splash_url if after.discovery_splash else 'None'}",
                    True,
                )
            )

        if before.owner_id != after.owner_id:
            fields.append(
                (
                    "Ownership Transfer",
                    f"From: <@{before.owner_id}>\nTo: <@{after.owner_id}>",
                    True,
                )
            )

        if before.verification_level != after.verification_level:
            fields.append(
                (
                    "Verification Level",
                    f"From: {before.verification_level}\nTo: {after.verification_level}",
                    True,
                )
            )

        if before.explicit_content_filter != after.explicit_content_filter:
            fields.append(
                (
                    "Content Filter",
                    f"From: {before.explicit_content_filter}\nTo: {after.explicit_content_filter}",
                    True,
                )
            )

        if before.default_notifications != after.default_notifications:
            fields.append(
                (
                    "Default Notifications",
                    f"From: {before.default_notifications}\nTo: {after.default_notifications}",
                    True,
                )
            )

        if before.afk_channel_id != after.afk_channel_id:
            before_afk = before.afk_channel.name if before.afk_channel else "None"
            after_afk = after.afk_channel.name if after.afk_channel else "None"
            fields.append(("AFK Channel", f"From: {before_afk}\nTo: {after_afk}", True))

        if before.afk_timeout != after.afk_timeout:
            fields.append(
                (
                    "AFK Timeout",
                    f"From: {before.afk_timeout} seconds\nTo: {after.afk_timeout} seconds",
                    True,
                )
            )

        if before.system_channel_id != after.system_channel_id:
            before_sys = before.system_channel.name if before.system_channel else "None"
            after_sys = after.system_channel.name if after.system_channel else "None"
            fields.append(
                ("System Channel", f"From: {before_sys}\nTo: {after_sys}", True)
            )

        if before.rules_channel_id != after.rules_channel_id:
            before_rules = before.rules_channel.name if before.rules_channel else "None"
            after_rules = after.rules_channel.name if after.rules_channel else "None"
            fields.append(
                ("Rules Channel", f"From: {before_rules}\nTo: {after_rules}", True)
            )

        if before.public_updates_channel_id != after.public_updates_channel_id:
            before_updates = (
                before.public_updates_channel.name
                if before.public_updates_channel
                else "None"
            )
            after_updates = (
                after.public_updates_channel.name
                if after.public_updates_channel
                else "None"
            )
            fields.append(
                (
                    "Public Updates Channel",
                    f"From: {before_updates}\nTo: {after_updates}",
                    True,
                )
            )

        if before.preferred_locale != after.preferred_locale:
            fields.append(
                (
                    "Preferred Locale",
                    f"From: {before.preferred_locale}\nTo: {after.preferred_locale}",
                    True,
                )
            )

        if before.premium_tier != after.premium_tier:
            fields.append(
                (
                    "Premium Tier",
                    f"From: Level {before.premium_tier}\nTo: Level {after.premium_tier}",
                    True,
                )
            )

        if before.premium_subscription_count != after.premium_subscription_count:
            fields.append(
                (
                    "Boost Count",
                    f"From: {before.premium_subscription_count} boosts\nTo: {after.premium_subscription_count} boosts",
                    True,
                )
            )

        if before.vanity_url_code != after.vanity_url_code:
            fields.append(
                (
                    "Vanity URL",
                    f"From: {before.vanity_url_code or 'None'}\nTo: {after.vanity_url_code or 'None'}",
                    True,
                )
            )

        if before.description != after.description:
            fields.append(
                (
                    "Description",
                    f"From: {before.description or 'None'}\nTo: {after.description or 'None'}",
                    False,
                )
            )

        if before.features != after.features:
            added_features = set(after.features) - set(before.features)
            removed_features = set(before.features) - set(after.features)

            if added_features:
                fields.append(
                    (
                        "Added Features",
                        "\n".join(f"- {feature}" for feature in added_features),
                        True,
                    )
                )

            if removed_features:
                fields.append(
                    (
                        "Removed Features",
                        "\n".join(f"- {feature}" for feature in removed_features),
                        True,
                    )
                )

        fields.extend(
            [
                ("Server ID", str(after.id), True),
                (
                    "Region",
                    str(after.region) if hasattr(after, "region") else "Unknown",
                    True,
                ),
                (
                    "Member Count",
                    (
                        str(after.member_count)
                        if hasattr(after, "member_count")
                        else "Unknown"
                    ),
                    True,
                ),
                (
                    "Created At",
                    after.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    True,
                ),
                ("Updated At", timestamp, True),
            ]
        )

        return EventLog(
            title="Server Updated",
            description=f"Server `{before.name}` has been modified with the following changes:",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.GUILD, "EmojisUpdate")
    async def on_guild_emojis_update(self, event: GuildEmojisUpdate) -> EventLog:
        added_emojis = set(event.after) - set(event.before)
        removed_emojis = set(event.before) - set(event.after)
        modified_emojis = {
            emoji
            for emoji in event.after
            if emoji in event.before
            and any(
                getattr(emoji, attr)
                != getattr(event.before[event.before.index(emoji)], attr)
                for attr in ["name", "roles", "require_colons", "managed", "available"]
            )
        }

        fields = []

        if added_emojis:
            emoji_details = []
            for emoji in added_emojis:
                emoji_info = [
                    f"Name: {emoji.name}",
                    f"ID: {emoji.id}",
                    f"Animated: {'Yes' if emoji.animated else 'No'}",
                    f"Available: {'Yes' if emoji.available else 'No'}",
                    f"Managed: {'Yes' if emoji.managed else 'No'}",
                ]
                if emoji.roles:
                    emoji_info.append(
                        f"Restricted to roles: {', '.join(role.name for role in emoji.roles)}"
                    )
                emoji_details.append("\n".join(emoji_info))
            fields.append(("Added Emojis", "\n\n".join(emoji_details), False))

        if removed_emojis:
            emoji_details = []
            for emoji in removed_emojis:
                emoji_info = [
                    f"Name: {emoji.name}",
                    f"ID: {emoji.id}",
                    f"Animated: {'Yes' if emoji.animated else 'No'}",
                ]
                emoji_details.append("\n".join(emoji_info))
            fields.append(("Removed Emojis", "\n\n".join(emoji_details), False))

        if modified_emojis:
            emoji_details = []
            for emoji in modified_emojis:
                old_emoji = event.before[event.before.index(emoji)]
                changes = []
                if emoji.name != old_emoji.name:
                    changes.append(f"Name: {old_emoji.name} → {emoji.name}")
                if emoji.roles != old_emoji.roles:
                    changes.append(
                        f"Roles: {', '.join(r.name for r in old_emoji.roles)} → {', '.join(r.name for r in emoji.roles)}"
                    )
                if emoji.available != old_emoji.available:
                    changes.append(
                        f"Available: {'Yes' if old_emoji.available else 'No'} → {'Yes' if emoji.available else 'No'}"
                    )
                emoji_details.append(
                    f"Emoji: {emoji.name} ({emoji.id})\n" + "\n".join(changes)
                )
            fields.append(("Modified Emojis", "\n\n".join(emoji_details), False))

        fields.extend(
            [
                ("Server", event.guild.name, True),
                ("Server ID", str(event.guild.id), True),
                ("Total Emojis", str(len(event.after)), True),
                (
                    "Regular Emojis",
                    str(len([e for e in event.after if not e.animated])),
                    True,
                ),
                (
                    "Animated Emojis",
                    str(len([e for e in event.after if e.animated])),
                    True,
                ),
                (
                    "Updated At",
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    True,
                ),
            ]
        )

        return EventLog(
            title="Server Emojis Updated",
            description=f"The emoji list for `{event.guild.name}` has been modified.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.INVITE, "Create")
    async def on_invite_create(self, event: InviteCreate) -> EventLog:
        fields = [
            ("Created By", event.inviter.display_name, True),
            ("Creator ID", str(event.inviter.id), True),
            ("Channel", event.channel.name, True),
            ("Channel ID", str(event.channel.id), True),
            ("Max Uses", "∞" if event.max_uses == 0 else str(event.max_uses), True),
            ("Current Uses", str(event.uses), True),
            (
                "Expires At",
                (
                    "Never"
                    if not event.expires_at
                    else event.expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                ),
                True,
            ),
            ("Temporary", "Yes" if event.temporary else "No", True),
            (
                "Created At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        if hasattr(event, "target_type"):
            fields.append(("Target Type", str(event.target_type), True))
        if hasattr(event, "target_user"):
            fields.append(("Target User", str(event.target_user), True))
        if hasattr(event, "target_application"):
            fields.append(("Target Application", str(event.target_application), True))

        return EventLog(
            title="Server Invite Created",
            description=(
                f"A new invite has been created by {event.inviter.display_name} "
                f"for channel #{event.channel.name}"
            ),
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.INVITE, "Delete")
    async def on_invite_delete(self, event: InviteDelete) -> EventLog:
        fields = [
            ("Channel", event.channel.name, True),
            ("Channel ID", str(event.channel.id), True),
            ("Code", event.code if hasattr(event, "code") else "Unknown", True),
            ("Guild", event.guild.name if event.guild else "Unknown", True),
            ("Guild ID", str(event.guild.id) if event.guild else "Unknown", True),
            (
                "Deleted At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="Server Invite Deleted",
            description=(
                f"An invite for channel #{event.channel.name} has been deleted"
            ),
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.BAN, "Create")
    async def on_ban_create(self, event: BanCreate) -> EventLog:
        fields = [
            ("User", event.user.display_name, True),
            ("User ID", str(event.user.id), True),
            ("Username", str(event.user), True),
            ("Bot Account", "Yes" if event.user.bot else "No", True),
            (
                "Account Created",
                event.user.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
            ("Server", event.guild.name, True),
            ("Server ID", str(event.guild.id), True),
            (
                "Banned At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
            ("Reason", event.reason if event.reason else "No reason provided", False),
        ]

        return EventLog(
            title="Member Banned",
            description=(
                f"{event.user.display_name} ({event.user}) has been banned from {event.guild.name}"
            ),
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.BAN, "Remove")
    async def on_ban_remove(self, event: BanRemove) -> EventLog:
        fields = [
            ("User", event.user.display_name, True),
            ("User ID", str(event.user.id), True),
            ("Username", str(event.user), True),
            ("Bot Account", "Yes" if event.user.bot else "No", True),
            (
                "Account Created",
                event.user.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
            ("Server", event.guild.name, True),
            ("Server ID", str(event.guild.id), True),
            (
                "Unbanned At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="Member Unbanned",
            description=(
                f"{event.user.display_name} ({event.user}) has been unbanned from {event.guild.name}"
            ),
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.SCHEDULED_EVENT, "Create")
    async def on_guild_scheduled_event_create(
        self, event: GuildScheduledEventCreate
    ) -> EventLog:
        description = event.scheduled_event.description
        truncated_desc = (
            f"{description[:1000]}..."
            if description and len(description) > 1000
            else description or "No description provided"
        )

        fields = [
            ("Event Name", event.scheduled_event.name, True),
            (
                "Creator",
                (
                    str(event.scheduled_event.creator.display_name)
                    if event.scheduled_event.creator
                    else "Unknown"
                ),
                True,
            ),
            (
                "Location",
                (
                    event.scheduled_event.location
                    if hasattr(event.scheduled_event, "location")
                    else "Not specified"
                ),
                True,
            ),
            (
                "Start Time",
                event.scheduled_event.start_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
            (
                "End Time",
                (
                    event.scheduled_event.end_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                    if event.scheduled_event.end_time
                    else "Not specified"
                ),
                True,
            ),
            ("Status", str(event.scheduled_event.status), True),
            ("Privacy Level", str(event.scheduled_event.privacy_level), True),
            ("Entity Type", str(event.scheduled_event.entity_type), True),
            (
                "Channel",
                (
                    event.scheduled_event.channel.name
                    if event.scheduled_event.channel
                    else "N/A"
                ),
                True,
            ),
            ("Description", truncated_desc, False),
            (
                "Created At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="Server Event Created",
            description=(
                f"A new scheduled event `{event.scheduled_event.name}` has been created"
            ),
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.SCHEDULED_EVENT, "Update")
    async def on_guild_scheduled_event_update(
        self, event: GuildScheduledEventUpdate
    ) -> EventLog:
        fields = []

        if event.before.name != event.after.name:
            fields.append(
                (
                    "Name Change",
                    f"From: `{event.before.name}`\nTo: `{event.after.name}`",
                    True,
                )
            )

        if event.before.description != event.after.description:
            before_desc = (
                event.before.description[:500] + "..."
                if event.before.description and len(event.before.description) > 500
                else event.before.description or "No description"
            )
            after_desc = (
                event.after.description[:500] + "..."
                if event.after.description and len(event.after.description) > 500
                else event.after.description or "No description"
            )
            fields.append(
                ("Description Change", f"From: {before_desc}\nTo: {after_desc}", False)
            )

        if event.before.start_time != event.after.start_time:
            fields.append(
                (
                    "Start Time Change",
                    f"From: {event.before.start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                    f"To: {event.after.start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}",
                    True,
                )
            )

        if event.before.end_time != event.after.end_time:
            before_time = (
                event.before.end_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                if event.before.end_time
                else "Not specified"
            )
            after_time = (
                event.after.end_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                if event.after.end_time
                else "Not specified"
            )
            fields.append(
                ("End Time Change", f"From: {before_time}\nTo: {after_time}", True)
            )

        if event.before.status != event.after.status:
            fields.append(
                (
                    "Status Change",
                    f"From: {event.before.status}\nTo: {event.after.status}",
                    True,
                )
            )

        if event.before.channel != event.after.channel:
            before_channel = (
                event.before.channel.name if event.before.channel else "None"
            )
            after_channel = event.after.channel.name if event.after.channel else "None"
            fields.append(
                ("Channel Change", f"From: {before_channel}\nTo: {after_channel}", True)
            )

        if (
            hasattr(event.before, "location")
            and hasattr(event.after, "location")
            and event.before.location != event.after.location
        ):
            fields.append(
                (
                    "Location Change",
                    f"From: {event.before.location}\nTo: {event.after.location}",
                    True,
                )
            )

        if event.before.privacy_level != event.after.privacy_level:
            fields.append(
                (
                    "Privacy Level Change",
                    f"From: {event.before.privacy_level}\nTo: {event.after.privacy_level}",
                    True,
                )
            )

        fields.extend(
            [
                ("Event ID", str(event.after.id), True),
                ("Server", event.guild.name if event.guild else "Unknown", True),
                ("Server ID", str(event.guild.id) if event.guild else "Unknown", True),
                (
                    "Updated At",
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    True,
                ),
            ]
        )

        return EventLog(
            title="Server Event Updated",
            description=(f"Scheduled event `{event.before.name}` has been modified"),
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.SCHEDULED_EVENT, "Delete")
    async def on_guild_scheduled_event_delete(
        self, event: GuildScheduledEventDelete
    ) -> EventLog:
        fields = [
            ("Event Name", event.scheduled_event.name, True),
            ("Event ID", str(event.scheduled_event.id), True),
            (
                "Creator",
                (
                    str(event.scheduled_event.creator.display_name)
                    if event.scheduled_event.creator
                    else "Unknown"
                ),
                True,
            ),
            (
                "Scheduled Start",
                event.scheduled_event.start_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
            (
                "Scheduled End",
                (
                    event.scheduled_event.end_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                    if event.scheduled_event.end_time
                    else "Not specified"
                ),
                True,
            ),
            ("Status", str(event.scheduled_event.status), True),
            ("Privacy Level", str(event.scheduled_event.privacy_level), True),
            ("Entity Type", str(event.scheduled_event.entity_type), True),
            (
                "Channel",
                (
                    event.scheduled_event.channel.name
                    if event.scheduled_event.channel
                    else "N/A"
                ),
                True,
            ),
            (
                "Location",
                (
                    event.scheduled_event.location
                    if hasattr(event.scheduled_event, "location")
                    else "N/A"
                ),
                True,
            ),
            ("Server", event.guild.name if event.guild else "Unknown", True),
            ("Server ID", str(event.guild.id) if event.guild else "Unknown", True),
            (
                "Deleted At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        if event.scheduled_event.description:
            fields.append(
                (
                    "Description",
                    (
                        event.scheduled_event.description[:1000] + "..."
                        if len(event.scheduled_event.description) > 1000
                        else event.scheduled_event.description
                    ),
                    False,
                )
            )

        return EventLog(
            title="Server Event Deleted",
            description=(
                f"Scheduled event `{event.scheduled_event.name}` has been cancelled"
            ),
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.STAGE_INSTANCE, "Create")
    async def on_stage_instance_create(self, event: StageInstanceCreate) -> EventLog:
        fields = [
            ("Topic", event.stage_instance.topic, True),
            ("Channel", event.stage_instance.channel.name, True),
            ("Channel ID", str(event.stage_instance.channel.id), True),
            ("Privacy Level", str(event.stage_instance.privacy_level), True),
            ("Guild", event.guild.name if event.guild else "Unknown", True),
            ("Guild ID", str(event.guild.id) if event.guild else "Unknown", True),
            (
                "Created At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        if hasattr(event.stage_instance, "discoverable_disabled"):
            fields.append(
                (
                    "Discoverable",
                    "No" if event.stage_instance.discoverable_disabled else "Yes",
                    True,
                )
            )

        if hasattr(event.stage_instance, "scheduled_event_id"):
            fields.append(
                ("Linked Event ID", str(event.stage_instance.scheduled_event_id), True)
            )

        return EventLog(
            title="Stage Instance Created",
            description=(
                f"A new stage instance has been created in {event.stage_instance.channel.name}"
            ),
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.STAGE_INSTANCE, "Update")
    async def on_stage_instance_update(self, event: StageInstanceUpdate) -> EventLog:
        fields = [
            ("Topic", event.stage_instance.topic, True),
            ("Channel", event.stage_instance.channel.name, True),
            ("Channel ID", str(event.stage_instance.channel.id), True),
            ("Privacy Level", str(event.stage_instance.privacy_level), True),
            ("Guild", event.guild.name if event.guild else "Unknown", True),
            ("Guild ID", str(event.guild.id) if event.guild else "Unknown", True),
            (
                "Updated At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        if hasattr(event.stage_instance, "discoverable_disabled"):
            fields.append(
                (
                    "Discoverable",
                    "No" if event.stage_instance.discoverable_disabled else "Yes",
                    True,
                )
            )

        if hasattr(event.stage_instance, "scheduled_event_id"):
            fields.append(
                ("Linked Event ID", str(event.stage_instance.scheduled_event_id), True)
            )

        return EventLog(
            title="Stage Instance Updated",
            description=(
                f"The stage instance in {event.stage_instance.channel.name} has been modified"
            ),
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.STAGE_INSTANCE, "Delete")
    async def on_stage_instance_delete(self, event: StageInstanceDelete) -> EventLog:
        fields = [
            ("Topic", event.stage_instance.topic, True),
            ("Channel", event.stage_instance.channel.name, True),
            ("Channel ID", str(event.stage_instance.channel.id), True),
            ("Privacy Level", str(event.stage_instance.privacy_level), True),
            ("Guild", event.guild.name if event.guild else "Unknown", True),
            ("Guild ID", str(event.guild.id) if event.guild else "Unknown", True),
            (
                "Deleted At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        if hasattr(event.stage_instance, "discoverable_disabled"):
            fields.append(
                (
                    "Was Discoverable",
                    "No" if event.stage_instance.discoverable_disabled else "Yes",
                    True,
                )
            )

        if hasattr(event.stage_instance, "scheduled_event_id"):
            fields.append(
                ("Linked Event ID", str(event.stage_instance.scheduled_event_id), True)
            )

        return EventLog(
            title="Stage Instance Deleted",
            description=f"The stage instance in {event.stage_instance.channel.name} has been ended.",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.WEBHOOK, "Update")
    async def on_webhooks_update(self, event: WebhooksUpdate) -> EventLog:
        fields = [
            ("Channel ID", str(event.channel_id), True),
            ("Guild ID", str(event.guild_id), True),
            (
                "Updated At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="Webhook Updated",
            description=f"A webhook in channel {event.channel_id} has been modified.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.VOICE, "UserMute")
    async def on_voice_user_mute(self, event: VoiceUserMute) -> EventLog:
        action = "muted" if event.mute else "unmuted"
        fields = [
            ("User", event.author.display_name, True),
            ("User ID", str(event.author.id), True),
            ("Channel", event.channel.name, True),
            ("Channel ID", str(event.channel.id), True),
            ("Action", action.capitalize(), True),
            ("Guild", event.guild.name if event.guild else "Unknown", True),
            ("Guild ID", str(event.guild.id) if event.guild else "Unknown", True),
            (
                "Timestamp",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title=f"User {action.capitalize()} in Voice Channel",
            description=f"{event.author.display_name} was {action} in voice channel {event.channel.name}.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.VOICE, "UserMove")
    async def on_voice_user_move(self, event: VoiceUserMove) -> EventLog:
        guild = event.new_channel.guild if event.new_channel else None

        fields = [
            ("User", event.state.member.display_name, True),
            ("User ID", str(event.state.member.id), True),
            ("From Channel", event.previous_channel.name, True),
            ("From Channel ID", str(event.previous_channel.id), True),
            ("To Channel", event.new_channel.name, True),
            ("To Channel ID", str(event.new_channel.id), True),
            ("Guild", guild.name if guild else "Unknown", True),
            ("Guild ID", str(guild.id) if guild else "Unknown", True),
            (
                "Moved At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="User Moved Voice Channels",
            description=f"{event.state.member.display_name} was moved from {event.previous_channel.name} to {event.new_channel.name}.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.VOICE, "UserDeafen")
    async def on_voice_user_deafen(self, event: VoiceUserDeafen) -> EventLog:
        action = "deafened" if event.deaf else "undeafened"
        fields = [
            ("User", event.author.display_name, True),
            ("User ID", str(event.author.id), True),
            ("Channel", event.channel.name, True),
            ("Channel ID", str(event.channel.id), True),
            ("Action", action.capitalize(), True),
            ("Guild", event.guild.name if event.guild else "Unknown", True),
            ("Guild ID", str(event.guild.id) if event.guild else "Unknown", True),
            (
                "Timestamp",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title=f"User {action.capitalize()} in Voice Channel",
            description=f"{event.author.display_name} was {action} in voice channel {event.channel.name}.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.THREAD, "ListSync")
    async def on_thread_list_sync(self, event: ThreadListSync) -> EventLog:
        active_threads = [thread for thread in event.threads if not thread.archived]
        archived_threads = [thread for thread in event.threads if thread.archived]

        fields = [
            ("Total Threads", str(len(event.threads)), True),
            ("Active Threads", str(len(active_threads)), True),
            ("Archived Threads", str(len(archived_threads)), True),
            ("Affected Channels", str(len(event.channel_ids)), True),
            ("Guild ID", str(event.guild_id), True),
            (
                "Synced At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        thread_details = []
        for thread in event.threads[:5]:
            status = "Active" if not thread.archived else "Archived"
            thread_details.append(f"{thread.name} ({status})")

        if thread_details:
            fields.append(("Thread Details", "\n".join(thread_details), False))
            if len(event.threads) > 5:
                fields.append(("Note", "Showing first 5 threads only", False))

        return EventLog(
            title="Thread List Synchronized",
            description=f"The thread list for {len(event.channel_ids)} channel(s) has been synchronized.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.MESSAGE, "DeleteBulk")
    async def on_message_delete_bulk(self, event: MessageDeleteBulk) -> EventLog:
        fields = [
            ("Channel ID", str(event.channel_id), True),
            ("Messages Deleted", str(len(event.ids)), True),
            ("Guild ID", str(event.guild_id), True),
            (
                "Deleted At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
            (
                "Message IDs",
                "\n".join(str(id) for id in event.ids[:5])
                + ("\n..." if len(event.ids) > 5 else ""),
                False,
            ),
        ]

        return EventLog(
            title="Bulk Message Delete",
            description=f"Multiple messages ({len(event.ids)}) were deleted in channel {event.channel_id}.",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.GUILD, "Unavailable")
    async def on_guild_unavailable(self, event: GuildUnavailable) -> EventLog:
        fields = [
            ("Guild ID", str(event.guild_id), True),
            (
                "Timestamp",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="Guild Unavailable",
            description="A guild has become unavailable. This may be due to a server outage.",
            color=EmbedColor.INFO,
            fields=tuple(fields),
        )

    @event_handler(EventType.GUILD, "StickersUpdate")
    async def on_guild_stickers_update(self, event: GuildStickersUpdate) -> EventLog:
        fields = [
            ("Guild", event.guild.name, True),
            ("Total Stickers", str(len(event.stickers)), True),
            (
                "Updated At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        sticker_details = []
        for sticker in event.stickers[:5]:
            sticker_details.append(f"{sticker.name} ({sticker.format_type})")

        if sticker_details:
            fields.append(("Sticker Details", "\n".join(sticker_details), False))
            if len(event.stickers) > 5:
                fields.append(("Note", "Showing first 5 stickers only", False))

        return EventLog(
            title="Guild Stickers Updated",
            description=f"The stickers for {event.guild.name} have been updated.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.SCHEDULED_EVENT, "UserRemove")
    async def on_guild_scheduled_event_user_remove(
        self, event: GuildScheduledEventUserRemove
    ) -> EventLog:
        fields = [
            ("User ID", str(event.user_id), True),
            ("Event ID", str(event.scheduled_event_id), True),
            (
                "Guild ID",
                str(event.guild_id) if hasattr(event, "guild_id") else "Unknown",
                True,
            ),
            (
                "Removed At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        if hasattr(event, "user") and event.user:
            fields.append(("Username", event.user.username, True))

        return EventLog(
            title="User Removed from Scheduled Event",
            description="A user has been removed from a scheduled event.",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.SCHEDULED_EVENT, "UserAdd")
    async def on_guild_scheduled_event_user_add(
        self, event: GuildScheduledEventUserAdd
    ) -> EventLog:
        fields = [
            ("User ID", str(event.user_id), True),
            ("Event ID", str(event.scheduled_event_id), True),
            (
                "Guild ID",
                str(event.guild_id) if hasattr(event, "guild_id") else "Unknown",
                True,
            ),
            (
                "Added At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        if hasattr(event, "user") and event.user:
            fields.append(("Username", event.user.username, True))

        return EventLog(
            title="User Added to Scheduled Event",
            description="A user has joined a scheduled event.",
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.GUILD, "MembersChunk")
    async def on_guild_members_chunk(self, event: GuildMembersChunk) -> EventLog:
        fields = [
            ("Guild", event.guild.name, True),
            ("Member Count", str(len(event.members)), True),
            ("Chunk Index", str(event.chunk_index), True),
            ("Chunk Count", str(event.chunk_count), True),
            (
                "Received At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        if hasattr(event, "presences") and event.presences:
            fields.append(("Presences Included", "Yes", True))

        member_samples = [
            f"{member.display_name} ({member.id})" for member in event.members[:5]
        ]
        if member_samples:
            fields.append(("Sample Members", "\n".join(member_samples), False))
            if len(event.members) > 5:
                fields.append(("Note", "Showing first 5 members only", False))

        return EventLog(
            title="Guild Members Chunk Received",
            description=f"Received chunk {event.chunk_index + 1}/{event.chunk_count} of members for {event.guild.name}.",
            color=EmbedColor.INFO,
            fields=tuple(fields),
        )

    @event_handler(EventType.GUILD, "Available")
    async def on_guild_available(self, event: GuildAvailable) -> EventLog:
        fields = [
            ("Guild", event.guild.name, True),
            ("Guild ID", str(event.guild.id), True),
            (
                "Member Count",
                (
                    str(event.guild.member_count)
                    if hasattr(event.guild, "member_count")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Available At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="Guild Available",
            description=f"The guild {event.guild.name} is now available.",
            color=EmbedColor.INFO,
            fields=tuple(fields),
        )

    @event_handler(EventType.ENTITLEMENT, "Update")
    async def on_entitlement_update(self, event: EntitlementUpdate) -> EventLog:
        fields = [
            ("Entitlement ID", str(event.entitlement.id), True),
            ("User ID", str(event.entitlement.user_id), True),
            (
                "SKU ID",
                (
                    str(event.entitlement.sku_id)
                    if hasattr(event.entitlement, "sku_id")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Application ID",
                (
                    str(event.entitlement.application_id)
                    if hasattr(event.entitlement, "application_id")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Type",
                (
                    str(event.entitlement.type)
                    if hasattr(event.entitlement, "type")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Updated At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="Entitlement Updated",
            description="An entitlement has been updated.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.ENTITLEMENT, "Delete")
    async def on_entitlement_delete(self, event: EntitlementDelete) -> EventLog:
        fields = [
            ("Entitlement ID", str(event.entitlement.id), True),
            ("User ID", str(event.entitlement.user_id), True),
            (
                "SKU ID",
                (
                    str(event.entitlement.sku_id)
                    if hasattr(event.entitlement, "sku_id")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Application ID",
                (
                    str(event.entitlement.application_id)
                    if hasattr(event.entitlement, "application_id")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Type",
                (
                    str(event.entitlement.type)
                    if hasattr(event.entitlement, "type")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Deleted At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="Entitlement Deleted",
            description="An entitlement has been deleted.",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.ENTITLEMENT, "Create")
    async def on_entitlement_create(self, event: EntitlementCreate) -> EventLog:
        fields = [
            ("Entitlement ID", str(event.entitlement.id), True),
            ("User ID", str(event.entitlement.user_id), True),
            (
                "SKU ID",
                (
                    str(event.entitlement.sku_id)
                    if hasattr(event.entitlement, "sku_id")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Application ID",
                (
                    str(event.entitlement.application_id)
                    if hasattr(event.entitlement, "application_id")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Type",
                (
                    str(event.entitlement.type)
                    if hasattr(event.entitlement, "type")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Created At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="Entitlement Created",
            description="A new entitlement has been created.",
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.AUTOMOD, "Exec")
    async def on_auto_mod_exec(self, event: AutoModExec) -> EventLog:
        fields = [
            ("Guild ID", str(event.guild_id), True),
            ("Action", str(event.action), True),
            ("Rule Trigger Type", str(event.rule_trigger_type), True),
            (
                "Rule ID",
                str(event.rule_id) if hasattr(event, "rule_id") else "Unknown",
                True,
            ),
            (
                "Channel ID",
                str(event.channel_id) if hasattr(event, "channel_id") else "Unknown",
                True,
            ),
            (
                "Message ID",
                str(event.message_id) if hasattr(event, "message_id") else "Unknown",
                True,
            ),
            (
                "Alert System Message ID",
                (
                    str(event.alert_system_message_id)
                    if hasattr(event, "alert_system_message_id")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Content",
                event.content if hasattr(event, "content") else "Unknown",
                False,
            ),
            (
                "Matched Keyword",
                (
                    event.matched_keyword
                    if hasattr(event, "matched_keyword")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Matched Content",
                (
                    event.matched_content
                    if hasattr(event, "matched_content")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Executed At",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                True,
            ),
        ]

        return EventLog(
            title="AutoMod Action Executed",
            description=f"AutoMod has executed a {event.action} action based on rule trigger: {event.rule_trigger_type}",
            color=EmbedColor.INFO,
            fields=tuple(fields),
        )
