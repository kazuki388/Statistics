import asyncio
import contextlib
import functools
import inspect
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum, auto
from itertools import accumulate
from logging.handlers import RotatingFileHandler
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Optional,
    Type,
    TypeGuard,
    TypeVar,
)

import interactions
from interactions.api.events import (
    AutoModCreated,
    AutoModDeleted,
    AutoModExec,
    AutoModUpdated,
    BanCreate,
    BanRemove,
    BaseEvent,
    ChannelCreate,
    ChannelDelete,
    ChannelUpdate,
    EntitlementCreate,
    EntitlementDelete,
    EntitlementUpdate,
    GuildAuditLogEntryCreate,
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
    IntegrationDelete,
    InteractionCreate,
    InviteCreate,
    InviteDelete,
    MemberAdd,
    MemberRemove,
    MemberUpdate,
    MessageDeleteBulk,
    MessageReactionRemoveAll,
    MessageReactionRemoveEmoji,
    NewThreadCreate,
    PresenceUpdate,
    RoleCreate,
    RoleDelete,
    RoleUpdate,
    StageInstanceCreate,
    StageInstanceDelete,
    StageInstanceUpdate,
    ThreadCreate,
    ThreadDelete,
    ThreadListSync,
    ThreadMembersUpdate,
    ThreadMemberUpdate,
    ThreadUpdate,
    VoiceStateUpdate,
    VoiceUserDeafen,
    VoiceUserMove,
    VoiceUserMute,
    WebhooksUpdate,
)
from interactions.client.errors import Forbidden

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
LOG_POST_ID: int = 1325002367875285083
LOG_FORUM_ID: int = 1159097493875871784
ROLE_POST_ID: int = 1325394043177275445
THREAD_POST_ID: int = 1325393614343376916
MEMBER_POST_ID: int = 1332687058199642112

T = TypeVar("T", covariant=True)
AsyncFunc = Callable[..., T]


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
    author: Optional[str] = None
    author_icon: Optional[str] = None
    footer: Optional[str] = None
    footer_icon: Optional[str] = None
    fields: tuple[tuple[str, Any, bool], ...] = ()


class EventType(IntEnum):
    CHANNEL = auto()
    ROLE = auto()
    MEMBER = auto()
    PRESENCE = auto()
    VOICE = auto()
    GUILD = auto()
    INTERACTION = auto()
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


