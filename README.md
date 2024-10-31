# Statistics

The **Statistics** module is designed to track, monitor, and log various events and activities within a Discord server, providing comprehensive analytics and audit trails.

## Features

- Track and log channel-related events (creation, deletion, updates)
- Monitor role changes and permission modifications
- Record voice channel activities and user states
- Track scheduled events and stage instances
- Monitor webhook updates and bulk message operations
- Log ban/unban activities and member management
- Track emoji and sticker updates
- Monitor thread synchronization and entitlements
- Provide detailed AutoMod execution logs
- Maintain activity statistics in designated forums
- Generate comprehensive event logs with timestamps
- Support paginated viewing of historical data
- Enable real-time monitoring of server activities

## Usage

The module automatically monitors and logs events as they occur. Events are formatted with:

- Descriptive titles and timestamps
- Event-specific details and changes
- Before/after states where applicable
- Related IDs and references
- Color-coded embeds for different event types

The module logs the following event categories:

- **Channel Events**: Creation, deletion, and modifications
- **Role Events**: Role creation, removal, and permission changes
- **Voice Events**: User movements, mute/unmute, deafen states
- **Guild Events**: Server updates, availability, and member chunks
- **Invite Events**: Creation and deletion of invites
- **Ban Events**: Member bans and unbans
- **Scheduled Events**: Creation, updates, and user participation
- **Stage Events**: Stage instance lifecycle
- **Webhook Events**: Webhook modifications
- **Thread Events**: List synchronization
- **Message Events**: Bulk deletions
- **Sticker Events**: Server sticker updates
- **Entitlement Events**: Creation, updates, and deletion
- **AutoMod Events**: Rule execution and actions

## Configuration

Key configuration settings in `main.py` include:

- `GUILD_ID`: Discord server ID for monitoring
- `MONITORED_FORUM_ID`: Forum channel for activity tracking
- `EXCLUDED_CATEGORY_ID`: Category to exclude from monitoring
- `TRANSPARENCY_FORUM_ID`: Forum for logging events
- `STATS_POST_ID`: Post ID for statistics updates
- `LOG_POST_ID`: Post ID for event logs
- `MAX_INTERACTIONS`: Maximum tracked interactions
- `MONITORED_ROLE_IDS`: Set of role IDs to monitor
- `LOG_ROTATION_THRESHOLD`: File size threshold for log rotation
- `LOG_FILE`: Path to statistics log file
- `STATS_FILE_PATH`: Path to statistics data file
