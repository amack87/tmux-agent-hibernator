# tmux-agent-hibernator

Automatically detect idle AI coding agents running in tmux and hibernate them to save resources. Restore them later with their full working context intact.

## Supported Agents

| Agent | Detection | Restore |
|-------|-----------|---------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `claude` process in tmux | `cat context.md \| claude` |
| [Codex](https://github.com/openai/codex) | `codex` process in tmux | `cat context.md \| codex` |
| [Cursor](https://cursor.com) (CLI) | `cursor` process in tmux | `cat context.md \| cursor` |

New agents can be added by defining an `AgentDefinition` in `hibernator/agents.py`.

## How It Works

1. A daemon runs every 5 minutes (via launchd or cron)
2. Discovers AI agent processes running inside tmux panes
3. Tracks idle time — agent must be at its prompt with no content changes
4. After the idle threshold (default: 12 hours, configurable):
   - Sends a hibernation prompt asking the agent to dump its working context
   - Captures the response from the tmux pane
   - Saves context to a Markdown file
   - Records metadata in a local SQLite database
   - Kills the tmux session
5. On restore: creates a new tmux session and pipes the saved context to the agent

## Install

Requires **Python 3.10+** and **tmux**. No external Python dependencies.

```bash
git clone https://github.com/amack87/tmux-agent-hibernator.git
cd tmux-agent-hibernator
pip install -e .
```

## Usage

```bash
# Show all running agent sessions with idle time
tmux-agent-hibernator status

# List hibernated sessions
tmux-agent-hibernator list
tmux-agent-hibernator list --json

# Show supported agents
tmux-agent-hibernator agents

# Manually hibernate a session
tmux-agent-hibernator hibernate <session-name>

# Restore a hibernated session (by name or ID)
tmux-agent-hibernator restore <session-name-or-id>

# View saved context
tmux-agent-hibernator context <session-name-or-id>

# Show full history (hibernated + restored)
tmux-agent-hibernator history

# Run one monitoring cycle manually
tmux-agent-hibernator check --threshold 30

# Verbose output
tmux-agent-hibernator -v <command>
```

Or run directly without installing:

```bash
python3 cli.py status
```

## Automatic Monitoring (macOS)

Copy the example launchd plist and update the paths:

```bash
cp setup/launchd-example.plist ~/Library/LaunchAgents/com.tmux-agent-hibernator.plist

# Edit the plist to set your paths:
# - WorkingDirectory → your clone location
# - PYTHONPATH → your clone location

# Load and start
launchctl load ~/Library/LaunchAgents/com.tmux-agent-hibernator.plist

# Check status
launchctl list | grep hibernator

# View logs
cat /tmp/tmux-agent-hibernator.log
```

## Configuration

| Setting | Default | Override |
|---------|---------|---------|
| Idle threshold | 720 minutes (12h) | `HIBERNATOR_IDLE_MINUTES` env var, or `--threshold` flag |
| Check interval | 5 minutes | `StartInterval` in the launchd plist |
| Data directory | `~/.tmux-agent-hibernator/` | — |

Data is stored in `~/.tmux-agent-hibernator/`:
- `contexts/` — saved Markdown context files
- `hibernator.db` — SQLite session metadata
- `idle_state.json` — idle tracking state across daemon runs

## Adding a New Agent

Define an `AgentDefinition` in `hibernator/agents.py`:

```python
MY_AGENT = AgentDefinition(
    agent_type=AgentType.MY_AGENT,     # add to the AgentType enum
    display_name="My Agent",
    process_patterns=(r"(?:^|/|\s)my-agent(?:\s|$)",),
    process_excludes=("grep", "hibernator"),
    working_indicators=("processing",),
    input_indicators=("(y/n)",),
    idle_indicators=("ready for input",),
    prompt_pattern=r">",
    hibernation_prompt="Output your working context as plain text.",
    restore_command="cat '{context_path}' | my-agent",
)
```

Then add it to `AGENTS` and `AgentType`.

## Upgrading from claude-hibernator

If you previously used `claude-hibernator`, the data directory (`~/.claude-hibernator`) will be automatically migrated to `~/.tmux-agent-hibernator` on first run. Your existing hibernated sessions and context files are preserved.

## License

MIT