def format_timestamp(dt: datetime) -> str:
    unix_ts = int(dt.timestamp())
    return f"<t:{unix_ts}:F> (<t:{unix_ts}:R>)"


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
        "automod": "auto_mod",
        "new_thread": "thread",
    }

    special_cases: dict[str, str] = {
        "interaction_create": "InteractionCreate",
        "integration_delete": "IntegrationDelete",
        "guild_audit_log_entry_create": "GuildAuditLogEntryCreate",
        "thread_update": "ThreadUpdate",
        "thread_members_update": "ThreadMembersUpdate",
        "thread_member_update": "ThreadMemberUpdate",
        "thread_delete": "ThreadDelete",
        "new_thread_create": "ThreadCreate",
        "thread_create": "ThreadCreate",
        "message_reaction_remove_all": "MessageReactionRemoveAll",
        "message_reaction_remove_emoji": "MessageReactionRemoveEmoji",
        "automod_create": "AutoModCreated",
        "automod_delete": "AutoModDeleted",
        "automod_update": "AutoModUpdated",
        "presence_update": "PresenceUpdate",
        "member_add": "MemberAdd",
        "member_remove": "MemberRemove",
        "member_update": "MemberUpdate",
    }

    if event_name in special_cases:
        return event_class_mapping.get(special_cases[event_name])

    words: tuple[str, ...] = tuple(
        filter(
            None, (corrections.get(w.lower(), w.lower()) for w in event_name.split("_"))
        )
    )

    def capitalize_words(words_list: list[str]) -> str:
        if not words_list:
            return ""
        return "".join(word.capitalize() for word in words_list)

    def filter_words(predicate: Callable[[str], bool]) -> str:
        return capitalize_words([w for w in words if predicate(w)])

    name_variants: tuple[str, ...] = (
        capitalize_words(list(words)),
        filter_words(lambda w: w != "channel"),
        f"{action.capitalize()}{filter_words(lambda w: w != action.lower())}",
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
                if event_log is not None:
                    await log_event(
                        self, f"{_event_type_name}{_event_action}", event_log
                    )
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
    return interactions.Embed(
        title=event_log.title,
        description=event_log.description,
        color=event_log.color.value,
        author=(
            {"name": event_log.author, "icon_url": event_log.author_icon}
            if event_log.author and event_log.author_icon
            else None
        ),
        footer=(
            {
                "text": event_log.footer,
                "icon_url": event_log.footer_icon,
            }
            if event_log.footer and event_log.footer_icon
            else None
        ),
        timestamp=interactions.Timestamp.now(timezone.utc),
        fields=[
            interactions.EmbedField(
                name=str(name),
                value=str(value) if value else "N/A",
                inline=bool(inline),
            )
            for name, value, inline in event_log.fields
        ],
    )


async def log_event(self, event_name: str, event_log: EventLog) -> None:
    if not event_name or not isinstance(event_log, EventLog):
        logger.error(
            f"Invalid arguments for log_event: name={event_name}, log={type(event_log).__name__}"
        )
        return

    embed = await asyncio.to_thread(create_embed, event_log)

    post_id = LOG_POST_ID
    if event_name.startswith("ROLE"):
        post_id = ROLE_POST_ID
    elif event_name.startswith("THREAD"):
        post_id = THREAD_POST_ID
    elif event_name.startswith("MEMBER"):
        post_id = MEMBER_POST_ID

    async with event_logger(self, event_name):
        try:
            max_retries = 3
            base_delay = 1.0

            for attempt in range(max_retries):
                try:
                    async with self.send_semaphore:
                        forum = await self.bot.fetch_channel(LOG_FORUM_ID)
                        if not forum:
                            logger.error("Could not fetch transparency forum channel")
                            return

                        post = await forum.fetch_post(post_id)
                        if not post:
                            logger.error("Could not fetch log post")
                            return

                        if post.archived:
                            try:
                                await post.edit(archived=False)
                            except Exception as e:
                                logger.error(f"Failed to unarchive post: {e}")
                                return

                        await post.send(embeds=embed)
                        logger.info("Successfully sent embed")
                        return

                except Forbidden as e:
                    logger.error(f"Permission error while sending event log: {e}")
                    return

                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Failed all retries while sending event log: {e}")
                        return

                    delay = base_delay * (2**attempt)
                    logger.warning(
                        f"Attempt {attempt + 1} failed, retrying in {delay}s: {e}"
                    )
                    await asyncio.sleep(delay)

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
        self.send_semaphore: asyncio.Semaphore = asyncio.Semaphore(5)

    # Commands

    module_base: interactions.SlashCommand = interactions.SlashCommand(
        name="stats", description="Activities statistics"
    )

    @module_base.subcommand("roles", sub_cmd_description="Get role statistics")
    @interactions.slash_option(
        name="role",
        description="The role to get statistics for. Leave empty for all roles.",
        opt_type=interactions.OptionType.ROLE,
        required=True,
    )
    @error_handler
    async def role_statistics(
        self, ctx: interactions.SlashContext, role: Optional[interactions.Role] = None
    ) -> None:
        await ctx.defer()

        role_data = (
            [
                RoleRecord(
                    role_id=role.id,
                    display_name=role.name,
                    count=sum(1 for _ in role.members),
                )
            ]
            if role
            else []
        )

        embed = interactions.Embed(
            title="Role Statistics",
            color=EmbedColor.SUCCESS.value,
            fields=[
                interactions.EmbedField(
                    name=f"Role {r.display_name}",
                    value=f"Count: {r.count}",
                    inline=False,
                )
                for r in role_data
            ],
            footer=interactions.EmbedFooter(
                text=f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
            ),
        )

        await ctx.send(embeds=[embed])

    # Event Channel

    @event_handler(EventType.CHANNEL, "Create")
    async def on_channel_create(self, event: ChannelCreate) -> EventLog:
        channel = event.channel
        channel_type = str(channel.type).replace("_", " ").title()
        created_timestamp = format_timestamp(datetime.now(timezone.utc))

        parent_channel = (
            await self.bot.fetch_channel(channel.parent_id)
            if hasattr(channel, "parent_id") and channel.parent_id
            else None
        )

        category_name, category_id = (
            (parent_channel.name, str(parent_channel.id))
            if parent_channel
            else ("None", "N/A")
        )

        permission_details = [
            f"{('Role' if overwrite.type == 0 else 'Member')} {overwrite.id}:\n"
            + (
                f"  Allow: {', '.join(map(str, overwrite.allow))}\n"
                if overwrite.allow
                else ""
            )
            + (
                f"  Deny: {', '.join(map(str, overwrite.deny))}"
                if overwrite.deny
                else ""
            )
            for overwrite in channel.permission_overwrites
            if overwrite.allow or overwrite.deny
        ]

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
            *(
                [("Topic", channel.topic, False)]
                if hasattr(channel, "topic") and channel.topic
                else []
            ),
            *(
                [("Bitrate", f"{channel.bitrate//1000}kbps", True)]
                if hasattr(channel, "bitrate")
                else []
            ),
            *(
                [
                    (
                        "User Limit",
                        str(channel.user_limit) if channel.user_limit > 0 else "∞",
                        True,
                    )
                ]
                if hasattr(channel, "user_limit")
                else []
            ),
            *(
                [("Permission Overwrites", "\n".join(permission_details), False)]
                if permission_details
                else []
            ),
        ]

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
        deleted_timestamp = format_timestamp(datetime.now(timezone.utc))
        created_timestamp = format_timestamp(channel.created_at)
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
            ("Created At", created_timestamp, True),
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
            fields = [*fields, ("Total Messages", str(channel.message_count), True)]

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
    async def on_channel_update(self, event: ChannelUpdate) -> Optional[EventLog]:

        fields = []
        before = event.before
        after = event.after

        if before.name != after.name:
            fields.append(
                ("Name Change", f"- From: `{before.name}`\n- To: `{after.name}`", True)
            )

        if before.type != after.type:
            fields.append(
                (
                    "Type Change",
                    f"- From: {str(before.type).replace('_', ' ').title()}\n- To: {str(after.type).replace('_', ' ').title()}",
                    True,
                )
            )

        if before.parent_id != after.parent_id:
            before_category = before.category.name if before.category else "None"
            after_category = after.category.name if after.category else "None"
            fields.append(
                (
                    "Category Change",
                    f"- From: `{before_category}` (ID: {before.parent_id or 'None'})\n- To: `{after_category}` (ID: {after.parent_id or 'None'}`)",
                    True,
                )
            )

        if before.position != after.position:
            fields.append(
                (
                    "Position Change",
                    f"- From: {before.position}\n- To: {after.position}",
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
                    f"- From: `{before.topic or 'None'}`\n- To: `{after.topic or 'None'}`",
                    False,
                )
            )

        if before.nsfw != after.nsfw:
            fields.append(
                (
                    "NSFW Status",
                    f"- From: {'Enabled' if before.nsfw else 'Disabled'}\n- To: {'Enabled' if after.nsfw else 'Disabled'}",
                    True,
                )
            )

        if hasattr(before, "slowmode_delay") and hasattr(after, "slowmode_delay"):
            if before.slowmode_delay != after.slowmode_delay:
                fields.append(
                    (
                        "Slowmode Change",
                        f"- From: {before.slowmode_delay}s\n- To: {after.slowmode_delay}s",
                        True,
                    )
                )

        if hasattr(before, "bitrate") and hasattr(after, "bitrate"):
            if before.bitrate != after.bitrate:
                fields.append(
                    (
                        "Bitrate Change",
                        f"- From: {before.bitrate//1000}kbps\n- To: {after.bitrate//1000}kbps",
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
                        f"- From: {before_limit}\n- To: {after_limit}",
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
                    format_timestamp(datetime.now(timezone.utc)),
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

    # Event Role

    @event_handler(EventType.ROLE, "Create")
    async def on_role_create(self, event: RoleCreate) -> EventLog:
        role = event.role
        created_timestamp = format_timestamp(datetime.now(timezone.utc))

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
        deleted_timestamp = format_timestamp(datetime.now(timezone.utc))

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
        created_timestamp = format_timestamp(role.created_at)
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
            ("Created At", created_timestamp, True),
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
                ("Color Change", f"- From: {before_color}\n- To: {after_color}", True)
            )

        if before.name != after.name:
            fields.append(
                ("Name Change", f"- From: `{before.name}`\n- To: `{after.name}`", True)
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
                    f"- From: {before.position}\n- To: {after.position}\n"
                    + f"(Moved {'up' if after.position > before.position else 'down'} "
                    + f"by {abs(after.position - before.position)} positions)",
                    True,
                )
            )

        if before.hoist != after.hoist:
            fields.append(
                (
                    "Hoist Status",
                    f"- From: {'Hoisted' if before.hoist else 'Not hoisted'}\n"
                    + f"- To: {'Hoisted' if after.hoist else 'Not hoisted'}",
                    True,
                )
            )

        if before.mentionable != after.mentionable:
            fields.append(
                (
                    "Mentionable Status",
                    f"- From: {'Mentionable' if before.mentionable else 'Not mentionable'}\n"
                    + f"- To: {'Mentionable' if after.mentionable else 'Not mentionable'}",
                    True,
                )
            )

        if before.managed != after.managed:
            fields.append(
                (
                    "Managed Status",
                    f"- From: {'Managed' if before.managed else 'Not managed'}\n"
                    + f"- To: {'Managed' if after.managed else 'Not managed'}",
                    True,
                )
            )

        if before.integration != after.integration:
            fields.append(
                (
                    "Integration Status",
                    f"- From: {'Integrated' if before.integration else 'Not integrated'}\n"
                    + f"- To: {'Integrated' if after.integration else 'Not integrated'}",
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
                    format_timestamp(datetime.now(timezone.utc)),
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

    # Event Voice

    @event_handler(EventType.VOICE, "StateUpdate")
    async def on_voice_state_update(self, event: VoiceStateUpdate) -> EventLog:
        fields = []
        before = event.before
        after = event.after
        timestamp = format_timestamp(datetime.now(timezone.utc))

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
                    + f"- From: `{before_channel}`\n"
                    + f"- To: `{after_channel}`",
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
                        f"User requested to speak at {format_timestamp(after.request_to_speak_timestamp)}",
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
                    ("User", after.member.mention, True),
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
                description=f"Voice state changes detected for {after.member.mention}",
                color=EmbedColor.UPDATE,
                fields=tuple(fields),
            )

        return EventLog(
            title="Voice State Updated",
            description="Voice state changes detected but member information unavailable",
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
            (
                "Timestamp",
                format_timestamp(datetime.now(timezone.utc)),
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
            ("User", event.author.display_name, True),
            ("User ID", str(event.author.id), True),
            ("- From Channel", event.previous_channel.name, True),
            ("- From Channel ID", str(event.previous_channel.id), True),
            ("To Channel", event.new_channel.name, True),
            ("To Channel ID", str(event.new_channel.id), True),
            ("Guild", guild.name if guild else "Unknown", True),
            ("Guild ID", str(guild.id) if guild else "Unknown", True),
            (
                "Moved At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="User Moved Voice Channels",
            description=f"{event.author.display_name} was moved from {event.previous_channel.name} to {event.new_channel.name}.",
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
            (
                "Timestamp",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title=f"User {action.capitalize()} in Voice Channel",
            description=f"{event.author.display_name} was {action} in voice channel {event.channel.name}.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    # Event Interaction

    @event_handler(EventType.INTERACTION, "Create")
    async def on_interaction_create(self, event: InteractionCreate) -> EventLog:
        fields = [
            ("Type", str(event.interaction["type"]), True),
            ("User", event.interaction["user"]["display_name"], True),
            ("User ID", str(event.interaction["user"]["id"]), True),
            ("Channel", event.interaction["channel"]["name"], True),
            ("Channel ID", str(event.interaction["channel"]["id"]), True),
            ("Guild ID", str(event.interaction["guild_id"]), True),
            (
                "Created At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        if "command" in event.interaction:
            fields.append(("Command", event.interaction["command"]["name"], True))

        return EventLog(
            title="Interaction Created",
            description=f"A new interaction was created by {event.interaction['user']['display_name']}",
            color=EmbedColor.INFO,
            fields=tuple(fields),
        )

    @event_handler(EventType.INTEGRATION, "Delete")
    async def on_integration_delete(self, event: IntegrationDelete) -> EventLog:
        fields = [
            ("Integration ID", str(event.id), True),
            ("Guild ID", str(event.guild_id), True),
            (
                "Application ID",
                str(event.application_id) if event.application_id else "N/A",
                True,
            ),
            (
                "Deleted At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="Integration Deleted",
            description="An integration has been deleted from the guild",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    # Event Guild

    # @event_handler(EventType.GUILD, "AuditLogEntryCreate")
    # async def on_guild_audit_log_entry_create(
    #     self, event: GuildAuditLogEntryCreate
    # ) -> EventLog:
    #     entry = event.audit_log_entry
    #     fields = [
    #         ("Action Type", str(entry.action_type), True),
    #         ("User ID", str(entry.user_id), True),
    #         ("Target ID", str(entry.target_id), True),
    #         ("Guild ID", str(event.guild_id), True),
    #         (
    #             "Created At",
    #             format_timestamp(datetime.now(timezone.utc)),
    #             True,
    #         ),
    #     ]

    #     if entry.options:
    #         fields.append(("Options", str(entry.options), True))

    #     if entry.changes:
    #         changes = []
    #         for change in entry.changes:
    #             changes.append(f"{change.key}: {change.old_value} → {change.new_value}")
    #         fields.append(("Changes", "\n".join(changes), False))

    #     if entry.reason:
    #         fields.append(("Reason", entry.reason, False))

    #     return EventLog(
    #         title="Audit Log Entry Created",
    #         description=f"A new audit log entry has been created for action {entry.action_type}",
    #         color=EmbedColor.INFO,
    #         fields=tuple(fields),
    #     )

    @event_handler(EventType.GUILD, "MembersChunk")
    async def on_guild_members_chunk(self, event: GuildMembersChunk) -> EventLog:
        fields = [
            ("Guild", event.guild.name, True),
            ("Member Count", str(len(event.members)), True),
            ("Chunk Index", str(event.chunk_index), True),
            ("Chunk Count", str(event.chunk_count), True),
            ("Nonce", event.nonce if event.nonce else "None", True),
            (
                "Received At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        if event.presences:
            fields.append(("Presences Count", str(len(event.presences)), True))

        member_samples = [
            f"{member.mention} ({member.id})" for member in event.members[:5]
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
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="Guild Available",
            description=f"The guild {event.guild.name} is now available.",
            color=EmbedColor.INFO,
            fields=tuple(fields),
        )

    @event_handler(EventType.GUILD, "Update")
    async def on_guild_update(self, event: GuildUpdate) -> EventLog:
        fields = []
        before = event.before
        after = event.after
        timestamp = format_timestamp(datetime.now(timezone.utc))

        if before.name != after.name:
            fields.append(
                ("Name Change", f"- From: `{before.name}`\n- To: `{after.name}`", True)
            )

        if before.icon != after.icon:
            fields.append(
                (
                    "Icon",
                    "Server icon has been updated\n"
                    + f"Old: {before.icon if before.icon else 'None'}\n"
                    + f"New: {after.icon if after.icon else 'None'}",
                    True,
                )
            )

        if before.banner != after.banner:
            fields.append(
                (
                    "Banner",
                    "Server banner has been updated\n"
                    + f"Old: {before.banner if before.banner else 'None'}\n"
                    + f"New: {after.banner if after.banner else 'None'}",
                    True,
                )
            )

        if before.splash != after.splash:
            fields.append(
                (
                    "Invite Splash",
                    "Invite splash image has been updated\n"
                    + f"Old: {before.splash if before.splash else 'None'}\n"
                    + f"New: {after.splash if after.splash else 'None'}",
                    True,
                )
            )

        if before.discovery_splash != after.discovery_splash:
            fields.append(
                (
                    "Discovery Splash",
                    "Discovery splash image has been updated\n"
                    + f"Old: {before.discovery_splash if before.discovery_splash else 'None'}\n"
                    + f"New: {after.discovery_splash if after.discovery_splash else 'None'}",
                    True,
                )
            )

        if before._owner_id != after._owner_id:
            fields.append(
                (
                    "Ownership Transfer",
                    f"- From: <@{before._owner_id}>\n- To: <@{after._owner_id}>",
                    True,
                )
            )

        if before.verification_level != after.verification_level:
            fields.append(
                (
                    "Verification Level",
                    f"- From: {before.verification_level}\n- To: {after.verification_level}",
                    True,
                )
            )

        if before.explicit_content_filter != after.explicit_content_filter:
            fields.append(
                (
                    "Content Filter",
                    f"- From: {before.explicit_content_filter}\n- To: {after.explicit_content_filter}",
                    True,
                )
            )

        if before.default_message_notifications != after.default_message_notifications:
            fields.append(
                (
                    "Default Notifications",
                    f"- From: {before.default_message_notifications}\n- To: {after.default_message_notifications}",
                    True,
                )
            )

        if before.afk_channel_id != after.afk_channel_id:
            before_afk = str(before.afk_channel_id) if before.afk_channel_id else "None"
            after_afk = str(after.afk_channel_id) if after.afk_channel_id else "None"
            fields.append(
                ("AFK Channel", f"- From: {before_afk}\n- To: {after_afk}", True)
            )

        if before.afk_timeout != after.afk_timeout:
            fields.append(
                (
                    "AFK Timeout",
                    f"- From: {before.afk_timeout} seconds\n- To: {after.afk_timeout} seconds",
                    True,
                )
            )

        if before.system_channel_id != after.system_channel_id:
            before_sys = before.system_channel.name if before.system_channel else "None"
            after_sys = after.system_channel.name if after.system_channel else "None"
            fields.append(
                ("System Channel", f"- From: {before_sys}\n- To: {after_sys}", True)
            )

        if before.rules_channel_id != after.rules_channel_id:
            before_rules = before.rules_channel.name if before.rules_channel else "None"
            after_rules = after.rules_channel.name if after.rules_channel else "None"
            fields.append(
                ("Rules Channel", f"- From: {before_rules}\n- To: {after_rules}", True)
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
                    f"- From: {before_updates}\n- To: {after_updates}",
                    True,
                )
            )

        if before.preferred_locale != after.preferred_locale:
            fields.append(
                (
                    "Preferred Locale",
                    f"- From: {before.preferred_locale}\n- To: {after.preferred_locale}",
                    True,
                )
            )

        if before.premium_tier != after.premium_tier:
            fields.append(
                (
                    "Premium Tier",
                    f"- From: Level {before.premium_tier}\n- To: Level {after.premium_tier}",
                    True,
                )
            )

        if before.premium_subscription_count != after.premium_subscription_count:
            fields.append(
                (
                    "Boost Count",
                    f"- From: {before.premium_subscription_count} boosts\n- To: {after.premium_subscription_count} boosts",
                    True,
                )
            )

        if before.vanity_url_code != after.vanity_url_code:
            fields.append(
                (
                    "Vanity URL",
                    f"- From: {before.vanity_url_code or 'None'}\n- To: {after.vanity_url_code or 'None'}",
                    True,
                )
            )

        if before.description != after.description:
            fields.append(
                (
                    "Description",
                    f"- From: {before.description or 'None'}\n- To: {after.description or 'None'}",
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
                    format_timestamp(after.created_at),
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

    @staticmethod
    def chunk_field_value(value: str, chunk_size: int = 1024) -> list[str]:

        if len(value) <= chunk_size:
            return [value]

        lines = value.split("\n")
        line_lengths = [len(line) + 1 for line in lines]
        cumulative_lengths = list(accumulate(line_lengths))

        chunk_indices = [-1] + [
            i for i, length in enumerate(cumulative_lengths) if length > chunk_size
        ]

        return [
            "\n".join(chunk)
            for chunk in (
                lines[start + 1 : end + 1]
                for start, end in zip(
                    chunk_indices, chunk_indices[1:] + [len(lines) - 1]
                )
            )
            if chunk
        ]

    @event_handler(EventType.GUILD, "Unavailable")
    async def on_guild_unavailable(self, event: GuildUnavailable) -> EventLog:
        fields = [
            ("Guild ID", str(event.guild_id), True),
            (
                "Timestamp",
                format_timestamp(datetime.now(timezone.utc)),
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
            ("Guild ID", str(event.guild_id), True),
            ("Total Stickers", str(len(event.stickers)), True),
            (
                "Updated At",
                format_timestamp(datetime.now(timezone.utc)),
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
            description=f"The stickers for guild {event.guild_id} have been updated.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.GUILD, "EmojisUpdate")
    async def on_guild_emojis_update(self, event: GuildEmojisUpdate) -> EventLog:
        added_emojis = [emoji for emoji in event.after if emoji not in event.before]
        removed_emojis = [emoji for emoji in event.before if emoji not in event.after]
        modified_emojis = []

        for after_emoji in event.after:
            for before_emoji in event.before:
                if after_emoji.id == before_emoji.id:
                    if (
                        after_emoji.name != before_emoji.name
                        or after_emoji.roles != before_emoji.roles
                        or after_emoji.require_colons != before_emoji.require_colons
                        or after_emoji.managed != before_emoji.managed
                        or after_emoji.available != before_emoji.available
                    ):
                        modified_emojis.append((before_emoji, after_emoji))

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

            chunks = self.chunk_field_value("\n\n".join(emoji_details))
            for i, chunk in enumerate(chunks, 1):
                suffix = f" (Part {i})" if len(chunks) > 1 else ""
                fields.append((f"Added Emojis{suffix}", chunk, False))

        if removed_emojis:
            emoji_details = []
            for emoji in removed_emojis:
                emoji_info = [
                    f"Name: {emoji.name}",
                    f"ID: {emoji.id}",
                    f"Animated: {'Yes' if emoji.animated else 'No'}",
                ]
                emoji_details.append("\n".join(emoji_info))

            chunks = self.chunk_field_value("\n\n".join(emoji_details))
            for i, chunk in enumerate(chunks, 1):
                suffix = f" (Part {i})" if len(chunks) > 1 else ""
                fields.append((f"Removed Emojis{suffix}", chunk, False))

        if modified_emojis:
            emoji_details = []
            for old_emoji, new_emoji in modified_emojis:
                changes = []
                if old_emoji.name != new_emoji.name:
                    changes.append(f"Name: {old_emoji.name} → {new_emoji.name}")
                if old_emoji.roles != new_emoji.roles:
                    changes.append(
                        f"Roles: {', '.join(r.name for r in old_emoji.roles)} → {', '.join(r.name for r in new_emoji.roles)}"
                    )
                if old_emoji.available != new_emoji.available:
                    changes.append(
                        f"Available: {'Yes' if old_emoji.available else 'No'} → {'Yes' if new_emoji.available else 'No'}"
                    )
                if changes:
                    emoji_details.append(
                        f"Emoji: {new_emoji.name} ({new_emoji.id})\n"
                        + "\n".join(changes)
                    )

            if emoji_details:
                chunks = self.chunk_field_value("\n\n".join(emoji_details))
                for i, chunk in enumerate(chunks, 1):
                    suffix = f" (Part {i})" if len(chunks) > 1 else ""
                    fields.append((f"Modified Emojis{suffix}", chunk, False))

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
                    format_timestamp(datetime.now(timezone.utc)),
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

    # Event Invite

    @event_handler(EventType.INVITE, "Create")
    async def on_invite_create(self, event: InviteCreate) -> EventLog:
        fields = [
            ("Created By", event.invite.inviter.display_name, True),
            ("Creator ID", str(event.invite.inviter.id), True),
            ("Channel", event.invite.channel.name, True),
            ("Channel ID", str(event.invite.channel.id), True),
            (
                "Max Uses",
                "∞" if event.invite.max_uses == 0 else str(event.invite.max_uses),
                True,
            ),
            ("Current Uses", str(event.invite.uses), True),
            (
                "Expires At",
                (
                    "Never"
                    if not event.invite.expires_at
                    else format_timestamp(event.invite.expires_at)
                ),
                True,
            ),
            ("Temporary", "Yes" if event.invite.temporary else "No", True),
            (
                "Created At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        if hasattr(event.invite, "target_type"):
            fields.append(("Target Type", str(event.invite.target_type), True))
        if hasattr(event.invite, "target_user"):
            fields.append(("Target User", str(event.invite.target_user), True))
        if hasattr(event.invite, "target_application"):
            fields.append(
                ("Target Application", str(event.invite.target_application), True)
            )

        return EventLog(
            title="Server Invite Created",
            description=(
                f"A new invite has been created by {event.invite.inviter.display_name} "
                f"for channel #{event.invite.channel.name}"
            ),
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.INVITE, "Delete")
    async def on_invite_delete(self, event: InviteDelete) -> EventLog:
        fields = [
            ("Channel", event.invite.channel.name, True),
            ("Channel ID", str(event.invite.channel.id), True),
            ("Code", event.invite.code, True),
            ("Guild", event.invite.guild.name, True),
            ("Guild ID", str(event.invite.guild.id), True),
            (
                "Deleted At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="Server Invite Deleted",
            description=(
                f"An invite for channel #{event.invite.channel.name} has been deleted"
            ),
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    # Event Ban

    @event_handler(EventType.BAN, "Create")
    async def on_ban_create(self, event: BanCreate) -> EventLog:
        fields = [
            ("User", event.user.display_name, True),
            ("User ID", str(event.user.id), True),
            ("Username", str(event.user), True),
            ("Bot Account", "Yes" if event.user.bot else "No", True),
            (
                "Account Created",
                format_timestamp(event.user.created_at),
                True,
            ),
            ("Server", event.guild.name, True),
            ("Server ID", str(event.guild.id), True),
            (
                "Banned At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
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
                format_timestamp(event.user.created_at),
                True,
            ),
            ("Server", event.guild.name, True),
            ("Server ID", str(event.guild.id), True),
            (
                "Unbanned At",
                format_timestamp(datetime.now(timezone.utc)),
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

    # Event Scheduled Event

    @event_handler(EventType.SCHEDULED_EVENT, "UserRemove")
    async def on_guild_scheduled_event_user_remove(
        self, event: GuildScheduledEventUserRemove
    ) -> EventLog:
        fields = [
            ("User ID", str(event.user_id), True),
            ("Event ID", str(event.scheduled_event_id), True),
            ("Guild ID", str(event.guild_id), True),
            (
                "Removed At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        if event.user:
            fields.append(("Username", event.user.username, True))

        if event.scheduled_event:
            fields.append(("Event Name", event.scheduled_event.name, True))

        if event.member:
            fields.append(("Member Name", event.member.mention, True))

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
            ("Guild ID", str(event.guild_id), True),
            (
                "Added At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        if event.user:
            fields.append(("Username", event.user.username, True))

        if event.scheduled_event:
            fields.append(("Event Name", event.scheduled_event.name, True))

        if event.member:
            fields.append(("Member Name", event.member.mention, True))

        return EventLog(
            title="User Added to Scheduled Event",
            description="A user has joined a scheduled event.",
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
                    str(event.scheduled_event._creator.display_name)
                    if event.scheduled_event._creator
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
                format_timestamp(event.scheduled_event.start_time),
                True,
            ),
            (
                "End Time",
                (
                    format_timestamp(event.scheduled_event.end_time)
                    if event.scheduled_event.end_time
                    else "Not specified"
                ),
                True,
            ),
            ("Status", str(event.scheduled_event.status), True),
            ("Privacy Level", str(event.scheduled_event.privacy_level), True),
            ("Entity Type", str(event.scheduled_event.entity_type), True),
            (
                "Channel ID",
                (
                    str(event.scheduled_event._channel_id)
                    if event.scheduled_event._channel_id
                    else "N/A"
                ),
                True,
            ),
            ("Description", truncated_desc, False),
            (
                "Created At",
                format_timestamp(datetime.now(timezone.utc)),
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

        if event.before and event.before.name != event.after.name:
            fields.append(
                (
                    "Name Change",
                    f"- From: `{event.before.name}`\n- To: `{event.after.name}`",
                    True,
                )
            )

        if event.before and event.before.description != event.after.description:
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
                (
                    "Description Change",
                    f"- From: {before_desc}\n- To: {after_desc}",
                    False,
                )
            )

        if event.before and event.before.start_time != event.after.start_time:
            fields.append(
                (
                    "Start Time Change",
                    f"- From: {format_timestamp(event.before.start_time)}\n"
                    f"- To: {format_timestamp(event.after.start_time)}",
                    True,
                )
            )

        if event.before and event.before.end_time != event.after.end_time:
            before_time = (
                format_timestamp(event.before.end_time)
                if event.before.end_time
                else "Not specified"
            )
            after_time = (
                format_timestamp(event.after.end_time)
                if event.after.end_time
                else "Not specified"
            )
            fields.append(
                ("End Time Change", f"- From: {before_time}\n- To: {after_time}", True)
            )

        if event.before and event.before.status != event.after.status:
            fields.append(
                (
                    "Status Change",
                    f"- From: {event.before.status}\n- To: {event.after.status}",
                    True,
                )
            )

        if event.before and event.before._channel_id != event.after._channel_id:
            before_channel = (
                event.before.channel.name if event.before.channel else "None"
            )
            after_channel = (
                event.after._channel_id if event.after._channel_id else "None"
            )
            fields.append(
                (
                    "Channel Change",
                    f"- From: {before_channel}\n- To: {after_channel}",
                    True,
                )
            )

        if (
            event.before
            and hasattr(event.before, "location")
            and hasattr(event.after, "location")
            and event.before.location != event.after.location
        ):
            fields.append(
                (
                    "Location Change",
                    f"- From: {event.before.location}\n- To: {event.after.location}",
                    True,
                )
            )

        if event.before and event.before.privacy_level != event.after.privacy_level:
            fields.append(
                (
                    "Privacy Level Change",
                    f"- From: {event.before.privacy_level}\n- To: {event.after.privacy_level}",
                    True,
                )
            )

        fields.extend(
            [
                ("Event ID", str(event.after.id), True),
                (
                    "Server",
                    (event.after.guild.name if event.after.guild else "Unknown"),
                    True,
                ),
                (
                    "Server ID",
                    (str(event.after.guild.id) if event.after.guild else "Unknown"),
                    True,
                ),
                (
                    "Updated At",
                    format_timestamp(datetime.now(timezone.utc)),
                    True,
                ),
            ]
        )

        return EventLog(
            title="Server Event Updated",
            description=f"Scheduled event `{event.after.name}` has been modified",
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
                    str(event.scheduled_event.creator)
                    if event.scheduled_event.creator
                    else "Unknown"
                ),
                True,
            ),
            (
                "Scheduled Start",
                format_timestamp(event.scheduled_event.start_time),
                True,
            ),
            (
                "Scheduled End",
                (
                    format_timestamp(event.scheduled_event.end_time)
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
                    event.scheduled_event.channel_id
                    if hasattr(event.scheduled_event, "channel_id")
                    else "N/A"
                ),
                True,
            ),
            (
                "Location",
                event.location if hasattr(event, "location") else "N/A",
                True,
            ),
            (
                "Server",
                (
                    event.scheduled_event.guild.name
                    if event.scheduled_event.guild
                    else "Unknown"
                ),
                True,
            ),
            (
                "Server ID",
                (
                    str(event.scheduled_event.guild.id)
                    if event.scheduled_event.guild
                    else "Unknown"
                ),
                True,
            ),
            (
                "Deleted At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="Server Event Deleted",
            description=f"Scheduled event `{event.scheduled_event.name}` has been cancelled",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    # Event Stage Instance

    @event_handler(EventType.STAGE_INSTANCE, "Create")
    async def on_stage_instance_create(self, event: StageInstanceCreate) -> EventLog:
        fields = [
            ("Topic", event.stage_instance.topic, True),
            ("Channel", event.stage_instance.channel.name, True),
            ("Channel ID", str(event.stage_instance.channel.id), True),
            ("Privacy Level", str(event.stage_instance.privacy_level), True),
            (
                "Guild",
                (
                    event.stage_instance.guild.name
                    if event.stage_instance.guild
                    else "Unknown"
                ),
                True,
            ),
            (
                "Guild ID",
                (
                    str(event.stage_instance.guild.id)
                    if event.stage_instance.guild
                    else "Unknown"
                ),
                True,
            ),
            (
                "Created At",
                format_timestamp(datetime.now(timezone.utc)),
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
            description=f"A new stage instance has been created in {event.stage_instance.channel.name}",
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
            (
                "Guild",
                (
                    event.stage_instance.guild.name
                    if event.stage_instance.guild
                    else "Unknown"
                ),
                True,
            ),
            (
                "Guild ID",
                (
                    str(event.stage_instance.guild.id)
                    if event.stage_instance.guild
                    else "Unknown"
                ),
                True,
            ),
            (
                "Updated At",
                format_timestamp(datetime.now(timezone.utc)),
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
            (
                "Guild",
                (
                    event.stage_instance.guild.name
                    if event.stage_instance.guild
                    else "Unknown"
                ),
                True,
            ),
            (
                "Guild ID",
                (
                    str(event.stage_instance.guild.id)
                    if event.stage_instance.guild
                    else "Unknown"
                ),
                True,
            ),
            (
                "Deleted At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        if hasattr(event, "discoverable_disabled"):
            fields.extend(
                [
                    (
                        "Was Discoverable",
                        "No" if event.discoverable_disabled else "Yes",
                        True,
                    )
                ]
            )

        if hasattr(event, "scheduled_event_id"):
            fields.append(("Linked Event ID", str(event.scheduled_event_id), True))

        return EventLog(
            title="Stage Instance Deleted",
            description=f"The stage instance in {event.stage_instance.channel.name} has been ended.",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    # Event Webhook

    @event_handler(EventType.WEBHOOK, "Update")
    async def on_webhooks_update(self, event: WebhooksUpdate) -> EventLog:
        fields = [
            ("Channel ID", str(event.channel_id), True),
            ("Guild ID", str(event.guild_id), True),
            (
                "Updated At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="Webhook Updated",
            description=f"A webhook in channel {event.channel_id} has been created, updated, or deleted.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    # Event Thread

    # @event_handler(EventType.THREAD, "Update")
    # async def on_thread_update(self, event: ThreadUpdate) -> EventLog:
    #     fields = []
    #     thread = event.thread

    #     if hasattr(event, "before"):
    #         before = event.before
    #         if before.name != thread.name:
    #             fields.append(
    #                 ("Name Change", f"- From: {before.name}\n- To: {thread.name}", True)
    #             )

    #         if before.archived != thread.archived:
    #             fields.append(
    #                 (
    #                     "Archive Status",
    #                     f"{'Archived' if thread.archived else 'Unarchived'}",
    #                     True,
    #                 )
    #             )

    #         if before.locked != thread.locked:
    #             fields.append(
    #                 (
    #                     "Lock Status",
    #                     f"{'Locked' if thread.locked else 'Unlocked'}",
    #                     True,
    #                 )
    #             )

    #     fields.extend(
    #         [
    #             ("Thread ID", str(thread.id), True),
    #             (
    #                 "Parent Channel",
    #                 thread.parent_channel.name if thread.parent_channel else "Unknown",
    #                 True,
    #             ),
    #             (
    #                 "Updated At",
    #                 format_timestamp(datetime.now(timezone.utc)),
    #                 True,
    #             ),
    #         ]
    #     )

    #     return EventLog(
    #         title="Thread Updated",
    #         description=f"Thread {thread.name} has been modified",
    #         color=EmbedColor.UPDATE,
    #         fields=tuple(fields),
    #     )

    # @event_handler(EventType.THREAD, "MembersUpdate")
    # async def on_thread_members_update(self, event: ThreadMembersUpdate) -> EventLog:
    #     added_members = (
    #         [str(member.id) for member in event.added_members]
    #         if event.added_members
    #         else []
    #     )
    #     removed_members = (
    #         [str(member_id) for member_id in event.removed_member_ids]
    #         if event.removed_member_ids
    #         else []
    #     )

    #     fields = [
    #         ("Thread ID", str(event.id), True),
    #         ("Member Count", str(event.member_count), True),
    #         (
    #             "Added Members",
    #             "\n".join(added_members) if added_members else "None",
    #             False,
    #         ),
    #         (
    #             "Removed Members",
    #             "\n".join(removed_members) if removed_members else "None",
    #             False,
    #         ),
    #         (
    #             "Updated At",
    #             format_timestamp(datetime.now(timezone.utc)),
    #             True,
    #         ),
    #     ]

    #     thread_name = event.channel.name if event.channel else str(event.id)
    #     return EventLog(
    #         title="Thread Members Updated",
    #         description=f"Member list updated for thread {thread_name}",
    #         color=EmbedColor.UPDATE,
    #         fields=tuple(fields),
    #     )

    # @event_handler(EventType.THREAD, "MemberUpdate")
    # async def on_thread_member_update(self, event: ThreadMemberUpdate) -> EventLog:
    #     fields = [
    #         ("Thread ID", str(event.thread.id), True),
    #         ("Member ID", str(event.member.id), True),
    #         (
    #             "Join Timestamp",
    #             (
    #                 format_timestamp(event.member.joined_at)
    #                 if event.member.joined_at
    #                 else "Unknown"
    #             ),
    #             True,
    #         ),
    #         (
    #             "Updated At",
    #             format_timestamp(datetime.now(timezone.utc)),
    #             True,
    #         ),
    #     ]

    #     return EventLog(
    #         title="Thread Member Updated",
    #         description=f"Member status updated in thread {event.thread.name}",
    #         color=EmbedColor.UPDATE,
    #         fields=tuple(fields),
    #     )

    @event_handler(EventType.THREAD, "Delete")
    async def on_thread_delete(self, event: ThreadDelete) -> EventLog:
        fields = [
            ("Thread Name", event.thread.name, True),
            ("Thread ID", str(event.thread.id), True),
            (
                "Parent Channel",
                (
                    event.thread.parent_channel.name
                    if event.thread.parent_channel
                    else "Unknown"
                ),
                True,
            ),
            (
                "Created At",
                (
                    format_timestamp(event.thread.created_at)
                    if event.thread.created_at
                    else "Unknown"
                ),
                True,
            ),
            (
                "Deleted At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
            (
                "Message Count",
                (
                    str(event.thread.message_count)
                    if event.thread.message_count
                    else "Unknown"
                ),
                True,
            ),
        ]

        return EventLog(
            title="Thread Deleted",
            description=f"Thread {event.thread.name} has been deleted",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    # @event_handler(EventType.THREAD, "Create")
    # async def on_new_thread_create(self, event: NewThreadCreate) -> EventLog:
    #     fields = [
    #         ("Thread Name", event.thread.name, True),
    #         ("Thread ID", str(event.thread.id), True),
    #         ("Owner ID", str(event.thread.owner_id), True),
    #         ("Parent Channel", event.thread.parent_channel.name, True),
    #         ("Parent ID", str(event.thread.parent_id), True),
    #         ("Message Count", str(event.thread.message_count), True),
    #         ("Member Count", str(event.thread.member_count), True),
    #         ("Archived", "Yes" if event.thread.archived else "No", True),
    #         ("Locked", "Yes" if event.thread.locked else "No", True),
    #         (
    #             "Created At",
    #             (
    #                 format_timestamp(event.thread.created_at)
    #                 if event.thread.created_at
    #                 else "Unknown"
    #             ),
    #             True,
    #         ),
    #     ]

    #     if event.thread.auto_archive_duration:
    #         fields.append(
    #             ("Auto Archive", f"{event.thread.auto_archive_duration}m", True)
    #         )

    #     return EventLog(
    #         title="New Thread Created",
    #         description=f"A new thread `{event.thread.mention}` has been created",
    #         color=EmbedColor.CREATE,
    #         fields=tuple(fields),
    #     )

    # @event_handler(EventType.THREAD, "Create")
    # async def on_thread_create(self, event: ThreadCreate) -> EventLog:
    #     fields = [
    #         ("Thread Name", event.thread.name, True),
    #         ("Thread ID", str(event.thread.id), True),
    #         (
    #             "Parent Channel",
    #             (
    #                 event.thread.parent_channel.name
    #                 if event.thread.parent_channel
    #                 else "Unknown"
    #             ),
    #             True,
    #         ),
    #         (
    #             "Created At",
    #             format_timestamp(event.thread.created_at),
    #             True,
    #         ),
    #         (
    #             "Auto Archive Duration",
    #             f"{event.thread.auto_archive_duration} minutes",
    #             True,
    #         ),
    #     ]

    #     return EventLog(
    #         title="Thread Created",
    #         description=f"New thread {event.thread.mention} has been created",
    #         color=EmbedColor.CREATE,
    #         fields=tuple(fields),
    #     )

    @event_handler(EventType.THREAD, "ListSync")
    async def on_thread_list_sync(self, event: ThreadListSync) -> EventLog:
        fields = [
            ("Total Threads", str(len(event.threads)), True),
            ("Thread Members", str(len(event.members)), True),
            ("Affected Channels", str(len(event.channel_ids)), True),
            (
                "Synced At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        thread_details = []
        for thread in event.threads[:5]:
            member = next((m for m in event.members if m.id == thread.id), None)
            status = "Member" if member else "Not Member"
            thread_details.append(f"{thread.name} ({status})")

        if thread_details:
            fields.append(("Thread Details", "\n".join(thread_details), False))
            if len(event.threads) > 5:
                fields.append(("Note", "Showing first 5 threads only", False))

        description = f"Thread list synchronized for {len(event.channel_ids)} channel(s). You have access to {len(event.threads)} threads and are a member of {len(event.members)} threads."

        return EventLog(
            title="Thread List Synchronized",
            description=description,
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    # Event Message

    @event_handler(EventType.MESSAGE, "ReactionRemoveAll")
    async def on_message_reaction_remove_all(
        self, event: MessageReactionRemoveAll
    ) -> EventLog:
        fields = [
            ("Message ID", str(event.message.id), True),
            ("Channel", event.message.channel.name, True),
            ("Channel ID", str(event.message.channel.id), True),
            (
                "Author",
                (
                    event.message.author.display_name
                    if event.message.author
                    else "Unknown"
                ),
                True,
            ),
            (
                "Cleared At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="All Reactions Removed",
            description="All reactions have been removed from a message",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.MESSAGE, "ReactionRemoveEmoji")
    async def on_message_reaction_remove_emoji(
        self, event: MessageReactionRemoveEmoji
    ) -> EventLog:
        fields = [
            ("Message ID", str(event.message.id), True),
            ("Channel ID", str(event.message.channel.id), True),
            ("Emoji", str(event.emoji), True),
            ("Guild ID", str(event.guild_id), True),
            (
                "Removed At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="Emoji Reaction Removed",
            description=f"All instances of {event.emoji} reaction have been removed",
            color=EmbedColor.DELETE,
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
                format_timestamp(datetime.now(timezone.utc)),
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

    # Event Entitlement

    @event_handler(EventType.ENTITLEMENT, "Update")
    async def on_entitlement_update(self, event: EntitlementUpdate) -> EventLog:
        fields = [
            ("Entitlement ID", str(event.entitlement.id), True),
            ("User ID", str(event.entitlement.user.id), True),
            (
                "SKU ID",
                (
                    str(event.entitlement.sku_id)
                    if event.entitlement.sku_id
                    else "Unknown"
                ),
                True,
            ),
            (
                "Application ID",
                (
                    str(event.entitlement.application_id)
                    if event.entitlement.application_id
                    else "Unknown"
                ),
                True,
            ),
            (
                "Type",
                str(event.entitlement.type) if event.entitlement.type else "Unknown",
                True,
            ),
            (
                "Updated At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="Entitlement Updated",
            description="A user's subscription has renewed for the next billing period.",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.ENTITLEMENT, "Delete")
    async def on_entitlement_delete(self, event: EntitlementDelete) -> EventLog:
        fields = [
            ("Entitlement ID", str(event.entitlement.id), True),
            ("User ID", str(event.entitlement.user.id), True),
            (
                "SKU ID",
                (
                    str(event.entitlement.sku_id)
                    if event.entitlement.sku_id
                    else "Unknown"
                ),
                True,
            ),
            (
                "Application ID",
                (
                    str(event.entitlement.application_id)
                    if event.entitlement.application_id
                    else "Unknown"
                ),
                True,
            ),
            (
                "Type",
                str(event.entitlement.type) if event.entitlement.type else "Unknown",
                True,
            ),
            (
                "Deleted At",
                format_timestamp(datetime.now(timezone.utc)),
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
            ("User ID", str(event.entitlement.user.id), True),
            (
                "SKU ID",
                (
                    str(event.entitlement.sku_id)
                    if event.entitlement.sku_id
                    else "Unknown"
                ),
                True,
            ),
            (
                "Application ID",
                (
                    str(event.entitlement.application_id)
                    if event.entitlement.application_id
                    else "Unknown"
                ),
                True,
            ),
            (
                "Type",
                str(event.entitlement.type) if event.entitlement.type else "Unknown",
                True,
            ),
            (
                "Created At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="Entitlement Created",
            description="A new entitlement has been created.",
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    # Event AutoMod

    @event_handler(EventType.AUTOMOD, "Create")
    async def on_automod_create(self, event: AutoModCreated) -> EventLog:
        fields = [
            ("Rule Name", event.rule.name, True),
            ("Rule ID", str(event.rule.id), True),
            ("Creator ID", str(event.rule._creator_id), True),
            ("Event Type", str(event.rule.event_type), True),
            ("Trigger Type", str(event.rule.trigger), True),
            ("Enabled", "Yes" if event.rule.enabled else "No", True),
            ("Guild ID", str(event.rule._guild_id), True),
            (
                "Created At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        if event.rule.exempt_roles:
            fields.append(
                (
                    "Exempt Roles",
                    ", ".join(f"<@&{role_id}>" for role_id in event.rule.exempt_roles),
                    False,
                )
            )

        if event.rule.exempt_channels:
            fields.append(
                (
                    "Exempt Channels",
                    ", ".join(
                        f"<#{channel_id}>" for channel_id in event.rule.exempt_channels
                    ),
                    False,
                )
            )

        if event.rule.actions:
            fields.append(
                (
                    "Actions",
                    "\n".join(str(action) for action in event.rule.actions),
                    False,
                )
            )

        return EventLog(
            title="AutoMod Rule Created",
            description=f"A new AutoMod rule `{event.rule.name}` has been created",
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.AUTOMOD, "Delete")
    async def on_automod_delete(self, event: AutoModDeleted) -> EventLog:
        fields = [
            ("Rule Name", event.rule.name, True),
            ("Rule ID", str(event.rule.id), True),
            ("Creator ID", str(event.rule._creator_id), True),
            ("Event Type", str(event.rule.event_type), True),
            ("Trigger Type", str(event.rule.trigger), True),
            ("Guild ID", str(event.rule._guild_id), True),
            (
                "Deleted At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="AutoMod Rule Deleted",
            description=f"AutoMod rule `{event.rule.name}` has been deleted",
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    @event_handler(EventType.AUTOMOD, "Update")
    async def on_automod_update(self, event: AutoModUpdated) -> EventLog:
        fields = []
        before = event.rule
        after = event.rule

        if before.name != after.name:
            fields.append(
                ("Name Change", f"- From: `{before.name}`\n- To: `{after.name}`", True)
            )

        if before.enabled != after.enabled:
            fields.append(
                (
                    "Status Change",
                    f"- From: {'Enabled' if before.enabled else 'Disabled'}\n- To: {'Enabled' if after.enabled else 'Disabled'}",
                    True,
                )
            )

        if before.event_type != after.event_type:
            fields.append(
                (
                    "Event Type Change",
                    f"- From: {before.event_type}\n- To: {after.event_type}",
                    True,
                )
            )

        if before.exempt_roles != after.exempt_roles:
            fields.append(
                (
                    "Exempt Roles Change",
                    f"- From: {', '.join(f'<@&{role_id}>' for role_id in before.exempt_roles)}\n"
                    f"- To: {', '.join(f'<@&{role_id}>' for role_id in after.exempt_roles)}",
                    False,
                )
            )

        if before.exempt_channels != after.exempt_channels:
            fields.append(
                (
                    "Exempt Channels Change",
                    f"- From: {', '.join(f'<#{channel_id}>' for channel_id in before.exempt_channels)}\n"
                    f"- To: {', '.join(f'<#{channel_id}>' for channel_id in after.exempt_channels)}",
                    False,
                )
            )

        if before.actions != after.actions:
            fields.append(
                (
                    "Actions Change",
                    f"- From: {', '.join(str(action) for action in before.actions)}\n"
                    f"- To: {', '.join(str(action) for action in after.actions)}",
                    False,
                )
            )

        fields.extend(
            [
                ("Rule ID", str(after.id), True),
                ("Guild ID", str(after._guild_id), True),
                (
                    "Updated At",
                    format_timestamp(datetime.now(timezone.utc)),
                    True,
                ),
            ]
        )

        return EventLog(
            title="AutoMod Rule Updated",
            description=f"AutoMod rule `{before.name}` has been modified",
            color=EmbedColor.UPDATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.AUTOMOD, "Exec")
    async def on_auto_mod_exec(self, event: AutoModExec) -> EventLog:
        fields = [
            ("Guild ID", str(event.guild.id), True),
            ("Action", str(event.execution.action), True),
            ("Rule Trigger Type", str(event.execution.rule_trigger_type), True),
            (
                "Rule ID",
                (
                    str(event.execution.rule_id)
                    if hasattr(event.execution, "rule_id")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Channel ID",
                str(event.channel.id),
                True,
            ),
            (
                "Message ID",
                (
                    str(event.execution.message_id)
                    if hasattr(event.execution, "message_id")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Alert System Message ID",
                (
                    str(event.execution.alert_system_message_id)
                    if hasattr(event.execution, "alert_system_message_id")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Content",
                (
                    event.execution.content
                    if hasattr(event.execution, "content")
                    else "Unknown"
                ),
                False,
            ),
            (
                "Matched Keyword",
                (
                    event.execution.matched_keyword
                    if hasattr(event.execution, "matched_keyword")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Matched Content",
                (
                    event.execution.matched_content
                    if hasattr(event.execution, "matched_content")
                    else "Unknown"
                ),
                True,
            ),
            (
                "Executed At",
                format_timestamp(datetime.now(timezone.utc)),
                True,
            ),
        ]

        return EventLog(
            title="AutoMod Action Executed",
            description=f"AutoMod has executed a {event.execution.action} action based on rule trigger: {event.execution.rule_trigger_type}",
            color=EmbedColor.INFO,
            fields=tuple(fields),
        )

    # Event Presence

    # @event_handler(EventType.PRESENCE, "Update")
    # async def on_presence_update(self, event: PresenceUpdate) -> EventLog:
    #     fields = []

    #     fields.extend(
    #         [
    #             (
    #                 "User",
    #                 event.user.display_name if event.user else "Unknown",
    #                 True,
    #             ),
    #             (
    #                 "User ID",
    #                 str(event.user.id) if event.user else "Unknown",
    #                 True,
    #             ),
    #             (
    #                 "Status",
    #                 event.status,
    #                 True,
    #             ),
    #             (
    #                 "Activities",
    #                 ", ".join(activity.name for activity in event.activities) or "None",
    #                 True,
    #             ),
    #             (
    #                 "Client Status",
    #                 ", ".join(
    #                     f"{platform}: {status}"
    #                     for platform, status in event.client_status.items()
    #                 )
    #                 or "None",
    #                 True,
    #             ),
    #             (
    #                 "Guild ID",
    #                 str(event.guild_id),
    #                 True,
    #             ),
    #             (
    #                 "Updated At",
    #                 format_timestamp(datetime.now(timezone.utc)),
    #                 True,
    #             ),
    #         ]
    #     )

    #     return EventLog(
    #         title="Presence Updated",
    #         description="User presence has been updated",
    #         color=EmbedColor.UPDATE,
    #         fields=tuple(fields),
    #     )

    # Event Member

    @event_handler(EventType.MEMBER, "Add")
    async def on_member_add(self, event: MemberAdd) -> EventLog:
        fields = [
            ("Member", event.member.display_name, True),
            ("User ID", str(event.member.id), True),
            ("Bot Account", "Yes" if event.member.bot else "No", True),
            ("Guild", event.member.guild.name, True),
            ("Guild ID", str(event.member.guild.id), True),
            (
                "Account Created",
                format_timestamp(event.member.created_at),
                True,
            ),
            (
                "Joined At",
                format_timestamp(event.member.joined_at),
                True,
            ),
            (
                "Assigned Roles",
                (
                    ", ".join(f"{role.mention}" for role in event.member.roles)
                    if event.member.roles
                    else "None"
                ),
                False,
            ),
        ]

        return EventLog(
            title="Member Joined",
            description=f"{event.member.mention} has joined {event.member.guild.name}",
            author=event.member.display_name if event.member.display_name else None,
            author_icon=event.member.avatar_url if event.member.avatar_url else None,
            footer=event.member.guild.name if event.member.guild.name else None,
            footer_icon=(
                event.member.guild.icon.url if event.member.guild.icon.url else None
            ),
            color=EmbedColor.CREATE,
            fields=tuple(fields),
        )

    @event_handler(EventType.MEMBER, "Remove")
    async def on_member_remove(self, event: MemberRemove) -> EventLog:
        fields = [
            ("Member", event.member.display_name, True),
            ("User ID", str(event.member.id), True),
            ("Bot Account", "Yes" if event.member.bot else "No", True),
            ("Guild", event.member.guild.name, True),
            ("Guild ID", str(event.member.guild.id), True),
            (
                "Account Created",
                format_timestamp(event.member.created_at),
                True,
            ),
            (
                "Joined At",
                format_timestamp(event.member.joined_at),
                True,
            ),
            (
                "Previous Roles",
                (
                    ", ".join(f"{role.mention}" for role in event.member.roles)
                    if event.member.roles
                    else "None"
                ),
                False,
            ),
        ]

        return EventLog(
            title="Member Left",
            description=f"{event.member.mention} has left {event.member.guild.name}",
            author=event.member.display_name if event.member.display_name else None,
            author_icon=event.member.avatar_url if event.member.avatar_url else None,
            footer=event.member.guild.name if event.member.guild.name else None,
            footer_icon=(
                event.member.guild.icon.url if event.member.guild.icon.url else None
            ),
            color=EmbedColor.DELETE,
            fields=tuple(fields),
        )

    # @event_handler(EventType.MEMBER, "Update")
    # async def on_member_update(self, event: MemberUpdate) -> EventLog:
    #     fields = []
    #     before = event.before
    #     after = event.after

    #     if before.nick != after.nick:
    #         fields.append(
    #             (
    #                 "Nickname Change",
    #                 f"- From: {before.nick or 'None'}\n- To: {after.nick or 'None'}",
    #                 True,
    #             )
    #         )

    #     if before.roles != after.roles:
    #         added_roles = set(after.roles) - set(before.roles)
    #         removed_roles = set(before.roles) - set(after.roles)

    #         if added_roles:
    #             fields.append(
    #                 ("Added Roles", ", ".join(role.name for role in added_roles), True)
    #             )
    #         if removed_roles:
    #             fields.append(
    #                 (
    #                     "Removed Roles",
    #                     ", ".join(role.name for role in removed_roles),
    #                     True,
    #                 )
    #             )

    #     if before.communication_disabled_until != after.communication_disabled_until:
    #         fields.append(
    #             (
    #                 "Timeout Status",
    #                 f"- From: {before.communication_disabled_until or 'None'}\n- To: {after.communication_disabled_until or 'None'}",
    #                 True,
    #             )
    #         )

    #     fields.extend(
    #         [
    #             ("Member", after.display_name, True),
    #             ("User ID", str(after.id), True),
    #             ("Guild", after.guild.name, True),
    #             (
    #                 "Updated At",
    #                 format_timestamp(datetime.now(timezone.utc)),
    #                 True,
    #             ),
    #         ]
    #     )

    #     return EventLog(
    #         title="Member Updated",
    #         description=f"Member {after.display_name} has been updated",
    #         color=EmbedColor.UPDATE,
    #         fields=tuple(fields),
    #     )
